"""Balance effects of a household transaction.

The transactions HTML routes, the JSON API, and the MCP transaction tools
all call these helpers so a sinking fund's stored balance and a budget's
spent amount stay aligned with the ledger. ``fund_balance`` still moves
only for a budget transfer.

A sinking-fund change is one ``UPDATE`` of ``current_balance`` by a quantized
delta, then that attribute is expired on any loaded fund. ``spent_amount``
stays a read-modify-write. Income allocation still assigns the whole balance
elsewhere; this helper is not race-free.
"""

from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Budget, SinkingFund, Transaction
from app.services.money import money

_CENT = Decimal("0.01")

_LOCKED_PAYMENT_FIELDS = (
    "amount",
    "type",
    "transaction_type",
    "category_id",
    "sinking_fund_id",
    "budget_id",
    "recurring_bill_id",
)


class RecordedPaymentLocked(Exception):
    """A recorded bill payment or its clawback cannot change money fields."""

    def __init__(self, detail: str | None = None):
        self.detail = detail or (
            "This recorded payment can't change its amount, type, category, "
            "fund, budget, or bill. Delete it and record it again."
        )
        super().__init__(self.detail)


def is_recorded_bill_payment(txn: Transaction) -> bool:
    """True when this row settled an occurrence or is its clawback."""
    return (
        txn.occurrence_due_date is not None
        or txn.adjustment_for_transaction_id is not None
    )


def _plain(value: object) -> object:
    value_attr = getattr(value, "value", None)
    if isinstance(value_attr, str):
        return value_attr
    return value


def assert_recorded_payment_unchanged(txn: Transaction, changes: dict) -> None:
    """Refuse a money-field edit on a recorded bill payment.

    Description, ledger date, and ``is_paid`` are not in ``changes`` checks
    here. A posted value equal to the stored value is not a change. Rows
    with both new columns null stay fully editable, including a hand-entered
    row that names a bill and a one-off Bills fund expense.
    """
    if not is_recorded_bill_payment(txn):
        return
    for field in _LOCKED_PAYMENT_FIELDS:
        if field not in changes:
            continue
        new = _plain(changes[field])
        old = getattr(txn, field)
        if field == "amount":
            if money(old) != money(new):
                raise RecordedPaymentLocked()
        elif new != old:
            raise RecordedPaymentLocked()


def _expire_fund_balance(db: Session, fund_id: int) -> None:
    fund = db.get(SinkingFund, fund_id)
    if fund is not None:
        db.expire(fund, ["current_balance"])


def adjust_sinking_fund_balance(
    db: Session,
    sinking_fund_id: int | None,
    txn_type: str,
    amount: float,
    reverse: bool = False,
) -> None:
    """Adjust a sinking fund's current_balance for a transaction.

    ``income`` and ``transfer`` add the amount. Every other ``type`` subtracts.
    ``is_paid`` and ``transaction_type`` are not inputs and do not affect the
    balance. Pass reverse=True to undo a previously applied transaction.
    A missing fund id, or an id that does not match a row, is a no-op.
    """
    if sinking_fund_id is None:
        return
    fund = db.get(SinkingFund, sinking_fund_id)
    if fund is None:
        return
    delta = (
        Decimal(str(amount))
        if txn_type in ("income", "transfer")
        else -Decimal(str(amount))
    )
    if reverse:
        delta = -delta
    delta = delta.quantize(_CENT)
    # Drop a stale in-memory balance before the statement. Autoflush would
    # otherwise write that Python value and the increment would apply to it.
    # ``evaluate`` cannot compile ``column + delta`` back onto the object.
    _expire_fund_balance(db, sinking_fund_id)
    db.execute(
        update(SinkingFund)
        .where(SinkingFund.id == sinking_fund_id)
        .values(current_balance=SinkingFund.current_balance + delta)
        .execution_options(synchronize_session=False)
    )
    _expire_fund_balance(db, sinking_fund_id)


def adjust_budget_fund_balance(
    db: Session,
    budget_id: int | None,
    transaction_type: str,
    amount: float,
    reverse: bool = False,
) -> None:
    """Adjust a budget's fund_balance for a budget_transfer transaction.

    Only acts when transaction_type is ``budget_transfer`` and budget_id is set.
    A budget_transfer moves money into the budget, so fund_balance increases
    when the transaction is applied and decreases when it is reversed.
    Does not change spent_amount.
    """
    if transaction_type != "budget_transfer" or budget_id is None:
        return
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if budget is None:
        return
    delta = Decimal(str(amount))
    if reverse:
        delta = -delta
    budget.fund_balance = float(
        Decimal(str(budget.fund_balance)).quantize(Decimal("0.01")) + delta
    )


def _counts_as_budget_spend(
    txn_type: str, transaction_type: str, budget_id: int | None
) -> bool:
    """True when this snapshot belongs in ``Budget.spent_amount``.

    Matches quick-expense creation: an expense classified as
    ``budget_expense`` with a budget link. Income, transfers, contribution
    refunds, and ``budget_transfer`` do not count. A budget transfer moves
    ``fund_balance`` instead.
    """
    return (
        budget_id is not None
        and txn_type == "expense"
        and transaction_type == "budget_expense"
    )


def _write_spent(budget: Budget, value: Decimal) -> None:
    """Store spent_amount in cents, never below zero.

    The budget schema rejects a negative spent_amount, so a delete or edit
    that would cross zero is clamped to 0.00. Quantizing a tiny negative
    remainder can yield -0.00; that is stored as 0.00 as well.
    """
    quantized = value.quantize(_CENT)
    if quantized <= 0:
        quantized = Decimal("0.00")
    budget.spent_amount = float(quantized)


def adjust_budget_spent_amount(
    db: Session,
    budget_id: int | None,
    txn_type: str,
    transaction_type: str,
    amount: float | Decimal,
    reverse: bool = False,
) -> None:
    """Add or remove a budget expense from ``Budget.spent_amount``.

    No-op unless the snapshot is an expense, ``transaction_type`` is
    ``budget_expense``, and ``budget_id`` is set. Pass reverse=True to undo
    a previously applied expense. A missing budget id, or an id that does
    not match a row, is a no-op. Does not change ``fund_balance``.
    """
    if not _counts_as_budget_spend(txn_type, transaction_type, budget_id):
        return
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if budget is None:
        return
    delta = Decimal(str(amount))
    if reverse:
        delta = -delta
    _write_spent(budget, Decimal(str(budget.spent_amount)) + delta)


def _apply_fund_balance_effects(
    db: Session,
    *,
    sinking_fund_id: int | None,
    txn_type: str,
    amount: float,
    budget_id: int | None,
    transaction_type: str,
    reverse: bool = False,
) -> None:
    """Apply or reverse sinking-fund and budget fund_balance effects."""
    adjust_sinking_fund_balance(db, sinking_fund_id, txn_type, amount, reverse=reverse)
    adjust_budget_fund_balance(db, budget_id, transaction_type, amount, reverse=reverse)


def _replace_budget_spent(
    db: Session,
    *,
    old_type: str,
    old_amount: float,
    old_budget_id: int | None,
    old_transaction_type: str,
    new_type: str,
    new_amount: float,
    new_budget_id: int | None,
    new_transaction_type: str,
) -> None:
    """Move spent_amount from the previous snapshot to the updated one.

    When both snapshots count toward the same budget, the net change is
    applied once and then clamped. An amount edit therefore does not clamp
    at zero in the middle and then add the new amount back.
    """
    old_on = _counts_as_budget_spend(old_type, old_transaction_type, old_budget_id)
    new_on = _counts_as_budget_spend(new_type, new_transaction_type, new_budget_id)
    if old_budget_id == new_budget_id:
        delta = Decimal(0)
        if old_on:
            delta -= Decimal(str(old_amount))
        if new_on:
            delta += Decimal(str(new_amount))
        if delta == 0 or new_budget_id is None:
            return
        budget = db.query(Budget).filter(Budget.id == new_budget_id).first()
        if budget is None:
            return
        _write_spent(budget, Decimal(str(budget.spent_amount)) + delta)
        return
    if old_on:
        adjust_budget_spent_amount(
            db,
            old_budget_id,
            old_type,
            old_transaction_type,
            old_amount,
            reverse=True,
        )
    if new_on:
        adjust_budget_spent_amount(
            db,
            new_budget_id,
            new_type,
            new_transaction_type,
            new_amount,
        )


def apply_transaction_balance_effects(
    db: Session,
    *,
    sinking_fund_id: int | None,
    txn_type: str,
    amount: float,
    budget_id: int | None,
    transaction_type: str,
    reverse: bool = False,
) -> None:
    """Apply or reverse one transaction snapshot's fund and spent effects."""
    _apply_fund_balance_effects(
        db,
        sinking_fund_id=sinking_fund_id,
        txn_type=txn_type,
        amount=amount,
        budget_id=budget_id,
        transaction_type=transaction_type,
        reverse=reverse,
    )
    adjust_budget_spent_amount(
        db,
        budget_id,
        txn_type,
        transaction_type,
        amount,
        reverse=reverse,
    )


def replace_transaction_balance_effects(
    db: Session,
    *,
    old_sinking_fund_id: int | None,
    old_type: str,
    old_amount: float,
    old_budget_id: int | None,
    old_transaction_type: str,
    new_sinking_fund_id: int | None,
    new_type: str,
    new_amount: float,
    new_budget_id: int | None,
    new_transaction_type: str,
) -> None:
    """Reverse the previous snapshot, then apply the updated one.

    Covers changes of amount, type, sinking fund (including a move between
    funds or to and from no fund), budget link, and whether the row counts
    toward spent_amount. Spent on one budget is adjusted by the net change.
    """
    _apply_fund_balance_effects(
        db,
        sinking_fund_id=old_sinking_fund_id,
        txn_type=old_type,
        amount=old_amount,
        budget_id=old_budget_id,
        transaction_type=old_transaction_type,
        reverse=True,
    )
    _apply_fund_balance_effects(
        db,
        sinking_fund_id=new_sinking_fund_id,
        txn_type=new_type,
        amount=new_amount,
        budget_id=new_budget_id,
        transaction_type=new_transaction_type,
    )
    _replace_budget_spent(
        db,
        old_type=old_type,
        old_amount=old_amount,
        old_budget_id=old_budget_id,
        old_transaction_type=old_transaction_type,
        new_type=new_type,
        new_amount=new_amount,
        new_budget_id=new_budget_id,
        new_transaction_type=new_transaction_type,
    )


def _reverse_and_delete(db: Session, txn: Transaction) -> None:
    apply_transaction_balance_effects(
        db,
        sinking_fund_id=txn.sinking_fund_id,
        txn_type=txn.type,
        amount=float(txn.amount),
        budget_id=txn.budget_id,
        transaction_type=txn.transaction_type,
        reverse=True,
    )
    db.delete(txn)


def _linked_partner(db: Session, txn: Transaction) -> Transaction | None:
    """The other row of a recorded pair, or None when it is already gone."""
    if txn.occurrence_due_date is not None:
        return (
            db.query(Transaction)
            .filter(Transaction.adjustment_for_transaction_id == txn.id)
            .one_or_none()
        )
    if txn.adjustment_for_transaction_id is None:
        return None
    return db.get(Transaction, txn.adjustment_for_transaction_id)


def delete_transaction_row(db: Session, txn: Transaction) -> None:
    """Reverse and delete one transaction, or a linked payment and clawback.

    Deleting either row of a linked pair reverses both and deletes the
    clawback first. A missing partner is a one-row reverse, not an error.
    Rows with both new columns null, including old payments and one-off
    Bills fund expenses, stay a one-row delete. The bill cursor is not moved.
    """
    if not is_recorded_bill_payment(txn):
        _reverse_and_delete(db, txn)
        return
    partner = _linked_partner(db, txn)
    if partner is None:
        _reverse_and_delete(db, txn)
        return
    clawback = txn if txn.adjustment_for_transaction_id is not None else partner
    payment = partner if clawback is txn else txn
    _reverse_and_delete(db, clawback)
    _reverse_and_delete(db, payment)
