"""MCP server exposing budget tool operations via FastMCP.

Tools cover transactions, recurring bills, and payment methods.
Authentication is handled at the HTTP middleware layer (Bearer token).
"""

import logging
import warnings
from decimal import Decimal
from typing import Any

from fastmcp import FastMCP
from pydantic import ValidationError
from pydantic.json_schema import PydanticJsonSchemaWarning

from app.audit import (
    audit_write,
    column_changes,
    emit_mcp,
    get_current_actor,
    related_rows,
    snapshot_columns,
    summary_for,
)
from app.config import TIMEZONE
from app.database import SessionLocal
from app.middleware import get_current_user_context
from app.models import Category, PaymentMethod, RecurringBill, Transaction
from app.schemas import (
    PaymentMethodCreate,
    PaymentMethodUpdate,
    RecurringBillCreate,
    RecurringBillUpdate,
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
    UnknownFrequencyError,
    public_validation_errors,
)
from app.services.payment_methods import (
    PaymentMethodError,
    apply_payment_method,
    archive_method,
    assign_payment_method,
    merge_payment_method,
    prepare_payment_method,
    replacement_payload,
    serialize_payment_method,
    unarchive_method,
)
from app.services.recurring_payments import (
    PaidFromError,
    PaymentBlocked,
    PaymentConflict,
    apply_paid_from,
    confirm_transaction_payload,
    due_to_confirm,
    serialize_bill,
    update_bill_fields,
)
from app.services.recurring_payments import (
    confirm_payment as confirm_payment_service,
)
from app.services.recurring_payments import (
    skip_payment as skip_payment_service,
)
from app.services.transaction_balances import (
    RecordedPaymentLocked,
    apply_transaction_balance_effects,
    assert_recorded_payment_unchanged,
    delete_transaction_row,
    replace_transaction_balance_effects,
)

logger = logging.getLogger(__name__)


class _Omit:
    """Sentinel so an omitted MCP id stays unchanged and null clears the link.

    The default is not JSON-serializable, so FastMCP leaves it out of the tool
    schema. Clients omit the argument to keep the current value, or pass null
    to clear it.
    """


_OMIT = _Omit()

mcp = FastMCP(
    name="glow-worm",
    instructions=(
        "Glow-worm is a household budgeting app. Use these tools to manage "
        "transactions, recurring bills, and payment methods. Transactions track "
        "individual income and expense events. Recurring bills track repeating "
        "charges like rent, utilities, and subscriptions. Payment methods record "
        "which card or account pays a bill. Only the last 4 digits are stored. "
        "Bills paid from the Bills fund are logged automatically. Payments from "
        "a budget or sinking fund, and variable Bills-fund bills, wait until "
        "you confirm or skip each one."
    ),
)


def _audit_username() -> str:
    """Return the username of the authenticated user, or 'unknown'.

    Prefer the frozen actor. Bearer auth commits and closes its session
    before the tool runs, so the ORM user is expired.
    """
    actor = get_current_actor()
    if actor is not None:
        return actor.username or "unknown"
    user = get_current_user_context()
    if user is None:
        return "unknown"
    return user.username or "unknown"


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

    if month is None or year is None:
        now = datetime.now(TIMEZONE)
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
            TransactionResponse.model_validate(t).model_dump(mode="json") for t in txns
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
@audit_write
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
    except (ValidationError, ValueError, TypeError) as exc:
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
        apply_transaction_balance_effects(
            db,
            sinking_fund_id=txn.sinking_fund_id,
            txn_type=txn.type,
            amount=float(data.amount),
            budget_id=txn.budget_id,
            transaction_type=txn.transaction_type,
        )
        db.commit()
        db.refresh(txn)
        emit_mcp(
            action="create",
            entity_type="transaction",
            entity_id=txn.id,
            endpoint="create_transaction",
            values=summary_for("transaction", snapshot_columns(txn)),
        )

        logger.info(
            "MCP [user=%s]: created transaction id=%d", _audit_username(), txn.id
        )
        return TransactionResponse.model_validate(txn).model_dump(mode="json")
    except Exception:
        logger.exception("MCP [user=%s]: error creating transaction", _audit_username())
        db.rollback()
        return "Internal error creating transaction."
    finally:
        db.close()


# _OMIT is not JSON-serializable, so the tool schema omits that default.
# Silence the resulting pydantic warning; null still clears the link.
warnings.filterwarnings(
    "ignore",
    category=PydanticJsonSchemaWarning,
    message=r"Default value .* is not JSON serializable",
)


@mcp.tool()
@audit_write
def update_transaction(
    transaction_id: int,
    date: str | None = None,
    amount: float | None = None,
    category_id: int | None = None,
    type: str | None = None,
    description: str | None = None,
    transaction_type: str | None = None,
    sinking_fund_id: int | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    recurring_bill_id: int | None = None,
    budget_id: int | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    is_paid: bool | None = None,
) -> dict | str:
    """Update an existing transaction.

    Only the provided fields will be updated; omitted fields remain unchanged.
    Pass null for sinking_fund_id or budget_id to clear that link.

    Args:
        transaction_id: The ID of the transaction to update.
        date: New date in YYYY-MM-DD format.
        amount: New positive monetary amount.
        category_id: New category ID.
        type: New type ('income', 'expense', or 'transfer').
        description: New description text.
        transaction_type: New transaction type.
        sinking_fund_id: New linked sinking fund ID. Null clears the link.
        recurring_bill_id: New linked recurring bill ID.
        budget_id: New linked budget ID. Null clears the link.
        is_paid: New paid status.

    Returns:
        The updated transaction object, or an error message.
    """
    update_data: dict[str, Any] = {}
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
    if sinking_fund_id is not _OMIT:
        update_data["sinking_fund_id"] = sinking_fund_id
    if recurring_bill_id is not None:
        update_data["recurring_bill_id"] = recurring_bill_id
    if budget_id is not _OMIT:
        update_data["budget_id"] = budget_id
    if is_paid is not None:
        update_data["is_paid"] = is_paid

    try:
        data = TransactionUpdate(**update_data)
    except (ValidationError, ValueError, TypeError) as exc:
        return f"Validation error: {exc}"

    db = SessionLocal()
    try:
        txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not txn:
            return f"Transaction {transaction_id} not found."

        old_sf_id = txn.sinking_fund_id
        old_type = txn.type
        old_txn_type = txn.transaction_type
        old_budget_id = txn.budget_id
        old_amount = txn.amount
        before = snapshot_columns(txn)

        changes = data.model_dump(exclude_unset=True)
        assert_recorded_payment_unchanged(txn, changes)

        for field, value in changes.items():
            if field in ("type", "transaction_type") and value is not None:
                setattr(txn, field, value.value if hasattr(value, "value") else value)
            else:
                setattr(txn, field, value)

        replace_transaction_balance_effects(
            db,
            old_sinking_fund_id=old_sf_id,
            old_type=old_type,
            old_amount=old_amount,
            old_budget_id=old_budget_id,
            old_transaction_type=old_txn_type,
            new_sinking_fund_id=txn.sinking_fund_id,
            new_type=txn.type,
            new_amount=txn.amount,
            new_budget_id=txn.budget_id,
            new_transaction_type=txn.transaction_type,
        )
        db.commit()
        db.refresh(txn)
        emit_mcp(
            action="update",
            entity_type="transaction",
            entity_id=txn.id,
            endpoint="update_transaction",
            changes=column_changes(before, snapshot_columns(txn)),
        )

        logger.info(
            "MCP [user=%s]: updated transaction id=%d", _audit_username(), txn.id
        )
        return TransactionResponse.model_validate(txn).model_dump(mode="json")
    except RecordedPaymentLocked as exc:
        db.rollback()
        return exc.detail
    except Exception:
        logger.exception(
            "MCP [user=%s]: error updating transaction id=%d",
            _audit_username(),
            transaction_id,
        )
        db.rollback()
        return "Internal error updating transaction."
    finally:
        db.close()


@mcp.tool()
@audit_write
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

        values = summary_for("transaction", snapshot_columns(txn))
        deleted_ids = delete_transaction_row(db, txn)
        db.commit()
        emit_mcp(
            action="delete",
            entity_type="transaction",
            entity_id=transaction_id,
            endpoint="delete_transaction",
            values=values,
            related=related_rows(
                "transaction",
                [
                    deleted_id
                    for deleted_id in deleted_ids
                    if deleted_id != transaction_id
                ],
                "delete",
            ),
        )

        logger.info(
            "MCP [user=%s]: deleted transaction id=%d",
            _audit_username(),
            transaction_id,
        )
        return f"Transaction {transaction_id} deleted."
    except Exception:
        logger.exception(
            "MCP [user=%s]: error deleting transaction id=%d",
            _audit_username(),
            transaction_id,
        )
        db.rollback()
        return "Internal error deleting transaction."
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
            query = query.filter(RecurringBill.is_active == True)
        bills = query.order_by(RecurringBill.next_due_date).all()
        return [serialize_bill(b) for b in bills]
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
        return serialize_bill(bill)
    finally:
        db.close()


@mcp.tool()
@audit_write
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
    payment_method_id: int | None = None,
    paid_from: str = "bills_fund",
    paid_from_fund_id: int | None = None,
    bill_type: str | None = None,
    importance: str | None = None,
    cancel_note: str | None = None,
    if_stopped_note: str | None = None,
) -> dict | str:
    """Create a new recurring bill.

    Args:
        name: Name of the bill (e.g. 'Rent', 'Netflix').
        amount: Positive monetary amount per occurrence.
        debtor_provider: Who the bill is paid to.
        start_date: When the bill started, in YYYY-MM-DD format.
        frequency: One of: 'weekly', 'fortnightly', '28_days', 'monthly',
            'quarterly', 'yearly', 'every_2_years'.
        category_id: ID of the expense category. For a budget payment this
            is the budget category.
        next_due_date: Next payment date in YYYY-MM-DD format.
        end_date: Optional end date in YYYY-MM-DD format.
        is_active: Whether the bill is active. Defaults to True.
        payment_method_id: Optional payment method id. Null leaves it unset.
        paid_from: 'bills_fund' (default), 'budget', or 'sinking_fund'.
        paid_from_fund_id: Sinking fund id when paid_from is 'sinking_fund'.
        bill_type: 'fixed' or 'variable'. Omit to keep the default, fixed.
            Only Bills-fund rows use it. Others always wait for confirm.
        importance: 'essential' (keep paying) or 'optional' (safe to cancel).
            Omit to leave it unset.
        cancel_note: How to cancel. Blank becomes null.
        if_stopped_note: What happens if this stops. Blank becomes null.

    Returns:
        The created bill object, or an error message.
    """
    try:
        create_kwargs: dict[str, Any] = {}
        if bill_type is not None:
            create_kwargs["bill_type"] = bill_type
        if importance is not None:
            create_kwargs["importance"] = importance
        if cancel_note is not None:
            create_kwargs["cancel_note"] = cancel_note
        if if_stopped_note is not None:
            create_kwargs["if_stopped_note"] = if_stopped_note
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
            payment_method_id=payment_method_id,
            paid_from=paid_from,
            paid_from_fund_id=paid_from_fund_id,
            **create_kwargs,
        )
    except ValidationError as exc:
        return _payment_validation(exc)
    except (ValueError, TypeError) as exc:
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
            bill_type=data.bill_type.value,
            importance=data.importance.value if data.importance is not None else None,
            cancel_note=data.cancel_note,
            if_stopped_note=data.if_stopped_note,
        )
        apply_paid_from(
            db,
            bill,
            paid_from=data.paid_from.value,
            paid_from_fund_id=data.paid_from_fund_id,
            category_id=data.category_id,
            force=True,
        )
        db.add(bill)
        db.flush()
        if data.payment_method_id is not None:
            assign_payment_method(db, bill, data.payment_method_id)
        db.commit()
        db.refresh(bill)
        emit_mcp(
            action="create",
            entity_type="bill",
            entity_id=bill.id,
            endpoint="create_bill",
            values=summary_for("bill", snapshot_columns(bill)),
        )

        logger.info(
            "MCP [user=%s]: created bill id=%d name=%s",
            _audit_username(),
            bill.id,
            bill.name,
        )
        return serialize_bill(bill)
    except (PaymentMethodError, PaidFromError) as exc:
        db.rollback()
        return exc.message
    except Exception:
        logger.exception("MCP [user=%s]: error creating bill", _audit_username())
        db.rollback()
        return "Internal error creating bill."
    finally:
        db.close()


@mcp.tool()
@audit_write
def update_bill(
    bill_id: int,
    name: str | None = None,
    amount: float | None = None,
    debtor_provider: str | None = None,
    frequency: str | None = None,
    end_date: str | None = None,
    is_active: bool | None = None,
    next_due_date: str | None = None,
    payment_method_id: int | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    paid_from: str | None = None,
    paid_from_fund_id: int | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    bill_type: str | None = None,
    category_id: int | None = None,
    expected_next_due_date: str | None = None,
    importance: str | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    cancel_note: str | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    if_stopped_note: str | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
) -> dict | str:
    """Update an existing recurring bill.

    Only the provided fields will be updated; omitted fields remain unchanged.
    Sending ``next_due_date`` requires ``expected_next_due_date``, the date
    last read. Omitting the date leaves the cursor alone.

    Args:
        bill_id: The ID of the bill to update.
        name: New name for the bill.
        amount: New monetary amount per occurrence.
        debtor_provider: New provider name.
        frequency: New frequency ('weekly', 'fortnightly', '28_days',
            'monthly', 'quarterly', 'yearly', 'every_2_years').
        end_date: New end date in YYYY-MM-DD format.
        is_active: New active status.
        next_due_date: New next due date in YYYY-MM-DD format.
        payment_method_id: New payment method id. Null clears the link.
        paid_from: 'bills_fund', 'budget', or 'sinking_fund'.
        paid_from_fund_id: Sinking fund id. Null clears it when leaving a fund.
        bill_type: 'fixed' or 'variable'. Stored for every row; only Bills-fund
            rows use it to decide automatic payment.
        category_id: Expense category. For a budget payment this is the budget.
        expected_next_due_date: The next due date you last read. Required
            when next_due_date is sent.
        importance: 'essential', 'optional', or null to clear. Omit to leave it.
        cancel_note: How to cancel. Null or a blank string clears it.
        if_stopped_note: What happens if this stops. Null or a blank string
            clears it.

    Returns:
        The updated bill object, or an error message.
    """
    update_data: dict[str, Any] = {}
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
    if expected_next_due_date is not None:
        update_data["expected_next_due_date"] = expected_next_due_date
    if payment_method_id is not _OMIT:
        update_data["payment_method_id"] = payment_method_id
    if paid_from is not None:
        update_data["paid_from"] = paid_from
    if paid_from_fund_id is not _OMIT:
        update_data["paid_from_fund_id"] = paid_from_fund_id
    if bill_type is not None:
        update_data["bill_type"] = bill_type
    if category_id is not None:
        update_data["category_id"] = category_id
    if importance is not _OMIT:
        update_data["importance"] = importance
    if cancel_note is not _OMIT:
        update_data["cancel_note"] = cancel_note
    if if_stopped_note is not _OMIT:
        update_data["if_stopped_note"] = if_stopped_note

    try:
        data = RecurringBillUpdate(**update_data)
    except ValidationError as exc:
        return _payment_validation(exc)
    except (ValueError, TypeError) as exc:
        return f"Validation error: {exc}"

    db = SessionLocal()
    try:
        bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
        if not bill:
            return f"Bill {bill_id} not found."

        before = snapshot_columns(bill)
        updates = data.model_dump(exclude_unset=True)
        payment_set = "payment_method_id" in updates
        payment_id = updates.pop("payment_method_id", None)
        if payment_set:
            assign_payment_method(db, bill, payment_id)
        update_bill_fields(db, bill, data)

        db.commit()
        db.refresh(bill)
        emit_mcp(
            action="update",
            entity_type="bill",
            entity_id=bill.id,
            endpoint="update_bill",
            changes=column_changes(before, snapshot_columns(bill)),
        )

        logger.info("MCP [user=%s]: updated bill id=%d", _audit_username(), bill.id)
        return serialize_bill(bill)
    except (PaymentMethodError, PaidFromError) as exc:
        db.rollback()
        return exc.message
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return exc.detail
    except Exception:
        logger.exception(
            "MCP [user=%s]: error updating bill id=%d", _audit_username(), bill_id
        )
        db.rollback()
        return "Internal error updating bill."
    finally:
        db.close()


@mcp.tool()
@audit_write
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

        values = summary_for("bill", snapshot_columns(bill))
        bill.is_active = False
        db.commit()
        emit_mcp(
            action="delete",
            entity_type="bill",
            entity_id=bill_id,
            endpoint="delete_bill",
            values=values,
        )

        logger.info("MCP [user=%s]: deactivated bill id=%d", _audit_username(), bill_id)
        return f"Bill {bill_id} ('{bill.name}') deactivated."
    except Exception:
        logger.exception(
            "MCP [user=%s]: error deactivating bill id=%d", _audit_username(), bill_id
        )
        db.rollback()
        return "Internal error deactivating bill."
    finally:
        db.close()


@mcp.tool()
def list_due_payments() -> list[dict]:
    """List payments waiting to be confirmed or skipped.

    One row per bill, for the oldest occurrence that is due today or overdue.
    Fixed bills paid from the Bills fund are logged automatically and are
    not listed.

    Returns:
        Due payment rows with the expected amount, source, and any problem
        that would block confirm.
    """
    db = SessionLocal()
    try:
        rows = due_to_confirm(db)
        return [
            {
                **row,
                "expected_amount": float(row["expected_amount"]),
            }
            for row in rows
        ]
    finally:
        db.close()


@mcp.tool()
@audit_write
def confirm_payment(bill_id: int, due_date: str, amount: float) -> dict | str:
    """Confirm one due occurrence and log it.

    The date is the occurrence's due date, which must still be next_due_date.
    A budget payment is logged against that month. If that month was already
    swept to an overflow fund, the same amount is taken back from the fund.

    Args:
        bill_id: The recurring payment to confirm.
        due_date: The occurrence date, YYYY-MM-DD. Must match next_due_date.
        amount: Amount to log. Use the real charge, not only the estimate.

    Returns:
        The transaction, the updated bill, and any overflow take-back,
        or a plain message when the payment is blocked or already done.
    """
    db = SessionLocal()
    try:
        bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
        if not bill:
            return f"Bill {bill_id} not found."
        result = confirm_payment_service(db, bill, Decimal(str(amount)), due_date)
        after = snapshot_columns(result.bill)
        related_ids = [result.transaction.id]
        taken = result.taken_back or {}
        clawback_id = taken.get("transaction_id")
        if clawback_id is not None:
            related_ids.append(int(clawback_id))
        db.commit()
        emit_mcp(
            action="confirm",
            entity_type="bill",
            entity_id=bill.id,
            endpoint="confirm_payment",
            changes=column_changes(result.prior, after),
            related=related_rows("transaction", related_ids, "create"),
        )
        db.refresh(result.transaction)
        db.refresh(result.bill)
        return {
            "transaction": confirm_transaction_payload(result.transaction),
            "bill": serialize_bill(result.bill),
            "taken_back": result.taken_back,
            "message": result.message,
        }
    except UnknownFrequencyError as exc:
        db.rollback()
        return str(exc)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return exc.detail
    except (ValueError, TypeError) as exc:
        db.rollback()
        return f"Validation error: {exc}"
    except Exception:
        logger.exception(
            "MCP [user=%s]: error confirming bill id=%d", _audit_username(), bill_id
        )
        db.rollback()
        return "Internal error confirming payment."
    finally:
        db.close()


@mcp.tool()
@audit_write
def skip_payment(bill_id: int, due_date: str) -> dict | str:
    """Skip one occurrence. Nothing is written to the ledger.

    Args:
        bill_id: The recurring payment to skip.
        due_date: The occurrence date, YYYY-MM-DD. Must match next_due_date.

    Returns:
        The updated bill, or a plain message when it cannot be skipped.
    """
    db = SessionLocal()
    try:
        bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
        if not bill:
            return f"Bill {bill_id} not found."
        result = skip_payment_service(db, bill, due_date)
        after = snapshot_columns(result.bill)
        db.commit()
        emit_mcp(
            action="skip",
            entity_type="bill",
            entity_id=bill.id,
            endpoint="skip_payment",
            changes=column_changes(result.prior, after),
        )
        db.refresh(result.bill)
        return {"bill": serialize_bill(result.bill), "message": result.message}
    except UnknownFrequencyError as exc:
        db.rollback()
        return str(exc)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return exc.detail
    except ValueError as exc:
        db.rollback()
        return f"Validation error: {exc}"
    except Exception:
        logger.exception(
            "MCP [user=%s]: error skipping bill id=%d", _audit_username(), bill_id
        )
        db.rollback()
        return "Internal error skipping payment."
    finally:
        db.close()


def _payment_validation(exc: ValidationError) -> dict:
    """Same shape as the bills API. The submitted value is not included."""
    return {"detail": public_validation_errors(exc)}


# ---------------------------------------------------------------------------
# Payment method tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_payment_methods(include_archived: bool = False) -> list[dict]:
    """List payment methods.

    Args:
        include_archived: Include archived methods. Defaults to False.

    Returns:
        Payment method objects. Full card numbers are never stored.
    """
    db = SessionLocal()
    try:
        query = db.query(PaymentMethod)
        if not include_archived:
            query = query.filter(PaymentMethod.is_archived == False)
        methods = query.order_by(PaymentMethod.owner, PaymentMethod.nickname).all()
        return [serialize_payment_method(method) for method in methods]
    finally:
        db.close()


@mcp.tool()
def get_payment_method(payment_method_id: int) -> dict | str:
    """Get one payment method, including open replacement progress.

    Args:
        payment_method_id: The payment method id.

    Returns:
        The payment method. ``replacement`` is null when none is open.
    """
    db = SessionLocal()
    try:
        method = db.get(PaymentMethod, payment_method_id)
        if method is None:
            return f"Payment method {payment_method_id} not found."
        data = serialize_payment_method(method)
        data["replacement"] = replacement_payload(db, method)
        db.commit()
        return data
    finally:
        db.close()


@mcp.tool()
@audit_write
def create_payment_method(
    nickname: str,
    kind: str,
    owner: str,
    last4: str | None = None,
    expiry_month: int | None = None,
    expiry_year: int | None = None,
) -> dict | str:
    """Add a payment method. Store only the last 4 digits of a card or account.

    Args:
        nickname: Short name, such as Everyday Visa.
        kind: ``card``, ``bank``, or ``manual``. A wallet is added on the payment methods page.
        owner: Who the method belongs to.
        last4: Last 4 digits. Required for a card. Not used for manual.
        expiry_month: 1–12. Cards only.
        expiry_year: Four-digit year from 2000 to 2100. Cards only.

    Returns:
        The created payment method, or an error that does not repeat the input.
    """
    if kind == "wallet":
        return "Add a wallet on the payment methods page."
    try:
        parsed = PaymentMethodCreate(
            nickname=nickname,
            kind=kind,
            owner=owner,
            last4=last4,
            expiry_month=expiry_month,
            expiry_year=expiry_year,
        )
        values = prepare_payment_method(
            {
                "nickname": parsed.nickname,
                "kind": parsed.kind.value,
                "last4": parsed.last4,
                "expiry_month": parsed.expiry_month,
                "expiry_year": parsed.expiry_year,
                "owner": parsed.owner,
            },
            explicit_last4=last4 is not None and last4 != "",
            explicit_expiry=expiry_month is not None or expiry_year is not None,
        )
    except ValidationError as exc:
        return _payment_validation(exc)
    except PaymentMethodError as exc:
        return exc.message

    db = SessionLocal()
    try:
        method = PaymentMethod(**values)
        db.add(method)
        db.commit()
        db.refresh(method)
        emit_mcp(
            action="create",
            entity_type="payment_method",
            entity_id=method.id,
            endpoint="create_payment_method",
            values=summary_for("payment_method", snapshot_columns(method)),
        )
        logger.info(
            "MCP [user=%s]: created payment method id=%d",
            _audit_username(),
            method.id,
        )
        return serialize_payment_method(method)
    except Exception:
        logger.exception(
            "MCP [user=%s]: error creating payment method", _audit_username()
        )
        db.rollback()
        return "Internal error creating payment method."
    finally:
        db.close()


# _OMIT is not JSON-serializable, so the tool schema omits that default.
# Silence the resulting pydantic warning; null still clears the link.
warnings.filterwarnings(
    "ignore",
    category=PydanticJsonSchemaWarning,
    message=r"Default value .* is not JSON serializable",
)


@mcp.tool()
@audit_write
def update_payment_method(
    payment_method_id: int,
    nickname: str | None = None,
    kind: str | None = None,
    last4: str | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    expiry_month: int | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    expiry_year: int | None = _OMIT,  # pyrefly: ignore[bad-function-definition]
    owner: str | None = None,
) -> dict | str:
    """Update a payment method. Omitted fields stay as they are.

    Pass null for last4, expiry_month, or expiry_year to clear that value
    when the type allows it. A card still needs last 4 digits and an expiry.

    Args:
        payment_method_id: The payment method to update.
        nickname: New nickname.
        kind: ``card``, ``bank``, or ``manual``. A wallet is added on the payment methods page.
        last4: New last 4 digits. Null clears it when the type allows that.
        expiry_month: New expiry month. Null clears it when the type allows that.
        expiry_year: New four-digit expiry year.
        owner: New owner.

    Returns:
        The updated payment method, or an error that does not repeat the input.
    """
    if kind == "wallet":
        return "Add a wallet on the payment methods page."
    patch: dict[str, Any] = {}
    if nickname is not None:
        patch["nickname"] = nickname
    if kind is not None:
        patch["kind"] = kind
    if owner is not None:
        patch["owner"] = owner
    if last4 is not _OMIT:
        patch["last4"] = last4
    if expiry_month is not _OMIT:
        patch["expiry_month"] = expiry_month
    if expiry_year is not _OMIT:
        patch["expiry_year"] = expiry_year
    explicit_last4 = last4 is not _OMIT and last4 is not None
    explicit_expiry = (expiry_month is not _OMIT and expiry_month is not None) or (
        expiry_year is not _OMIT and expiry_year is not None
    )

    db = SessionLocal()
    try:
        method = db.get(PaymentMethod, payment_method_id)
        if method is None:
            return f"Payment method {payment_method_id} not found."
        try:
            parsed = PaymentMethodUpdate(**patch)
        except ValidationError as exc:
            return _payment_validation(exc)
        merged_patch: dict[str, Any] = {}
        if "nickname" in parsed.model_fields_set:
            merged_patch["nickname"] = parsed.nickname
        if "kind" in parsed.model_fields_set and parsed.kind is not None:
            merged_patch["kind"] = parsed.kind.value
        if "owner" in parsed.model_fields_set:
            merged_patch["owner"] = parsed.owner
        if "last4" in parsed.model_fields_set:
            merged_patch["last4"] = parsed.last4
        if "expiry_month" in parsed.model_fields_set:
            merged_patch["expiry_month"] = parsed.expiry_month
        if "expiry_year" in parsed.model_fields_set:
            merged_patch["expiry_year"] = parsed.expiry_year
        try:
            values = prepare_payment_method(
                merge_payment_method(method, merged_patch),
                explicit_last4=explicit_last4,
                explicit_expiry=explicit_expiry,
                db=db,
                existing=method,
            )
        except ValidationError as exc:
            return _payment_validation(exc)
        except PaymentMethodError as exc:
            return exc.message
        before = snapshot_columns(method)
        apply_payment_method(method, values)
        db.commit()
        db.refresh(method)
        emit_mcp(
            action="update",
            entity_type="payment_method",
            entity_id=method.id,
            endpoint="update_payment_method",
            changes=column_changes(before, snapshot_columns(method)),
        )
        logger.info(
            "MCP [user=%s]: updated payment method id=%d",
            _audit_username(),
            method.id,
        )
        return serialize_payment_method(method)
    except Exception:
        logger.exception(
            "MCP [user=%s]: error updating payment method id=%d",
            _audit_username(),
            payment_method_id,
        )
        db.rollback()
        return "Internal error updating payment method."
    finally:
        db.close()


@mcp.tool()
@audit_write
def archive_payment_method(
    payment_method_id: int, unarchive: bool = False
) -> dict | str:
    """Archive a payment method, or unarchive it when unarchive is true.

    Archiving is refused while an active bill still uses it, or while a
    replacement is open. There is no delete tool.

    Args:
        payment_method_id: The payment method id.
        unarchive: Set true to make an archived method active again.

    Returns:
        The payment method, or a one-line error.
    """
    db = SessionLocal()
    try:
        method = db.get(PaymentMethod, payment_method_id)
        if method is None:
            return f"Payment method {payment_method_id} not found."
        before = snapshot_columns(method)
        action = "unarchive" if unarchive else "archive"
        try:
            if unarchive:
                unarchive_method(method)
            else:
                archive_method(db, method)
        except PaymentMethodError as exc:
            db.rollback()
            return exc.message
        db.commit()
        db.refresh(method)
        emit_mcp(
            action=action,
            entity_type="payment_method",
            entity_id=method.id,
            endpoint="archive_payment_method",
            changes=column_changes(before, snapshot_columns(method)),
        )
        logger.info(
            "MCP [user=%s]: %s payment method id=%d",
            _audit_username(),
            "unarchived" if unarchive else "archived",
            method.id,
        )
        return serialize_payment_method(method)
    except Exception:
        logger.exception(
            "MCP [user=%s]: error archiving payment method id=%d",
            _audit_username(),
            payment_method_id,
        )
        db.rollback()
        return "Internal error updating payment method."
    finally:
        db.close()
