"""Recurring payments that are confirmed by hand.

Bills-fund rows still feed the Bills forecast, the recommended amount, and
the scheduler. Anything else, and a variable Bills-fund bill, waits until
someone confirms or skips that one occurrence.

Confirm, skip, manual pay, and auto-pay settle one occurrence through
``transition_occurrence``. That function flushes and does not commit.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.services.clock as clock_module
from app.audit import snapshot_columns
from app.models import (
    Budget,
    Category,
    RecurringBill,
    SinkingFund,
    Transaction,
)
from app.schemas import (
    BillFrequency,
    PaidFrom,
    RecurringBillResponse,
    RecurringBillUpdate,
)
from app.services.money import money
from app.services.transaction_balances import apply_transaction_balance_effects
from app.tasks import advance_due_date, iter_bill_due_dates

logger = logging.getLogger(__name__)


class _Unset:
    """Marks a paid-from fund id that the caller did not send."""


UNSET = _Unset()


class PaymentError(Exception):
    def __init__(self, code: str, detail: str, href: str | None = None):
        self.code = code
        self.detail = detail
        self.href = href
        super().__init__(detail)


class PaymentConflict(PaymentError):
    """409: the occurrence was not advanced."""


class PaymentBlocked(PaymentError):
    """400: nothing was written and the due date stays put."""


class PaidFromError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass
class ResolvedSource:
    budget: Budget | None = None
    fund: SinkingFund | None = None


@dataclass
class ConfirmResult:
    transaction: Transaction
    bill: RecurringBill
    taken_back: dict | None
    message: str
    prior: dict[str, object]


@dataclass
class SkipResult:
    bill: RecurringBill
    message: str
    prior: dict[str, object]


@dataclass
class PayResult:
    """Manual Bills-fund payment plus the bill columns from before the call."""

    transaction: Transaction
    prior: dict[str, object]


def _today() -> date:
    return clock_module.today()


# Test hook. Runs inside the caller's transaction, just before a payment insert.
payment_insert_hook: Callable[[RecurringBill], None] | None = None

_BILL_EDIT_COLUMNS = (
    "name",
    "amount",
    "debtor_provider",
    "frequency",
    "category_id",
    "end_date",
    "is_active",
    "bill_type",
    "foreign_amount",
    "foreign_currency",
    "payment_method_id",
    "contract_id",
    "paid_from",
    "paid_from_fund_id",
)


def format_due_date(value: date | str) -> str:
    """26 Oct 2026."""
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.day} {value.strftime('%b %Y')}"


def format_month_year(value: date | str) -> str:
    """October 2026."""
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.strftime('%B')} {value.year}"


def format_money(amount: Decimal) -> str:
    return f"${money(amount):,.2f}"


def needs_confirm(bill: RecurringBill) -> bool:
    """True when this row is not logged by the scheduler."""
    return bill.paid_from != PaidFrom.bills_fund.value or bill.bill_type == "variable"


def bills_fund_bills(db: Session) -> list[RecurringBill]:
    """Active rows that still count toward Bills figures."""
    return (
        db.query(RecurringBill)
        .filter(
            RecurringBill.is_active == True,
            RecurringBill.paid_from == PaidFrom.bills_fund.value,
        )
        .all()
    )


def find_bills_fund(db: Session) -> SinkingFund | None:
    return (
        db.query(SinkingFund)
        .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)
        .first()
    )


def source_label(bill: RecurringBill) -> str:
    """Bills fund, Groceries budget, or Kids fund."""
    if bill.paid_from == PaidFrom.budget.value:
        category = bill.category
        name = category.name if category is not None else "Budget"
        if category is None or category.is_deleted:
            return f"{name} (deleted)"
        return f"{name} budget"
    if bill.paid_from == PaidFrom.sinking_fund.value:
        fund = bill.paid_from_fund
        if fund is None:
            return "Fund (deleted)"
        if fund.is_deleted:
            return f"{fund.name} (deleted)"
        return fund.name
    return "Bills fund"


def source_short_name(bill: RecurringBill) -> str:
    """Name used in 'paid from Groceries'."""
    if bill.paid_from == PaidFrom.budget.value and bill.category is not None:
        return bill.category.name
    if (
        bill.paid_from == PaidFrom.sinking_fund.value
        and bill.paid_from_fund is not None
    ):
        return bill.paid_from_fund.name
    return "the Bills fund"


def source_is_broken(bill: RecurringBill) -> bool:
    if bill.paid_from == PaidFrom.budget.value:
        category = bill.category
        return (
            category is None
            or category.is_deleted
            or category.type != "expense"
            or not category.is_budget_category
        )
    if bill.paid_from == PaidFrom.sinking_fund.value:
        fund = bill.paid_from_fund
        return fund is None or fund.is_deleted
    return False


def validate_paid_from(
    db: Session,
    paid_from: str,
    fund_id: int | None,
    category_id: int | None,
) -> None:
    """Check a paid-from triple. Callers skip this when those keys did not change."""
    if paid_from not in {item.value for item in PaidFrom}:
        raise PaidFromError("Choose where this is paid from.")

    if paid_from == PaidFrom.sinking_fund.value:
        if fund_id is None:
            raise PaidFromError("Choose a sinking fund.")
        fund = db.query(SinkingFund).filter(SinkingFund.id == fund_id).first()
        if fund is None:
            raise PaidFromError("That sinking fund was not found.")
        if fund.name == "Bills" and not fund.is_deleted:
            raise PaidFromError("To pay from the Bills fund, choose Bills fund.")
        if fund.is_deleted:
            raise PaidFromError("That sinking fund was deleted.")
        category = (
            db.query(Category).filter(Category.id == category_id).first()
            if category_id is not None
            else None
        )
        if category is None or category.is_deleted or category.type != "expense":
            raise PaidFromError("Choose an expense category.")
        return

    if fund_id is not None:
        raise PaidFromError("A fund is only set when Paid from is a sinking fund.")

    if paid_from == PaidFrom.budget.value:
        category = (
            db.query(Category).filter(Category.id == category_id).first()
            if category_id is not None
            else None
        )
        if (
            category is None
            or category.is_deleted
            or category.type != "expense"
            or not category.is_budget_category
        ):
            raise PaidFromError("Choose a budget category that isn't deleted.")


def apply_paid_from(
    db: Session,
    bill: RecurringBill,
    *,
    paid_from: str | None,
    paid_from_fund_id: int | None | _Unset = UNSET,
    category_id: int | None = None,
    force: bool = False,
) -> None:
    """Validate and store paid-from when one of its keys changed.

    Switching away from a sinking fund clears the fund id. A rename of a row
    whose fund was deleted does not come through here, so it still saves.
    """
    resulting_from = paid_from if paid_from is not None else bill.paid_from
    if resulting_from != PaidFrom.sinking_fund.value:
        resulting_fund = None
    elif not isinstance(paid_from_fund_id, _Unset):
        resulting_fund = paid_from_fund_id
    else:
        resulting_fund = bill.paid_from_fund_id
    resulting_category = category_id if category_id is not None else bill.category_id

    source_changed = paid_from is not None and paid_from != bill.paid_from
    fund_changed = (
        not isinstance(paid_from_fund_id, _Unset)
        and paid_from_fund_id != bill.paid_from_fund_id
    ) or (resulting_from != PaidFrom.sinking_fund.value and bill.paid_from_fund_id)
    category_changed = (
        category_id is not None
        and category_id != bill.category_id
        and resulting_from in (PaidFrom.budget.value, PaidFrom.sinking_fund.value)
    )
    if not (force or source_changed or fund_changed or category_changed):
        if category_id is not None and category_id != bill.category_id:
            bill.category_id = category_id
        return

    validate_paid_from(db, resulting_from, resulting_fund, resulting_category)
    bill.paid_from = resulting_from
    bill.paid_from_fund_id = resulting_fund
    if category_id is not None:
        bill.category_id = resulting_category


def _plain(value: object) -> object:
    if isinstance(value, PaidFrom | BillFrequency):
        return value.value
    value_attr = getattr(value, "value", None)
    if isinstance(value_attr, str):
        return value_attr
    return value


def pending_bill_values(bill: RecurringBill) -> dict[str, object]:
    """Column values changed on this instance since it was loaded."""
    state = sa_inspect(bill)
    values: dict[str, object] = {}
    for key in _BILL_EDIT_COLUMNS:
        if state.attrs[key].history.has_changes():
            values[key] = getattr(bill, key)
    return values


def cas_save_bill(
    db: Session, bill: RecurringBill, values: dict[str, object], expected_due: str
) -> None:
    """Write every edited column only while the stored due date is unchanged.

    The bill is expired before the statement so a dirty ``next_due_date``
    cannot flush without the predicate. Zero matched rows saves nothing.
    """
    stored = dict(values)
    stored.pop("last_auto_settled_on", None)
    stored["updated_at"] = datetime.now(UTC)
    db.expire(bill)
    result = db.execute(
        update(RecurringBill)
        .where(RecurringBill.id == bill.id)
        .where(RecurringBill.next_due_date == expected_due)
        .values(**stored)
        .execution_options(synchronize_session=False)
    )
    db.expire(bill)
    if getattr(result, "rowcount", 0) != 1:
        raise PaymentConflict("stale", "This schedule changed. Reload the page.")


def update_bill_fields(
    db: Session, bill: RecurringBill, data: RecurringBillUpdate
) -> None:
    """Apply a schema update, including paid-from rules.

    When ``next_due_date`` is present, ``expected_next_due_date`` is required
    and every edited column is one conditional update. Omitting the date
    leaves the cursor alone.
    """
    updates = data.model_dump(exclude_unset=True)
    updates.pop("payment_method_id", None)
    updates.pop("contract_id", None)
    expected = updates.pop("expected_next_due_date", None)
    date_set = "next_due_date" in updates
    new_due = updates.pop("next_due_date", None) if date_set else None
    expected_due: str | None = None
    if date_set:
        if expected is None or not str(expected).strip():
            raise PaymentBlocked(
                "expected_due",
                "Send the due date you last read.",
            )
        try:
            new_due = date.fromisoformat(str(new_due)).isoformat()
            expected_due = date.fromisoformat(str(expected)).isoformat()
        except ValueError as exc:
            raise PaymentBlocked("invalid_date", "Enter a real date.") from exc
    paid_from_set = "paid_from" in updates
    paid_from = _plain(updates.pop("paid_from", None)) if paid_from_set else None
    fund_set = "paid_from_fund_id" in updates
    fund_id = updates.pop("paid_from_fund_id", None)
    category_set = "category_id" in updates
    category_id = updates.pop("category_id", None)

    for field, value in updates.items():
        if field in ("frequency", "bill_type") and value is not None:
            setattr(bill, field, _plain(value))
        else:
            setattr(bill, field, value)

    apply_paid_from(
        db,
        bill,
        paid_from=str(paid_from) if paid_from is not None else None,
        paid_from_fund_id=fund_id if fund_set else UNSET,
        category_id=category_id if category_set else None,
    )
    if date_set:
        if expected_due is None or new_due is None:
            raise PaymentBlocked("invalid_date", "Enter a real date.")
        values = pending_bill_values(bill)
        values.pop("next_due_date", None)
        values["next_due_date"] = new_due
        cas_save_bill(db, bill, values, expected_due)


def serialize_bill(bill: RecurringBill) -> dict:
    payload = RecurringBillResponse.model_validate(bill).model_dump(mode="json")
    payload["paid_from_label"] = source_label(bill)
    return payload


def paying_bills_for_fund(db: Session, fund_id: int) -> list[RecurringBill]:
    return (
        db.query(RecurringBill)
        .filter(
            RecurringBill.is_active == True,
            RecurringBill.paid_from == PaidFrom.sinking_fund.value,
            RecurringBill.paid_from_fund_id == fund_id,
        )
        .order_by(RecurringBill.id)
        .all()
    )


def paying_bills_for_category(db: Session, category_id: int) -> list[RecurringBill]:
    return (
        db.query(RecurringBill)
        .filter(
            RecurringBill.is_active == True,
            RecurringBill.paid_from == PaidFrom.budget.value,
            RecurringBill.category_id == category_id,
        )
        .order_by(RecurringBill.id)
        .all()
    )


def delete_blocked_message(bills: list[RecurringBill], kind: str) -> str | None:
    if not bills:
        return None
    return f"{bills[0].name} is paid from this {kind}. Change that first."


def _budget_for(db: Session, category_id: int, due: date) -> Budget | None:
    return (
        db.query(Budget)
        .filter(
            Budget.category_id == category_id,
            Budget.month == due.month,
            Budget.year == due.year,
        )
        .first()
    )


def _require_source(db: Session, bill: RecurringBill, due: date) -> ResolvedSource:
    """Raise PaymentBlocked when this confirm would land in the wrong place."""
    if bill.paid_from == PaidFrom.budget.value:
        category = bill.category
        name = category.name if category is not None else "That category"
        if (
            category is None
            or category.is_deleted
            or category.type != "expense"
            or not category.is_budget_category
        ):
            raise PaymentBlocked(
                "not_budget",
                f"{name} isn't a budget category any more. "
                "Change where this is paid from.",
                href="/bills",
            )
        budget = _budget_for(db, category.id, due)
        if budget is None:
            raise PaymentBlocked(
                "no_budget",
                f"There's no {name} budget for {format_month_year(due)}. "
                "Set it on the Budgets page first.",
                href=f"/budgets?month={due.month}&year={due.year}",
            )
        return ResolvedSource(budget=budget)

    if bill.paid_from == PaidFrom.sinking_fund.value:
        fund = bill.paid_from_fund
        if fund is None or fund.is_deleted:
            name = fund.name if fund is not None else "The fund"
            raise PaymentBlocked(
                "source_missing",
                f"{name} was deleted. Change where this is paid from.",
                href="/bills",
            )
        return ResolvedSource(fund=fund)

    fund = find_bills_fund(db)
    if fund is None:
        raise PaymentBlocked(
            "source_missing",
            "The Bills fund is missing.",
            href="/bills",
        )
    return ResolvedSource(fund=fund)


def _problem(db: Session, bill: RecurringBill, due: date) -> PaymentBlocked | None:
    try:
        _require_source(db, bill, due)
    except PaymentBlocked as exc:
        return exc
    return None


def _step(due_date: str, frequency: str) -> str:
    return advance_due_date(date.fromisoformat(due_date), frequency).isoformat()


def _end_open(due_date: str):
    return or_(
        RecurringBill.end_date.is_(None),
        RecurringBill.end_date >= due_date,
    )


def _cas(
    db: Session,
    bill: RecurringBill,
    due_date: str,
    frequency: str,
    action: str,
    auto_today: date | None,
) -> bool:
    """Move next_due_date one step when this occurrence is still current."""
    nxt = _step(due_date, frequency)
    values: dict[str, object] = {"next_due_date": nxt}
    stmt = (
        update(RecurringBill)
        .where(RecurringBill.id == bill.id)
        .where(RecurringBill.is_active == True)
        .where(RecurringBill.next_due_date == due_date)
    )
    if action in ("confirm", "skip"):
        stmt = stmt.where(
            or_(
                RecurringBill.paid_from != PaidFrom.bills_fund.value,
                RecurringBill.bill_type == "variable",
            )
        ).where(_end_open(due_date))
    elif action == "manual_pay":
        stmt = stmt.where(RecurringBill.paid_from == PaidFrom.bills_fund.value).where(
            _end_open(due_date)
        )
    else:
        today_str = auto_today.isoformat() if auto_today is not None else due_date
        stmt = stmt.where(
            RecurringBill.paid_from == PaidFrom.bills_fund.value,
            RecurringBill.bill_type == "fixed",
            _end_open(due_date),
            RecurringBill.next_due_date <= today_str,
            or_(
                RecurringBill.last_auto_settled_on.is_(None),
                RecurringBill.last_auto_settled_on != today_str,
            ),
        )
        values["last_auto_settled_on"] = today_str
    # A dirty cursor must not flush before this predicate runs.
    db.expire(bill)
    result = db.execute(
        stmt.values(**values).execution_options(synchronize_session=False)
    )
    db.expire(bill)
    return getattr(result, "rowcount", 0) == 1


def _reject(db: Session, bill: RecurringBill, due_date: str, *, pay: bool) -> None:
    """Drop any in-memory due date so a later flush cannot write it back."""
    db.expire(bill)
    db.refresh(bill)
    if not bill.is_active:
        raise PaymentConflict("inactive", "This payment is no longer active.")
    if pay:
        if bill.paid_from != PaidFrom.bills_fund.value:
            raise PaymentConflict(
                "wrong_source",
                f"This is paid from {source_short_name(bill)}. Confirm it instead.",
            )
        if (
            bill.end_date
            and due_date > bill.end_date
            and bill.next_due_date == due_date
        ):
            raise PaymentConflict(
                "ended",
                f"This payment ended on {format_due_date(bill.end_date)}.",
            )
        raise PaymentConflict("stale", "Already done. Showing what's next.")
    if not needs_confirm(bill):
        raise PaymentConflict(
            "wrong_source",
            "This bill is paid automatically from the Bills fund.",
        )
    if bill.end_date and due_date > bill.end_date and bill.next_due_date == due_date:
        raise PaymentConflict(
            "ended",
            f"This payment ended on {format_due_date(bill.end_date)}.",
        )
    raise PaymentConflict("stale", "Already done. Showing what's next.")


def _apply(db: Session, txn: Transaction) -> None:
    apply_transaction_balance_effects(
        db,
        sinking_fund_id=txn.sinking_fund_id,
        txn_type=txn.type,
        amount=float(txn.amount),
        budget_id=txn.budget_id,
        transaction_type=txn.transaction_type,
    )


def _transfer_category(db: Session) -> Category | None:
    return (
        db.query(Category)
        .filter(Category.type == "transfer", Category.is_deleted == False)
        .first()
    )


def month_was_swept(db: Session, due: date) -> bool:
    """True when that due month was settled with a fund that can be debited.

    ``surplus``, ``shortfall``, and ``zero`` count. ``no_overflow``,
    ``unresolved``, a missing row, and a month with no sweep record do not.
    Callers that move money must already have run ``ensure_backfill``.
    """
    from app.services.household_months import clawback_outcome

    return clawback_outcome(db, due.year, due.month) is not None


def _clawback_fund(db: Session, due: date) -> SinkingFund | None:
    """Overflow fund recorded on the due month, not today's setting."""
    from app.services.household_months import clawback_outcome

    found = clawback_outcome(db, due.year, due.month)
    if found is None:
        return None
    _outcome, fund_id = found
    fund = db.get(SinkingFund, fund_id)
    if fund is None or fund.is_deleted:
        return None
    return fund


@dataclass
class _BillSnapshot:
    """Scalars copied before the bill is expired. The cursor is not included."""

    contract_id: int | None
    name: str
    provider: str
    category_id: int
    frequency: str


def _copy_bill(bill: RecurringBill) -> _BillSnapshot:
    return _BillSnapshot(
        contract_id=bill.contract_id,
        name=bill.name,
        provider=bill.debtor_provider,
        category_id=bill.category_id,
        frequency=bill.frequency,
    )


def _add_payment(db: Session, txn: Transaction, bill: RecurringBill) -> None:
    if payment_insert_hook is not None:
        payment_insert_hook(bill)
    try:
        db.add(txn)
        db.flush()
    except IntegrityError as exc:
        raise PaymentConflict("stale", "Already done. Showing what's next.") from exc
    _apply(db, txn)


def _insert_clawback(
    db: Session,
    bill: RecurringBill,
    payment: Transaction,
    amount: Decimal,
    due: date,
    name: str,
    category_name: str,
) -> dict | None:
    overflow = _clawback_fund(db, due)
    if overflow is None:
        return None
    transfer = _transfer_category(db)
    if transfer is None:
        return None
    back = Transaction(
        date=_today().isoformat(),
        description=(
            f"Taken back for {name} ({format_month_year(due)} {category_name})"
        ),
        amount=amount,
        category_id=transfer.id,
        type="expense",
        transaction_type="withdrawal",
        sinking_fund_id=overflow.id,
        recurring_bill_id=bill.id,
        adjustment_for_transaction_id=payment.id,
        is_paid=True,
    )
    db.add(back)
    db.flush()
    _apply(db, back)
    return {
        "fund_id": overflow.id,
        "fund_name": overflow.name,
        "amount": float(amount),
        "transaction_id": back.id,
    }


def transition_occurrence(
    db: Session,
    bill: RecurringBill,
    action: str,
    due_date: str,
    amount: Decimal | None = None,
    ledger_date: str | None = None,
) -> ConfirmResult | SkipResult | Transaction:
    """Settle one occurrence. Flush only. The caller commits.

    ``action`` is ``confirm``, ``skip``, ``manual_pay``, or ``auto``.
    A payment stores ``due_date`` as ``occurrence_due_date``. Skip writes
    no row. Does not call ``ensure_backfill``.
    """
    if action not in ("confirm", "skip", "manual_pay", "auto"):
        raise PaymentBlocked("invalid_action", "Unknown payment action.")
    paid_amount: Decimal | None = None
    if action != "skip":
        if amount is None:
            raise PaymentBlocked("invalid_amount", "Amount must be greater than zero.")
        paid_amount = money(amount)
        if paid_amount <= 0:
            raise PaymentBlocked("invalid_amount", "Amount must be greater than zero.")
    due = date.fromisoformat(due_date)
    due_date = due.isoformat()
    # Copy columns before expire/refresh. Callers that expired the bill
    # earlier (confirm) replace this with their own earlier snapshot.
    prior = snapshot_columns(bill)
    copied = _copy_bill(bill)
    # Drop a dirty cursor before any query. ensure_backfill already ran
    # for confirm, and it must not have committed this date.
    db.expire(bill)

    auto_today: date | None = None
    source: ResolvedSource | None = None
    if action == "confirm":
        if needs_confirm(bill):
            source = _require_source(db, bill, due)
    elif action == "manual_pay":
        fund = (
            find_bills_fund(db) if bill.paid_from == PaidFrom.bills_fund.value else None
        )
        if bill.paid_from == PaidFrom.bills_fund.value and fund is None:
            raise PaymentBlocked("source_missing", "The Bills fund is missing.")
        if fund is not None:
            db.expire(fund, ["current_balance"])
            source = ResolvedSource(fund=fund)
    elif action == "auto":
        auto_today = date.fromisoformat(ledger_date) if ledger_date else _today()
        fund = find_bills_fund(db)
        if fund is None:
            raise PaymentBlocked("source_missing", "The Bills fund is missing.")
        db.expire(fund, ["current_balance"])
        source = ResolvedSource(fund=fund)

    frequency = copied.frequency
    if not _cas(db, bill, due_date, frequency, action, auto_today):
        _reject(db, bill, due_date, pay=action in ("manual_pay", "auto"))
    if action == "confirm" and source is None:
        _reject(db, bill, due_date, pay=False)
    if action == "manual_pay" and (source is None or source.fund is None):
        _reject(db, bill, due_date, pay=True)

    if action == "skip":
        logger.info(
            "Skipped recurring payment bill_id=%s due_date=%s", bill.id, due_date
        )
        db.expire(bill)
        db.refresh(bill)
        message = (
            f"Skipped {format_due_date(due_date)}. "
            f"Next due {format_due_date(bill.next_due_date)}."
        )
        return SkipResult(bill=bill, message=message, prior=prior)

    assert paid_amount is not None
    name = copied.name
    provider = copied.provider
    category_id = copied.category_id
    contract_id = copied.contract_id
    category = db.get(Category, category_id)
    category_name = category.name if category is not None else "budget"
    if action == "auto":
        description = f"Auto-payment: {name} to {provider}"
        ledger = (
            auto_today.isoformat() if auto_today is not None else _today().isoformat()
        )
    elif action == "manual_pay":
        description = f"Bill payment: {name} to {provider}"
        ledger = ledger_date or _today().isoformat()
    else:
        description = f"Bill payment: {name} to {provider}"
        ledger = due_date

    taken: dict | None = None
    if action == "confirm" and bill.paid_from == PaidFrom.budget.value:
        if source is None or source.budget is None:
            raise PaymentBlocked(
                "no_budget",
                "There's no budget for that month. Set it on the Budgets page first.",
            )
        txn = Transaction(
            date=ledger,
            description=description,
            amount=paid_amount,
            category_id=category_id,
            type="expense",
            transaction_type="budget_expense",
            budget_id=source.budget.id,
            recurring_bill_id=bill.id,
            contract_id=contract_id,
            occurrence_due_date=due_date,
            is_paid=True,
        )
        _add_payment(db, txn, bill)
        taken = _insert_clawback(db, bill, txn, paid_amount, due, name, category_name)
        if taken is not None:
            message = (
                f"Logged to {calendar.month_name[due.month]} {category_name}; "
                f"{format_money(paid_amount)} taken back from {taken['fund_name']}."
            )
        else:
            message = (
                f"Logged {format_money(paid_amount)} from {category_name} budget "
                f"({format_month_year(due)})."
            )
        db.expire(bill)
        db.refresh(bill)
        db.flush()
        return ConfirmResult(
            transaction=txn, bill=bill, taken_back=taken, message=message, prior=prior
        )

    if source is None or source.fund is None:
        raise PaymentBlocked("source_missing", "The fund for this payment is missing.")
    txn = Transaction(
        date=ledger,
        description=description,
        amount=paid_amount,
        category_id=category_id,
        type="expense",
        transaction_type="regular",
        sinking_fund_id=source.fund.id,
        recurring_bill_id=bill.id,
        contract_id=contract_id,
        occurrence_due_date=due_date,
        is_paid=True,
    )
    _add_payment(db, txn, bill)
    db.expire(bill)
    db.refresh(bill)
    db.flush()
    if action == "confirm":
        message = f"Logged {format_money(paid_amount)} from {source_label(bill)}."
        return ConfirmResult(
            transaction=txn,
            bill=bill,
            taken_back=None,
            message=message,
            prior=prior,
        )
    return txn


def confirm_payment(
    db: Session, bill: RecurringBill, amount: Decimal, due_date: str
) -> ConfirmResult:
    """Log one occurrence dated on its due date, then step next_due_date."""
    from app.services.household_months import ensure_backfill

    # Snapshot before expire and before ensure_backfill, which may commit.
    prior = snapshot_columns(bill)
    # The backfill commit must not store a cursor move or a payment.
    db.expire(bill, ["next_due_date"])
    ensure_backfill(db, today_value=_today())
    result = transition_occurrence(
        db, bill, "confirm", due_date, amount=amount, ledger_date=due_date
    )
    assert isinstance(result, ConfirmResult)
    result.prior = prior
    return result


def skip_payment(db: Session, bill: RecurringBill, due_date: str) -> SkipResult:
    """Step past this occurrence without writing a transaction."""
    result = transition_occurrence(db, bill, "skip", due_date)
    assert isinstance(result, SkipResult)
    return result


def pay_bills_fund(
    db: Session, bill: RecurringBill, amount: Decimal, payment_date: str
) -> PayResult:
    """Pay the current Bills-fund occurrence on a caller-supplied ledger date.

    The occurrence is the cursor at call time, which may differ from the
    ledger date. Manual pay does not read or write ``last_auto_settled_on``.
    ``prior`` is the bill's columns from before this call expires the row.
    """
    prior = snapshot_columns(bill)
    loaded_due = bill.next_due_date
    result = transition_occurrence(
        db,
        bill,
        "manual_pay",
        loaded_due,
        amount=amount,
        ledger_date=payment_date,
    )
    assert isinstance(result, Transaction)
    return PayResult(transaction=result, prior=prior)


def due_to_confirm(db: Session, today: date | None = None) -> list[dict]:
    """One row per bill, oldest unconfirmed occurrence first."""
    today = today or _today()
    today_str = today.isoformat()
    bills = (
        db.query(RecurringBill)
        .filter(
            RecurringBill.is_active == True,
            RecurringBill.next_due_date <= today_str,
        )
        .all()
    )
    rows: list[dict] = []
    for bill in bills:
        if not needs_confirm(bill):
            continue
        if bill.end_date and bill.next_due_date > bill.end_date:
            continue
        occurrences = list(
            iter_bill_due_dates(
                bill.next_due_date, bill.frequency, today, bill.end_date
            )
        )
        due = date.fromisoformat(bill.next_due_date)
        blocked = _problem(db, bill, due)
        problem = None
        if blocked is not None:
            problem = {"code": blocked.code, "detail": blocked.detail}
            if blocked.href:
                problem["href"] = blocked.href
        rows.append(
            {
                "bill_id": bill.id,
                "name": bill.name,
                "provider": bill.debtor_provider,
                "expected_amount": money(bill.amount),
                "due_date": bill.next_due_date,
                "more_due": max(0, len(occurrences) - 1),
                "paid_from": bill.paid_from,
                "paid_from_label": source_label(bill),
                "budget_month": (
                    format_month_year(due)
                    if bill.paid_from == PaidFrom.budget.value
                    else None
                ),
                "problem": problem,
            }
        )
    rows.sort(key=lambda row: (row["due_date"], row["bill_id"]))
    return rows


def confirm_transaction_payload(txn: Transaction) -> dict:
    return {
        "id": txn.id,
        "date": txn.date,
        "amount": float(txn.amount),
        "description": txn.description,
        "occurrence_due_date": txn.occurrence_due_date,
    }
