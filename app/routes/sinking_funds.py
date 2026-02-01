from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware import get_current_user
from app.models import RecurringBill, SinkingFund
from app.schemas import SinkingFundCreate, SinkingFundResponse, SinkingFundUpdate

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

FREQUENCY_ANNUAL_MULTIPLIER = {
    "monthly": 12,
    "quarterly": 4,
    "yearly": 1,
    "28_days": Decimal("13.036"),  # 365.25 / 28
}


def _active_funds(db: Session):
    return (
        db.query(SinkingFund)
        .filter(SinkingFund.is_deleted == False)  # noqa: E712
        .order_by(SinkingFund.name)
        .all()
    )


def _compute_bills_recommended(db: Session) -> Decimal:
    bills = (
        db.query(RecurringBill)
        .filter(RecurringBill.is_active == True)  # noqa: E712
        .all()
    )
    total_annual = sum(
        (
            Decimal(str(b.amount)) * Decimal(str(FREQUENCY_ANNUAL_MULTIPLIER.get(b.frequency, 1)))
            for b in bills
        ),
        Decimal("0"),
    )
    return (total_annual / 12).quantize(Decimal("0.01"))


def _bills_due_next_30_days(db: Session) -> Decimal:
    today = date.today()
    cutoff = today + timedelta(days=30)
    today_str = today.isoformat()
    cutoff_str = cutoff.isoformat()
    bills = (
        db.query(RecurringBill)
        .filter(
            RecurringBill.is_active == True,  # noqa: E712
            RecurringBill.next_due_date >= today_str,
            RecurringBill.next_due_date <= cutoff_str,
        )
        .all()
    )
    return sum((Decimal(str(b.amount)) for b in bills), Decimal("0"))


def _fund_context(db: Session):
    funds = _active_funds(db)
    total_monthly_allocation = sum(
        (Decimal(str(f.monthly_allocation)) for f in funds), Decimal("0")
    ).quantize(Decimal("0.01"))
    total_balance = sum(
        (Decimal(str(f.current_balance)) for f in funds), Decimal("0")
    ).quantize(Decimal("0.01"))
    bills_recommended = _compute_bills_recommended(db)
    bills_due_30 = _bills_due_next_30_days(db)

    # Find the Bills fund to check buffer warning
    bills_fund = next((f for f in funds if f.name == "Bills"), None)
    buffer_warning = False
    if bills_fund and bills_due_30 > 0:
        buffer_warning = Decimal(str(bills_fund.current_balance)) < bills_due_30

    return {
        "funds": funds,
        "total_monthly_allocation": total_monthly_allocation,
        "total_balance": total_balance,
        "bills_recommended": bills_recommended,
        "buffer_warning": buffer_warning,
        "bills_due_30": bills_due_30,
    }


def _render_table_body(request: Request, db: Session) -> str:
    ctx = _fund_context(db)
    return templates.TemplateResponse(
        request,
        "sinking_funds.html",
        {**ctx, "fragment": "table_body"},
    ).body.decode()


def _render_fund_row(request: Request, fund: SinkingFund, bills_recommended: Decimal = Decimal("0")) -> str:
    return templates.TemplateResponse(
        request,
        "sinking_funds.html",
        {"fund": fund, "bills_recommended": bills_recommended, "fragment": "fund_row"},
    ).body.decode()


def _render_edit_row(request: Request, fund: SinkingFund) -> str:
    return templates.TemplateResponse(
        request,
        "sinking_funds.html",
        {"fund": fund, "fragment": "edit_row"},
    ).body.decode()


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/sinking-funds", response_class=HTMLResponse)
async def sinking_funds_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    ctx = _fund_context(db)
    return templates.TemplateResponse(
        request,
        "sinking_funds.html",
        {"username": user.username, **ctx},
    )


@router.post("/sinking-funds", response_class=HTMLResponse)
async def sinking_funds_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip() or None
    color = (form.get("color") or "").strip()

    if not name:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Name is required.</p>'
        )

    if not color:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Color is required.</p>'
        )

    try:
        monthly_allocation = Decimal(form.get("monthly_allocation", "0"))
    except (InvalidOperation, TypeError):
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Invalid allocation amount.</p>'
        )

    if monthly_allocation < 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Allocation must be zero or greater.</p>'
        )

    try:
        current_balance = Decimal(form.get("current_balance", "0"))
    except (InvalidOperation, TypeError):
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Invalid initial balance.</p>'
        )

    fund = SinkingFund(
        name=name,
        description=description,
        monthly_allocation=monthly_allocation,
        color=color,
        current_balance=current_balance,
    )
    db.add(fund)
    db.commit()

    return HTMLResponse(_render_table_body(request, db))


@router.get("/sinking-funds/{fund_id}/edit", response_class=HTMLResponse)
async def sinking_funds_edit_form(request: Request, fund_id: int, db: Session = Depends(get_db)):
    fund = db.query(SinkingFund).filter(SinkingFund.id == fund_id).first()
    if not fund:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_render_edit_row(request, fund))


@router.post("/sinking-funds/{fund_id}", response_class=HTMLResponse)
async def sinking_funds_update(request: Request, fund_id: int, db: Session = Depends(get_db)):
    fund = db.query(SinkingFund).filter(SinkingFund.id == fund_id).first()
    if not fund:
        return HTMLResponse("Not found", status_code=404)

    form = await request.form()

    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    color = (form.get("color") or "").strip()

    if name:
        fund.name = name
    if description is not None:
        fund.description = description or None
    if color:
        fund.color = color

    raw_allocation = form.get("monthly_allocation")
    if raw_allocation:
        try:
            allocation = Decimal(raw_allocation)
            if allocation >= 0:
                fund.monthly_allocation = allocation
        except (InvalidOperation, TypeError):
            pass

    db.commit()
    db.refresh(fund)

    bills_recommended = _compute_bills_recommended(db)
    return HTMLResponse(_render_fund_row(request, fund, bills_recommended))


@router.delete("/sinking-funds/{fund_id}", response_class=HTMLResponse)
async def sinking_funds_delete(request: Request, fund_id: int, db: Session = Depends(get_db)):
    fund = db.query(SinkingFund).filter(SinkingFund.id == fund_id).first()
    if not fund:
        return HTMLResponse("Not found", status_code=404)
    fund.is_deleted = True
    db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------


@router.get("/api/sinking-funds")
async def api_list_funds(request: Request, db: Session = Depends(get_db)):
    funds = _active_funds(db)
    return [SinkingFundResponse.model_validate(f).model_dump(mode="json") for f in funds]


@router.post("/api/sinking-funds")
async def api_create_fund(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = SinkingFundCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    fund = SinkingFund(
        name=data.name,
        description=data.description,
        monthly_allocation=data.monthly_allocation,
        current_balance=data.current_balance,
        color=data.color,
    )
    db.add(fund)
    db.commit()
    db.refresh(fund)

    response = SinkingFundResponse.model_validate(fund)
    return JSONResponse(response.model_dump(mode="json"), status_code=201)


@router.get("/api/sinking-funds/{fund_id}")
async def api_get_fund(request: Request, fund_id: int, db: Session = Depends(get_db)):
    fund = db.query(SinkingFund).filter(SinkingFund.id == fund_id).first()
    if not fund:
        return JSONResponse({"detail": "Sinking fund not found"}, status_code=404)
    response = SinkingFundResponse.model_validate(fund)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.put("/api/sinking-funds/{fund_id}")
async def api_update_fund(request: Request, fund_id: int, db: Session = Depends(get_db)):
    fund = db.query(SinkingFund).filter(SinkingFund.id == fund_id).first()
    if not fund:
        return JSONResponse({"detail": "Sinking fund not found"}, status_code=404)

    try:
        body = await request.json()
        data = SinkingFundUpdate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(fund, field, value)

    db.commit()
    db.refresh(fund)

    response = SinkingFundResponse.model_validate(fund)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.delete("/api/sinking-funds/{fund_id}")
async def api_delete_fund(request: Request, fund_id: int, db: Session = Depends(get_db)):
    fund = db.query(SinkingFund).filter(SinkingFund.id == fund_id).first()
    if not fund:
        return JSONResponse({"detail": "Sinking fund not found"}, status_code=404)
    fund.is_deleted = True
    db.commit()
    return JSONResponse({"detail": "Sinking fund deleted"}, status_code=200)
