from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware import get_current_user
from app.models import Category, RecurringBill, SinkingFund, Transaction
from app.schemas import (
    RecurringBillCreate,
    RecurringBillPay,
    RecurringBillResponse,
    RecurringBillUpdate,
)
from app.tasks import advance_due_date
from app.templating import templates

router = APIRouter()

FREQUENCY_LABELS = {
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "yearly": "Yearly",
    "28_days": "Every 28 Days",
}

FREQUENCY_ANNUAL_MULTIPLIER = {
    "monthly": 12,
    "quarterly": 4,
    "yearly": 1,
    "28_days": Decimal("13.036"),  # 365.25 / 28
}


def _active_bills(db: Session):
    return (
        db.query(RecurringBill)
        .filter(RecurringBill.is_active == True)  # noqa: E712
        .order_by(RecurringBill.next_due_date)
        .all()
    )


def _expense_categories(db: Session):
    return (
        db.query(Category)
        .filter(Category.is_deleted == False, Category.type == "expense")  # noqa: E712
        .order_by(Category.name)
        .all()
    )


def _compute_annual_cost(amount, frequency: str) -> Decimal:
    multiplier = FREQUENCY_ANNUAL_MULTIPLIER.get(frequency, 1)
    return Decimal(str(amount)) * Decimal(str(multiplier))


def _bill_context(db: Session):
    bills = _active_bills(db)
    categories = _expense_categories(db)
    total_annual = sum(
        (_compute_annual_cost(b.amount, b.frequency) for b in bills), Decimal("0")
    )
    total_monthly = (total_annual / 12).quantize(Decimal("0.01"))
    return {
        "bills": bills,
        "categories": categories,
        "frequency_labels": FREQUENCY_LABELS,
        "total_annual": total_annual.quantize(Decimal("0.01")),
        "total_monthly": total_monthly,
    }


def _render_table_body(request: Request, db: Session) -> str:
    ctx = _bill_context(db)
    return bytes(
        templates.TemplateResponse(
            request,
            "bills.html",
            {**ctx, "fragment": "table_body"},
        ).body
    ).decode()


def _render_bill_row(request: Request, bill: RecurringBill) -> str:
    return bytes(
        templates.TemplateResponse(
            request,
            "bills.html",
            {
                "bill": bill,
                "frequency_labels": FREQUENCY_LABELS,
                "fragment": "bill_row",
            },
        ).body
    ).decode()


def _render_edit_row(request: Request, bill: RecurringBill, categories) -> str:
    return bytes(
        templates.TemplateResponse(
            request,
            "bills.html",
            {
                "bill": bill,
                "categories": categories,
                "frequency_labels": FREQUENCY_LABELS,
                "fragment": "edit_row",
            },
        ).body
    ).decode()


def _render_pay_row(request: Request, bill: RecurringBill) -> str:
    today = date.today().isoformat()
    return bytes(
        templates.TemplateResponse(
            request,
            "bills.html",
            {
                "bill": bill,
                "frequency_labels": FREQUENCY_LABELS,
                "today": today,
                "fragment": "pay_row",
            },
        ).body
    ).decode()


def _record_bill_payment(
    db: Session, bill: RecurringBill, amount: Decimal, payment_date_str: str
) -> Transaction:
    """Create a transaction for a bill payment, deduct Bills fund, advance next_due_date."""
    bills_fund = (
        db.query(SinkingFund)
        .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)  # noqa: E712
        .first()
    )
    if not bills_fund:
        raise ValueError("Bills sinking fund not found")

    txn = Transaction(
        date=payment_date_str,
        description=f"Bill payment: {bill.name} to {bill.debtor_provider}",
        amount=amount,
        category_id=bill.category_id,
        type="expense",
        transaction_type="regular",
        sinking_fund_id=bills_fund.id,
        recurring_bill_id=bill.id,
    )
    db.add(txn)

    bills_fund.current_balance = float(
        Decimal(str(bills_fund.current_balance)) - amount
    )

    current_due = date.fromisoformat(bill.next_due_date)
    bill.next_due_date = advance_due_date(current_due, bill.frequency).isoformat()

    return txn


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/bills", response_class=HTMLResponse)
async def bills_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    ctx = _bill_context(db)
    return templates.TemplateResponse(
        request,
        "bills.html",
        {"username": user.username, **ctx},
    )


@router.post("/bills", response_class=HTMLResponse)
async def bills_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    name = str(form.get("name") or "").strip()
    provider = str(form.get("debtor_provider") or "").strip()
    frequency = str(form.get("frequency") or "")
    start_date = str(form.get("start_date") or "").strip()
    next_due_date = str(form.get("next_due_date") or "").strip()
    bill_type = str(form.get("bill_type") or "fixed").strip()

    if not name or not provider:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Name and provider are required.</p>'
        )

    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation, TypeError:
        return HTMLResponse('<p class="text-red-600 text-sm">Invalid amount.</p>')

    if amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Amount must be greater than zero.</p>'
        )

    bills_category = (
        db.query(Category)
        .filter(
            Category.name == "Bills",
            Category.type == "expense",
            Category.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not bills_category:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Bills category not found. Please create it first.</p>'
        )
    category_id = bills_category.id

    if not start_date or not next_due_date:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Start date and next due date are required.</p>'
        )

    if bill_type not in ("fixed", "variable"):
        bill_type = "fixed"

    foreign_amount_raw = form.get("foreign_amount")
    foreign_currency_raw = (
        str(form.get("foreign_currency") or "").strip().upper() or None
    )
    foreign_amount = None
    if foreign_amount_raw:
        try:
            fa = Decimal(str(foreign_amount_raw))
            foreign_amount = fa if fa > 0 else None
        except InvalidOperation, TypeError:
            pass

    bill = RecurringBill(
        name=name,
        amount=amount,
        debtor_provider=provider,
        start_date=start_date,
        frequency=frequency,
        category_id=category_id,
        next_due_date=next_due_date,
        bill_type=bill_type,
        foreign_amount=foreign_amount,
        foreign_currency=foreign_currency_raw if foreign_amount else None,
    )
    db.add(bill)
    db.commit()

    return HTMLResponse(_render_table_body(request, db))


@router.get("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_get_row(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_render_bill_row(request, bill))


@router.get("/bills/{bill_id}/edit", response_class=HTMLResponse)
async def bills_edit_form(
    request: Request, bill_id: int, db: Session = Depends(get_db)
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    categories = _expense_categories(db)
    return HTMLResponse(_render_edit_row(request, bill, categories))


@router.get("/bills/{bill_id}/pay", response_class=HTMLResponse)
async def bills_pay_form(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_render_pay_row(request, bill))


@router.post("/bills/{bill_id}/pay", response_class=HTMLResponse)
async def bills_pay(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)

    form = await request.form()

    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation, TypeError:
        return HTMLResponse('<p class="text-red-600 text-sm">Invalid amount.</p>')

    if amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Amount must be greater than zero.</p>'
        )

    payment_date = str(form.get("date") or "").strip() or date.today().isoformat()

    try:
        _record_bill_payment(db, bill, amount, payment_date)
        db.commit()
        db.refresh(bill)
    except ValueError as exc:
        return HTMLResponse(f'<p class="text-red-600 text-sm">{exc}</p>')

    return HTMLResponse(_render_bill_row(request, bill))


@router.post("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_update(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)

    form = await request.form()

    name = str(form.get("name") or "").strip()
    provider = str(form.get("debtor_provider") or "").strip()

    if name:
        bill.name = name
    if provider:
        bill.debtor_provider = provider

    raw_amount = form.get("amount")
    if raw_amount:
        try:
            amount = Decimal(str(raw_amount))
            if amount > 0:
                bill.amount = float(amount)
        except InvalidOperation, TypeError:
            pass

    frequency = form.get("frequency")
    if frequency:
        bill.frequency = str(frequency)

    category_id_raw = form.get("category_id")
    if category_id_raw:
        try:
            bill.category_id = int(str(category_id_raw))
        except ValueError, TypeError:
            pass

    next_due_date = str(form.get("next_due_date") or "").strip()
    if next_due_date:
        bill.next_due_date = next_due_date

    bill_type = form.get("bill_type")
    if bill_type in ("fixed", "variable"):
        bill.bill_type = str(bill_type)

    foreign_amount_raw = form.get("foreign_amount")
    foreign_currency_raw = (
        str(form.get("foreign_currency") or "").strip().upper() or None
    )
    if foreign_amount_raw is not None:
        try:
            fa = Decimal(str(foreign_amount_raw))
            bill.foreign_amount = float(fa) if fa > 0 else None
        except InvalidOperation, TypeError:
            bill.foreign_amount = None
        bill.foreign_currency = foreign_currency_raw if bill.foreign_amount else None

    db.commit()
    db.refresh(bill)

    return HTMLResponse(_render_bill_row(request, bill))


@router.delete("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_delete(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    bill.is_active = False
    db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------


@router.get("/api/bills")
async def api_list_bills(request: Request, db: Session = Depends(get_db)):
    bills = _active_bills(db)
    return [
        RecurringBillResponse.model_validate(b).model_dump(mode="json") for b in bills
    ]


@router.post("/api/bills")
async def api_create_bill(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = RecurringBillCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    bill = RecurringBill(
        name=data.name,
        amount=data.amount,
        debtor_provider=data.debtor_provider,
        start_date=data.start_date,
        frequency=data.frequency.value,
        category_id=data.category_id,
        end_date=data.end_date,
        is_active=data.is_active,
        next_due_date=data.next_due_date,
        bill_type=data.bill_type.value,
        foreign_amount=data.foreign_amount,
        foreign_currency=data.foreign_currency,
    )
    db.add(bill)
    db.commit()
    db.refresh(bill)

    response = RecurringBillResponse.model_validate(bill)
    return JSONResponse(response.model_dump(mode="json"), status_code=201)


@router.get("/api/bills/{bill_id}")
async def api_get_bill(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)
    response = RecurringBillResponse.model_validate(bill)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.put("/api/bills/{bill_id}")
async def api_update_bill(
    request: Request, bill_id: int, db: Session = Depends(get_db)
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)

    try:
        body = await request.json()
        data = RecurringBillUpdate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    for field, value in data.model_dump(exclude_unset=True).items():
        if field in ("frequency", "bill_type") and value is not None:
            setattr(bill, field, value.value if hasattr(value, "value") else value)
        else:
            setattr(bill, field, value)

    db.commit()
    db.refresh(bill)

    response = RecurringBillResponse.model_validate(bill)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.delete("/api/bills/{bill_id}")
async def api_delete_bill(
    request: Request, bill_id: int, db: Session = Depends(get_db)
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)
    bill.is_active = False
    db.commit()
    return JSONResponse({"detail": "Bill deactivated"}, status_code=200)


@router.post("/api/bills/{bill_id}/pay")
async def api_pay_bill(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)

    try:
        body = await request.json()
        data = RecurringBillPay(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    try:
        txn = _record_bill_payment(db, bill, data.amount, data.date)
        db.commit()
        db.refresh(bill)
        db.refresh(txn)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    bill_response = RecurringBillResponse.model_validate(bill)
    return JSONResponse(
        {
            "transaction": {
                "id": txn.id,
                "date": txn.date,
                "amount": float(txn.amount),
                "description": txn.description,
            },
            "bill": bill_response.model_dump(mode="json"),
        },
        status_code=200,
    )
