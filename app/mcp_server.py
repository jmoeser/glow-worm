"""MCP server exposing budget tool operations via FastMCP.

Tools are scoped to transactions and recurring bills CRUD only.
Authentication is handled at the HTTP middleware layer (Bearer token).
"""

import logging
from decimal import Decimal, InvalidOperation

from fastmcp import FastMCP

from app.database import SessionLocal
from app.middleware import get_current_user_context
from app.models import Category, RecurringBill, SinkingFund, Transaction
from app.schemas import (
    RecurringBillCreate,
    RecurringBillResponse,
    RecurringBillUpdate,
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
)

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="glow-worm",
    instructions=(
        "Glow-worm is a household budgeting app. Use these tools to manage "
        "transactions and recurring bills. Transactions track individual income "
        "and expense events. Recurring bills track repeating charges like rent, "
        "utilities, and subscriptions."
    ),
)


def _audit_username() -> str:
    """Return the username of the authenticated user, or 'unknown'."""
    user = get_current_user_context()
    return user.username if user else "unknown"


# ---------------------------------------------------------------------------
# Transaction tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_transactions(
    month: int | None = None,
    year: int | None = None,
    type_filter: str | None = None,
    category_id: int | None = None,
) -> list[dict]:
    """List transactions for a given month and year.

    Args:
        month: Month number (1-12). Defaults to current month.
        year: Four-digit year. Defaults to current year.
        type_filter: Filter by 'income' or 'expense'.
        category_id: Filter by category ID.

    Returns:
        List of transaction objects with id, date, description, amount,
        category_id, type, transaction_type, and linked entity IDs.
    """
    import calendar
    from datetime import datetime

    import pytz

    if month is None or year is None:
        now = datetime.now(pytz.timezone("Australia/Brisbane"))
        month = month or now.month
        year = year or now.year

    start = f"{year:04d}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    end = f"{year:04d}-{month:02d}-{last_day:02d}"

    db = SessionLocal()
    try:
        query = db.query(Transaction).filter(
            Transaction.date >= start, Transaction.date <= end
        )
        if type_filter:
            query = query.filter(Transaction.type == type_filter)
        if category_id:
            query = query.filter(Transaction.category_id == category_id)

        txns = query.order_by(Transaction.date.desc(), Transaction.id.desc()).all()
        return [
            TransactionResponse.model_validate(t).model_dump(mode="json")
            for t in txns
        ]
    finally:
        db.close()


@mcp.tool()
def get_transaction(transaction_id: int) -> dict | str:
    """Get a single transaction by its ID.

    Args:
        transaction_id: The unique ID of the transaction.

    Returns:
        The transaction object, or an error message if not found.
    """
    db = SessionLocal()
    try:
        txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not txn:
            return f"Transaction {transaction_id} not found."
        return TransactionResponse.model_validate(txn).model_dump(mode="json")
    finally:
        db.close()


@mcp.tool()
def create_transaction(
    date: str,
    amount: float,
    category_id: int,
    type: str,
    description: str | None = None,
    transaction_type: str = "regular",
    sinking_fund_id: int | None = None,
    recurring_bill_id: int | None = None,
    budget_id: int | None = None,
    is_paid: bool = True,
) -> dict | str:
    """Create a new transaction.

    Args:
        date: Date in YYYY-MM-DD format.
        amount: Positive monetary amount (e.g. 150.00).
        category_id: ID of the category for this transaction.
        type: Either 'income' or 'expense'.
        description: Optional description text.
        transaction_type: One of: regular, income, income_allocation,
            contribution, withdrawal, budget_expense, budget_transfer.
            Defaults to 'regular'.
        sinking_fund_id: Optional linked sinking fund ID.
        recurring_bill_id: Optional linked recurring bill ID.
        budget_id: Optional linked budget ID.
        is_paid: Whether the transaction is paid. Defaults to True.

    Returns:
        The created transaction object, or an error message.
    """
    try:
        data = TransactionCreate(
            date=date,
            description=description,
            amount=Decimal(str(amount)),
            category_id=category_id,
            type=type,
            transaction_type=transaction_type,
            sinking_fund_id=sinking_fund_id,
            recurring_bill_id=recurring_bill_id,
            budget_id=budget_id,
            is_paid=is_paid,
        )
    except Exception as exc:
        return f"Validation error: {exc}"

    db = SessionLocal()
    try:
        # Verify category exists
        cat = db.query(Category).filter(Category.id == category_id).first()
        if not cat:
            return f"Category {category_id} not found."

        txn = Transaction(
            date=data.date,
            description=data.description,
            amount=float(data.amount),
            category_id=data.category_id,
            type=data.type.value,
            transaction_type=data.transaction_type.value,
            sinking_fund_id=data.sinking_fund_id,
            recurring_bill_id=data.recurring_bill_id,
            budget_id=data.budget_id,
            is_paid=data.is_paid,
        )
        db.add(txn)
        db.commit()
        db.refresh(txn)

        logger.info("MCP [user=%s]: created transaction id=%d", _audit_username(), txn.id)
        return TransactionResponse.model_validate(txn).model_dump(mode="json")
    except Exception as exc:
        db.rollback()
        return f"Error creating transaction: {exc}"
    finally:
        db.close()


@mcp.tool()
def update_transaction(
    transaction_id: int,
    date: str | None = None,
    amount: float | None = None,
    category_id: int | None = None,
    type: str | None = None,
    description: str | None = None,
    transaction_type: str | None = None,
    sinking_fund_id: int | None = None,
    recurring_bill_id: int | None = None,
    budget_id: int | None = None,
    is_paid: bool | None = None,
) -> dict | str:
    """Update an existing transaction.

    Only the provided fields will be updated; omitted fields remain unchanged.

    Args:
        transaction_id: The ID of the transaction to update.
        date: New date in YYYY-MM-DD format.
        amount: New positive monetary amount.
        category_id: New category ID.
        type: New type ('income' or 'expense').
        description: New description text.
        transaction_type: New transaction type.
        sinking_fund_id: New linked sinking fund ID.
        recurring_bill_id: New linked recurring bill ID.
        budget_id: New linked budget ID.
        is_paid: New paid status.

    Returns:
        The updated transaction object, or an error message.
    """
    update_data = {}
    if date is not None:
        update_data["date"] = date
    if amount is not None:
        update_data["amount"] = Decimal(str(amount))
    if category_id is not None:
        update_data["category_id"] = category_id
    if type is not None:
        update_data["type"] = type
    if description is not None:
        update_data["description"] = description
    if transaction_type is not None:
        update_data["transaction_type"] = transaction_type
    if sinking_fund_id is not None:
        update_data["sinking_fund_id"] = sinking_fund_id
    if recurring_bill_id is not None:
        update_data["recurring_bill_id"] = recurring_bill_id
    if budget_id is not None:
        update_data["budget_id"] = budget_id
    if is_paid is not None:
        update_data["is_paid"] = is_paid

    try:
        data = TransactionUpdate(**update_data)
    except Exception as exc:
        return f"Validation error: {exc}"

    db = SessionLocal()
    try:
        txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not txn:
            return f"Transaction {transaction_id} not found."

        for field, value in data.model_dump(exclude_unset=True).items():
            if field in ("type", "transaction_type") and value is not None:
                setattr(txn, field, value.value if hasattr(value, "value") else value)
            else:
                setattr(txn, field, value)

        db.commit()
        db.refresh(txn)

        logger.info("MCP [user=%s]: updated transaction id=%d", _audit_username(), txn.id)
        return TransactionResponse.model_validate(txn).model_dump(mode="json")
    except Exception as exc:
        db.rollback()
        return f"Error updating transaction: {exc}"
    finally:
        db.close()


@mcp.tool()
def delete_transaction(transaction_id: int) -> str:
    """Delete a transaction permanently.

    Args:
        transaction_id: The ID of the transaction to delete.

    Returns:
        A confirmation message or error.
    """
    db = SessionLocal()
    try:
        txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not txn:
            return f"Transaction {transaction_id} not found."

        db.delete(txn)
        db.commit()

        logger.info("MCP [user=%s]: deleted transaction id=%d", _audit_username(), transaction_id)
        return f"Transaction {transaction_id} deleted."
    except Exception as exc:
        db.rollback()
        return f"Error deleting transaction: {exc}"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Recurring bill tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_bills(include_inactive: bool = False) -> list[dict]:
    """List recurring bills.

    Args:
        include_inactive: If True, include deactivated bills. Defaults to False.

    Returns:
        List of recurring bill objects with id, name, amount, frequency,
        next_due_date, and other details.
    """
    db = SessionLocal()
    try:
        query = db.query(RecurringBill)
        if not include_inactive:
            query = query.filter(RecurringBill.is_active == True)  # noqa: E712
        bills = query.order_by(RecurringBill.next_due_date).all()
        return [
            RecurringBillResponse.model_validate(b).model_dump(mode="json")
            for b in bills
        ]
    finally:
        db.close()


@mcp.tool()
def get_bill(bill_id: int) -> dict | str:
    """Get a single recurring bill by its ID.

    Args:
        bill_id: The unique ID of the recurring bill.

    Returns:
        The bill object, or an error message if not found.
    """
    db = SessionLocal()
    try:
        bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
        if not bill:
            return f"Bill {bill_id} not found."
        return RecurringBillResponse.model_validate(bill).model_dump(mode="json")
    finally:
        db.close()


@mcp.tool()
def create_bill(
    name: str,
    amount: float,
    debtor_provider: str,
    start_date: str,
    frequency: str,
    category_id: int,
    next_due_date: str,
    end_date: str | None = None,
    is_active: bool = True,
) -> dict | str:
    """Create a new recurring bill.

    Args:
        name: Name of the bill (e.g. 'Rent', 'Netflix').
        amount: Positive monetary amount per occurrence.
        debtor_provider: Who the bill is paid to.
        start_date: When the bill started, in YYYY-MM-DD format.
        frequency: One of: '28_days', 'monthly', 'quarterly', 'yearly'.
        category_id: ID of the expense category.
        next_due_date: Next payment date in YYYY-MM-DD format.
        end_date: Optional end date in YYYY-MM-DD format.
        is_active: Whether the bill is active. Defaults to True.

    Returns:
        The created bill object, or an error message.
    """
    try:
        data = RecurringBillCreate(
            name=name,
            amount=Decimal(str(amount)),
            debtor_provider=debtor_provider,
            start_date=start_date,
            frequency=frequency,
            category_id=category_id,
            next_due_date=next_due_date,
            end_date=end_date,
            is_active=is_active,
        )
    except Exception as exc:
        return f"Validation error: {exc}"

    db = SessionLocal()
    try:
        cat = db.query(Category).filter(Category.id == category_id).first()
        if not cat:
            return f"Category {category_id} not found."

        bill = RecurringBill(
            name=data.name,
            amount=float(data.amount),
            debtor_provider=data.debtor_provider,
            start_date=data.start_date,
            frequency=data.frequency.value,
            category_id=data.category_id,
            next_due_date=data.next_due_date,
            end_date=data.end_date,
            is_active=data.is_active,
        )
        db.add(bill)
        db.commit()
        db.refresh(bill)

        logger.info("MCP [user=%s]: created bill id=%d name=%s", _audit_username(), bill.id, bill.name)
        return RecurringBillResponse.model_validate(bill).model_dump(mode="json")
    except Exception as exc:
        db.rollback()
        return f"Error creating bill: {exc}"
    finally:
        db.close()


@mcp.tool()
def update_bill(
    bill_id: int,
    name: str | None = None,
    amount: float | None = None,
    debtor_provider: str | None = None,
    frequency: str | None = None,
    end_date: str | None = None,
    is_active: bool | None = None,
    next_due_date: str | None = None,
) -> dict | str:
    """Update an existing recurring bill.

    Only the provided fields will be updated; omitted fields remain unchanged.

    Args:
        bill_id: The ID of the bill to update.
        name: New name for the bill.
        amount: New monetary amount per occurrence.
        debtor_provider: New provider name.
        frequency: New frequency ('28_days', 'monthly', 'quarterly', 'yearly').
        end_date: New end date in YYYY-MM-DD format.
        is_active: New active status.
        next_due_date: New next due date in YYYY-MM-DD format.

    Returns:
        The updated bill object, or an error message.
    """
    update_data = {}
    if name is not None:
        update_data["name"] = name
    if amount is not None:
        update_data["amount"] = Decimal(str(amount))
    if debtor_provider is not None:
        update_data["debtor_provider"] = debtor_provider
    if frequency is not None:
        update_data["frequency"] = frequency
    if end_date is not None:
        update_data["end_date"] = end_date
    if is_active is not None:
        update_data["is_active"] = is_active
    if next_due_date is not None:
        update_data["next_due_date"] = next_due_date

    try:
        data = RecurringBillUpdate(**update_data)
    except Exception as exc:
        return f"Validation error: {exc}"

    db = SessionLocal()
    try:
        bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
        if not bill:
            return f"Bill {bill_id} not found."

        for field, value in data.model_dump(exclude_unset=True).items():
            if field == "frequency" and value is not None:
                setattr(bill, field, value.value if hasattr(value, "value") else value)
            else:
                setattr(bill, field, value)

        db.commit()
        db.refresh(bill)

        logger.info("MCP [user=%s]: updated bill id=%d", _audit_username(), bill.id)
        return RecurringBillResponse.model_validate(bill).model_dump(mode="json")
    except Exception as exc:
        db.rollback()
        return f"Error updating bill: {exc}"
    finally:
        db.close()


@mcp.tool()
def delete_bill(bill_id: int) -> str:
    """Deactivate a recurring bill (soft delete).

    The bill is not permanently removed; it is marked as inactive to
    preserve transaction history.

    Args:
        bill_id: The ID of the bill to deactivate.

    Returns:
        A confirmation message or error.
    """
    db = SessionLocal()
    try:
        bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
        if not bill:
            return f"Bill {bill_id} not found."

        bill.is_active = False
        db.commit()

        logger.info("MCP [user=%s]: deactivated bill id=%d", _audit_username(), bill_id)
        return f"Bill {bill_id} ('{bill.name}') deactivated."
    except Exception as exc:
        db.rollback()
        return f"Error deactivating bill: {exc}"
    finally:
        db.close()
