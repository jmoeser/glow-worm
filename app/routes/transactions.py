import calendar
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pytz
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.middleware import get_current_user
from app.models import Budget, Category, RecurringBill, SinkingFund, Transaction
from app.schemas import TransactionCreate, TransactionResponse, TransactionUpdate
from app.templating import templates

router = APIRouter()

BRISBANE = pytz.timezone("Australia/Brisbane")

TRANSACTION_TYPE_LABELS = {
    "regular": "Regular",
    "income": "Income",
    "income_allocation": "Income Allocation",
    "contribution": "Contribution",
    "withdrawal": "Withdrawal",
    "budget_expense": "Budget Expense",
    "budget_transfer": "Budget Transfer",
}


def _current_month_year() -> tuple[int, int]:
    now = datetime.now(BRISBANE)
    return now.month, now.year


def _transactions_for_month(
    db: Session,
    month: int,
    year: int,
    type_filter: str | None = None,
    category_filter: int | None = None,
) -> list[Transaction]:
    start = f"{year:04d}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    end = f"{year:04d}-{month:02d}-{last_day:02d}"

    query = (
        db.query(Transaction)
        .options(
            joinedload(Transaction.category),
            joinedload(Transaction.sinking_fund),
            joinedload(Transaction.recurring_bill),
            joinedload(Transaction.budget),
        )
        .filter(Transaction.date >= start, Transaction.date <= end)
    )

    if type_filter:
        query = query.filter(Transaction.type == type_filter)
    if category_filter:
        query = query.filter(Transaction.category_id == category_filter)

    return query.order_by(Transaction.date.desc(), Transaction.id.desc()).all()


def _all_categories(db: Session) -> list[Category]:
    return (
        db.query(Category)
        .filter(Category.is_deleted == False)  # noqa: E712
        .order_by(Category.name)
        .all()
    )


def _active_sinking_funds(db: Session) -> list[SinkingFund]:
    return (
        db.query(SinkingFund)
        .filter(SinkingFund.is_deleted == False)  # noqa: E712
        .order_by(SinkingFund.name)
        .all()
    )


def _active_recurring_bills(db: Session) -> list[RecurringBill]:
    return (
        db.query(RecurringBill)
        .filter(RecurringBill.is_active == True)  # noqa: E712
        .order_by(RecurringBill.name)
        .all()
    )


def _budgets_for_month_dropdown(db: Session, month: int, year: int) -> list[Budget]:
    return (
        db.query(Budget)
        .options(joinedload(Budget.category))
        .filter(Budget.month == month, Budget.year == year)
        .all()
    )


def _transaction_context(
    db: Session,
    month: int,
    year: int,
    type_filter: str | None = None,
    category_filter: int | None = None,
) -> dict:
    transactions = _transactions_for_month(db, month, year, type_filter, category_filter)
    categories = _all_categories(db)
    sinking_funds = _active_sinking_funds(db)
    recurring_bills = _active_recurring_bills(db)
    budgets = _budgets_for_month_dropdown(db, month, year)

    total_income = sum(
        (Decimal(str(t.amount)) for t in transactions if t.type == "income"),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    total_expenses = sum(
        (Decimal(str(t.amount)) for t in transactions if t.type == "expense"),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    net = (total_income - total_expenses).quantize(Decimal("0.01"))

    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year

    if month == 12:
        next_month, next_year = 1, year + 1
    else:
        next_month, next_year = month + 1, year

    return {
        "transactions": transactions,
        "categories": categories,
        "sinking_funds": sinking_funds,
        "recurring_bills": recurring_bills,
        "budgets": budgets,
        "transaction_type_labels": TRANSACTION_TYPE_LABELS,
        "month": month,
        "year": year,
        "month_name": calendar.month_name[month],
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net": net,
        "prev_month": prev_month,
        "prev_year": prev_year,
        "next_month": next_month,
        "next_year": next_year,
        "type_filter": type_filter or "",
        "category_filter": category_filter or "",
    }


def _parse_optional_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _render_table_body(request: Request, db: Session, month: int, year: int, type_filter=None, category_filter=None) -> str:
    ctx = _transaction_context(db, month, year, type_filter, category_filter)
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {**ctx, "fragment": "table_body"},
    ).body.decode()


def _render_transaction_row(request: Request, txn: Transaction) -> str:
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {"txn": txn, "transaction_type_labels": TRANSACTION_TYPE_LABELS, "fragment": "transaction_row"},
    ).body.decode()


def _render_edit_row(request: Request, txn: Transaction, db: Session) -> str:
    month_val, year_val = _current_month_year()
    # Parse month/year from transaction date for budget dropdown
    try:
        parts = txn.date.split("-")
        year_val = int(parts[0])
        month_val = int(parts[1])
    except (ValueError, IndexError):
        pass

    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "txn": txn,
            "categories": _all_categories(db),
            "sinking_funds": _active_sinking_funds(db),
            "recurring_bills": _active_recurring_bills(db),
            "budgets": _budgets_for_month_dropdown(db, month_val, year_val),
            "transaction_type_labels": TRANSACTION_TYPE_LABELS,
            "fragment": "edit_row",
        },
    ).body.decode()


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/transactions", response_class=HTMLResponse)
async def transactions_page(
    request: Request,
    month: int | None = None,
    year: int | None = None,
    type_filter: str | None = None,
    category_id: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request)
    if month is None or year is None:
        month, year = _current_month_year()
    ctx = _transaction_context(db, month, year, type_filter, category_id)
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {"username": user.username, **ctx},
    )


@router.post("/transactions", response_class=HTMLResponse)
async def transactions_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    date = (form.get("date") or "").strip()
    description = (form.get("description") or "").strip() or None
    raw_amount = form.get("amount", "")
    raw_category_id = form.get("category_id", "")
    txn_type = form.get("type", "")
    transaction_type = form.get("transaction_type", "regular")
    raw_month = form.get("month", "")
    raw_year = form.get("year", "")

    if not date:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Date is required.</p>'
        )

    if not raw_category_id:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Category is required.</p>'
        )

    try:
        category_id = int(raw_category_id)
    except (ValueError, TypeError):
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Invalid category.</p>'
        )

    try:
        amount = Decimal(raw_amount)
    except (InvalidOperation, TypeError):
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Invalid amount.</p>'
        )

    if amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Amount must be greater than zero.</p>'
        )

    if txn_type not in ("income", "expense"):
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Type must be income or expense.</p>'
        )

    sinking_fund_id = _parse_optional_int(form.get("sinking_fund_id"))
    recurring_bill_id = _parse_optional_int(form.get("recurring_bill_id"))
    budget_id = _parse_optional_int(form.get("budget_id"))
    is_paid = form.get("is_paid") == "on" or form.get("is_paid") == "true"

    try:
        month = int(raw_month)
        year = int(raw_year)
    except (ValueError, TypeError):
        month, year = _current_month_year()

    txn = Transaction(
        date=date,
        description=description,
        amount=amount,
        category_id=category_id,
        type=txn_type,
        transaction_type=transaction_type,
        sinking_fund_id=sinking_fund_id,
        recurring_bill_id=recurring_bill_id,
        budget_id=budget_id,
        is_paid=is_paid,
    )
    db.add(txn)
    db.commit()

    return HTMLResponse(_render_table_body(request, db, month, year))


@router.get("/transactions/{txn_id}/edit", response_class=HTMLResponse)
async def transactions_edit_form(request: Request, txn_id: int, db: Session = Depends(get_db)):
    txn = (
        db.query(Transaction)
        .options(
            joinedload(Transaction.category),
            joinedload(Transaction.sinking_fund),
            joinedload(Transaction.recurring_bill),
            joinedload(Transaction.budget),
        )
        .filter(Transaction.id == txn_id)
        .first()
    )
    if not txn:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_render_edit_row(request, txn, db))


@router.post("/transactions/{txn_id}", response_class=HTMLResponse)
async def transactions_update(request: Request, txn_id: int, db: Session = Depends(get_db)):
    txn = (
        db.query(Transaction)
        .options(
            joinedload(Transaction.category),
            joinedload(Transaction.sinking_fund),
            joinedload(Transaction.recurring_bill),
            joinedload(Transaction.budget),
        )
        .filter(Transaction.id == txn_id)
        .first()
    )
    if not txn:
        return HTMLResponse("Not found", status_code=404)

    form = await request.form()

    date = (form.get("date") or "").strip()
    if date:
        txn.date = date

    description = form.get("description")
    if description is not None:
        txn.description = description.strip() or None

    raw_amount = form.get("amount")
    if raw_amount:
        try:
            amount = Decimal(raw_amount)
            if amount > 0:
                txn.amount = amount
        except (InvalidOperation, TypeError):
            pass

    raw_category_id = form.get("category_id")
    if raw_category_id:
        try:
            txn.category_id = int(raw_category_id)
        except (ValueError, TypeError):
            pass

    txn_type = form.get("type")
    if txn_type in ("income", "expense"):
        txn.type = txn_type

    transaction_type = form.get("transaction_type")
    if transaction_type and transaction_type in TRANSACTION_TYPE_LABELS:
        txn.transaction_type = transaction_type

    # Optional FK fields — allow clearing by setting to empty string
    if "sinking_fund_id" in form:
        txn.sinking_fund_id = _parse_optional_int(form.get("sinking_fund_id"))
    if "recurring_bill_id" in form:
        txn.recurring_bill_id = _parse_optional_int(form.get("recurring_bill_id"))
    if "budget_id" in form:
        txn.budget_id = _parse_optional_int(form.get("budget_id"))

    if "is_paid" in form:
        txn.is_paid = form.get("is_paid") == "on" or form.get("is_paid") == "true"

    db.commit()
    db.refresh(txn)

    return HTMLResponse(_render_transaction_row(request, txn))


@router.delete("/transactions/{txn_id}", response_class=HTMLResponse)
async def transactions_delete(request: Request, txn_id: int, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        return HTMLResponse("Not found", status_code=404)
    db.delete(txn)
    db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------


@router.get("/api/transactions")
async def api_list_transactions(
    request: Request,
    month: int | None = None,
    year: int | None = None,
    type_filter: str | None = None,
    category_id: int | None = None,
    db: Session = Depends(get_db),
):
    if month is None or year is None:
        month, year = _current_month_year()
    transactions = _transactions_for_month(db, month, year, type_filter, category_id)
    return [TransactionResponse.model_validate(t).model_dump(mode="json") for t in transactions]


@router.post("/api/transactions")
async def api_create_transaction(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = TransactionCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    txn = Transaction(
        date=data.date,
        description=data.description,
        amount=data.amount,
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

    response = TransactionResponse.model_validate(txn)
    return JSONResponse(response.model_dump(mode="json"), status_code=201)


@router.get("/api/transactions/{txn_id}")
async def api_get_transaction(request: Request, txn_id: int, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        return JSONResponse({"detail": "Transaction not found"}, status_code=404)
    response = TransactionResponse.model_validate(txn)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.put("/api/transactions/{txn_id}")
async def api_update_transaction(request: Request, txn_id: int, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        return JSONResponse({"detail": "Transaction not found"}, status_code=404)

    try:
        body = await request.json()
        data = TransactionUpdate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    for field, value in data.model_dump(exclude_unset=True).items():
        if field in ("type", "transaction_type") and value is not None:
            setattr(txn, field, value.value if hasattr(value, "value") else value)
        else:
            setattr(txn, field, value)

    db.commit()
    db.refresh(txn)

    response = TransactionResponse.model_validate(txn)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.delete("/api/transactions/{txn_id}")
async def api_delete_transaction(request: Request, txn_id: int, db: Session = Depends(get_db)):
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        return JSONResponse({"detail": "Transaction not found"}, status_code=404)
    db.delete(txn)
    db.commit()
    return JSONResponse({"detail": "Transaction deleted"}, status_code=200)
