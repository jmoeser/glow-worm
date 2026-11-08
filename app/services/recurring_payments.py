"""Recurring payments that are confirmed by hand.

Bills-fund rows still feed the Bills forecast, the recommended amount, and
the scheduler. Anything else, and a variable Bills-fund bill, waits until
someone confirms or skips that one occurrence.

Confirm and skip advance ``next_due_date`` with one compare-and-set update.
The transaction is written only when that update changes a row, so a retry
cannot log the same occurrence twice.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.config import TIMEZONE
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


@dataclass
class SkipResult:
    bill: RecurringBill
    message: str


def _today() -> date:
    return datetime.now(TIMEZONE).date()


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


def update_bill_fields(
    db: Session, bill: RecurringBill, data: RecurringBillUpdate
) -> None:
    """Apply a schema update, including paid-from rules."""
    updates = data.model_dump(exclude_unset=True)
    updates.pop("payment_method_id", None)
    updates.pop("contract_id", None)
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


def _cas(db: Session, bill: RecurringBill, due_date: str, *, pay: bool) -> bool:
    """Move next_due_date one step when the loaded occurrence is still current."""
    nxt = _step(due_date, bill.frequency)
    stmt = (
        update(RecurringBill)
        .where(RecurringBill.id == bill.id)
        .where(RecurringBill.is_active == True)
        .where(RecurringBill.next_due_date == due_date)
    )
    if pay:
        stmt = stmt.where(RecurringBill.paid_from == PaidFrom.bills_fund.value)
    else:
        stmt = stmt.where(
            or_(
                RecurringBill.paid_from != PaidFrom.bills_fund.value,
                RecurringBill.bill_type == "variable",
            )
        ).where(
            or_(
                RecurringBill.end_date.is_(None),
                RecurringBill.end_date >= due_date,
            )
        )
    result = db.execute(
        stmt.values(next_due_date=nxt).execution_options(synchronize_session=False)
    )
    changed = getattr(result, "rowcount", 0)
    if changed != 1:
        return False
    db.expire(bill)
    return True


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


def _insert_confirm(
    db: Session,
    bill: RecurringBill,
    source: ResolvedSource,
    amount: Decimal,
    due_date: str,
    contract_id: int | None,
) -> ConfirmResult:
    description = f"Bill payment: {bill.name} to {bill.debtor_provider}"
    due = date.fromisoformat(due_date)
    taken: dict | None = None

    if bill.paid_from == PaidFrom.budget.value:
        if source.budget is None:
            raise PaymentBlocked(
                "no_budget",
                "There's no budget for that month. Set it on the Budgets page first.",
            )
        category_name = bill.category.name if bill.category is not None else "budget"
        txn = Transaction(
            date=due_date,
            description=description,
            amount=amount,
            category_id=bill.category_id,
            type="expense",
            transaction_type="budget_expense",
            budget_id=source.budget.id,
            recurring_bill_id=bill.id,
            contract_id=contract_id,
            is_paid=True,
        )
        db.add(txn)
        db.flush()
        _apply(db, txn)
        overflow = _clawback_fund(db, due)
        if overflow is not None:
            transfer = _transfer_category(db)
            if transfer is not None:
                # A clawback is not a payment, so it does not stamp contract_id.
                back = Transaction(
                    date=_today().isoformat(),
                    description=(
                        f"Taken back for {bill.name} "
                        f"({format_month_year(due)} {category_name})"
                    ),
                    amount=amount,
                    category_id=transfer.id,
                    type="expense",
                    transaction_type="withdrawal",
                    sinking_fund_id=overflow.id,
                    recurring_bill_id=bill.id,
                    is_paid=True,
                )
                db.add(back)
                db.flush()
                _apply(db, back)
                taken = {
                    "fund_id": overflow.id,
                    "fund_name": overflow.name,
                    "amount": float(amount),
                    "transaction_id": back.id,
                }
        if taken is not None:
            message = (
                f"Logged to {calendar.month_name[due.month]} {category_name}; "
                f"{format_money(amount)} taken back from {taken['fund_name']}."
            )
        else:
            message = (
                f"Logged {format_money(amount)} from {category_name} budget "
                f"({format_month_year(due)})."
            )
    else:
        if source.fund is None:
            raise PaymentBlocked(
                "source_missing", "The fund for this payment is missing."
            )
        txn = Transaction(
            date=due_date,
            description=description,
            amount=amount,
            category_id=bill.category_id,
            type="expense",
            transaction_type="regular",
            sinking_fund_id=source.fund.id,
            recurring_bill_id=bill.id,
            contract_id=contract_id,
            is_paid=True,
        )
        db.add(txn)
        db.flush()
        _apply(db, txn)
        message = f"Logged {format_money(amount)} from {source_label(bill)}."

    db.expire(bill)
    db.refresh(bill)
    return ConfirmResult(transaction=txn, bill=bill, taken_back=taken, message=message)


def confirm_payment(
    db: Session, bill: RecurringBill, amount: Decimal, due_date: str
) -> ConfirmResult:
    """Log one occurrence dated on its due date, then step next_due_date."""
    from app.services.household_months import ensure_backfill

    ensure_backfill(db, today_value=_today())
    amount = money(amount)
    if amount <= 0:
        raise PaymentBlocked("invalid_amount", "Amount must be greater than zero.")
    due = date.fromisoformat(due_date)
    # Copy the plan before the due-date update expires the bill.
    contract_id = bill.contract_id
    source = _require_source(db, bill, due) if needs_confirm(bill) else None
    if not _cas(db, bill, due_date, pay=False):
        _reject(db, bill, due_date, pay=False)
    if source is None:
        _reject(db, bill, due_date, pay=False)
    assert source is not None
    return _insert_confirm(db, bill, source, amount, due_date, contract_id)


def skip_payment(db: Session, bill: RecurringBill, due_date: str) -> SkipResult:
    """Step past this occurrence without writing a transaction."""
    if not _cas(db, bill, due_date, pay=False):
        _reject(db, bill, due_date, pay=False)
    logger.info("Skipped recurring payment bill_id=%s due_date=%s", bill.id, due_date)
    db.expire(bill)
    db.refresh(bill)
    message = (
        f"Skipped {format_due_date(due_date)}. "
        f"Next due {format_due_date(bill.next_due_date)}."
    )
    return SkipResult(bill=bill, message=message)


def pay_bills_fund(
    db: Session, bill: RecurringBill, amount: Decimal, payment_date: str
) -> Transaction:
    """Pay a Bills-fund bill on a caller-supplied date.

    The compare-and-set uses the loaded next due date, so a confirm that
    already moved that date leaves this call with nothing to insert.
    The balance is assigned directly, matching the scheduler.
    """
    amount = money(amount)
    if amount <= 0:
        raise PaymentBlocked("invalid_amount", "Amount must be greater than zero.")
    loaded_due = bill.next_due_date
    fund = find_bills_fund(db) if bill.paid_from == PaidFrom.bills_fund.value else None
    if bill.paid_from == PaidFrom.bills_fund.value and fund is None:
        raise PaymentBlocked("source_missing", "The Bills fund is missing.")
    description = f"Bill payment: {bill.name} to {bill.debtor_provider}"
    category_id = bill.category_id
    contract_id = bill.contract_id
    if not _cas(db, bill, loaded_due, pay=True):
        _reject(db, bill, loaded_due, pay=True)
    if fund is None:
        _reject(db, bill, loaded_due, pay=True)
    assert fund is not None
    txn = Transaction(
        date=payment_date,
        description=description,
        amount=amount,
        category_id=category_id,
        type="expense",
        transaction_type="regular",
        sinking_fund_id=fund.id,
        recurring_bill_id=bill.id,
        contract_id=contract_id,
        is_paid=True,
    )
    db.add(txn)
    fund.current_balance = float(money(fund.current_balance) - amount)
    db.expire(bill)
    return txn


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
    }
