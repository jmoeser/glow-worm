"""Balance effects of a household transaction.

The transactions HTML routes, the JSON API, and the MCP transaction tools
all call these helpers so a sinking fund's stored balance stays aligned
with its ledger.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Budget, SinkingFund


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
    fund = db.query(SinkingFund).filter(SinkingFund.id == sinking_fund_id).first()
    if fund is None:
        return
    delta = (
        Decimal(str(amount))
        if txn_type in ("income", "transfer")
        else -Decimal(str(amount))
    )
    if reverse:
        delta = -delta
    fund.current_balance = float(
        Decimal(str(fund.current_balance)).quantize(Decimal("0.01")) + delta
    )


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
    """Apply or reverse one transaction snapshot's fund and budget effects."""
    adjust_sinking_fund_balance(db, sinking_fund_id, txn_type, amount, reverse=reverse)
    adjust_budget_fund_balance(db, budget_id, transaction_type, amount, reverse=reverse)


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
    funds or to and from no fund), and budget link.
    """
    apply_transaction_balance_effects(
        db,
        sinking_fund_id=old_sinking_fund_id,
        txn_type=old_type,
        amount=old_amount,
        budget_id=old_budget_id,
        transaction_type=old_transaction_type,
        reverse=True,
    )
    apply_transaction_balance_effects(
        db,
        sinking_fund_id=new_sinking_fund_id,
        txn_type=new_type,
        amount=new_amount,
        budget_id=new_budget_id,
        transaction_type=new_transaction_type,
    )
