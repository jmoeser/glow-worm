from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware import get_current_user
from app.models import IncomeAllocation, IncomeAllocationToSinkingFund, SinkingFund
from app.schemas import IncomeAllocationCreate, IncomeAllocationResponse
from app.templating import templates

router = APIRouter()


def _upsert_allocation(
    db: Session,
    monthly_income_amount: Decimal,
    monthly_budget_allocation: Decimal,
    bills_fund_allocation_type: str,
    bills_fund_fixed_amount: Decimal | None,
    fund_allocations: list[dict],
) -> tuple[IncomeAllocation, bool]:
    """Create or update the single IncomeAllocation row.

    Returns (allocation, created) where created is True if a new row was inserted.
    """
    allocation = db.query(IncomeAllocation).first()
    created = allocation is None

    if created:
        allocation = IncomeAllocation(
            monthly_income_amount=monthly_income_amount,
            monthly_budget_allocation=monthly_budget_allocation,
            bills_fund_allocation_type=bills_fund_allocation_type,
            bills_fund_fixed_amount=bills_fund_fixed_amount,
        )
        db.add(allocation)
        db.flush()
    else:
        allocation.monthly_income_amount = monthly_income_amount
        allocation.monthly_budget_allocation = monthly_budget_allocation
        allocation.bills_fund_allocation_type = bills_fund_allocation_type
        allocation.bills_fund_fixed_amount = bills_fund_fixed_amount
        # Delete existing junction rows
        db.query(IncomeAllocationToSinkingFund).filter(
            IncomeAllocationToSinkingFund.income_allocation_id == allocation.id
        ).delete()

    # Insert new junction rows
    for fa in fund_allocations:
        junction = IncomeAllocationToSinkingFund(
            income_allocation_id=allocation.id,
            sinking_fund_id=fa["sinking_fund_id"],
            allocation_amount=fa["allocation_amount"],
        )
        db.add(junction)

    db.commit()
    db.refresh(allocation)
    return allocation, created


@router.get("/income", response_class=HTMLResponse)
async def income_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    allocation = db.query(IncomeAllocation).first()
    sinking_funds = (
        db.query(SinkingFund).filter(SinkingFund.is_deleted == False).all()  # noqa: E712
    )

    fund_allocation_map: dict[int, float] = {}
    if allocation:
        for junction in allocation.sinking_fund_allocations:
            fund_allocation_map[junction.sinking_fund_id] = float(junction.allocation_amount)

    sinking_funds_data = [
        {"id": f.id, "name": f.name, "color": f.color}
        for f in sinking_funds
    ]

    return templates.TemplateResponse(
        request,
        "income.html",
        {
            "username": user.username,
            "allocation": allocation,
            "sinking_funds": sinking_funds,
            "fund_allocation_map": fund_allocation_map,
            "sinking_funds_data": sinking_funds_data,
        },
    )


@router.post("/income", response_class=HTMLResponse)
async def income_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    # Parse income amount
    try:
        monthly_income_amount = Decimal(form.get("monthly_income_amount", "0"))
    except (InvalidOperation, TypeError):
        monthly_income_amount = Decimal("0")

    if monthly_income_amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Income must be greater than zero.</p>'
        )

    # Parse budget allocation
    try:
        monthly_budget_allocation = Decimal(form.get("monthly_budget_allocation", "0"))
    except (InvalidOperation, TypeError):
        monthly_budget_allocation = Decimal("0")

    # Parse bills fund allocation type
    bills_fund_allocation_type = form.get("bills_fund_allocation_type", "recommended")
    bills_fund_fixed_amount = None

    if bills_fund_allocation_type == "fixed":
        raw = form.get("bills_fund_fixed_amount", "")
        if not raw:
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Fixed amount is required when type is fixed.</p>'
            )
        try:
            bills_fund_fixed_amount = Decimal(raw)
        except (InvalidOperation, TypeError):
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Fixed amount is required when type is fixed.</p>'
            )

    # Parse sinking fund allocations from fund_<id> keys
    fund_allocations = []
    for key in form:
        if key.startswith("fund_"):
            try:
                fund_id = int(key.removeprefix("fund_"))
                amount = Decimal(form[key])
                if amount > 0:
                    fund_allocations.append(
                        {"sinking_fund_id": fund_id, "allocation_amount": amount}
                    )
            except (ValueError, InvalidOperation):
                continue

    _upsert_allocation(
        db,
        monthly_income_amount,
        monthly_budget_allocation,
        bills_fund_allocation_type,
        bills_fund_fixed_amount,
        fund_allocations,
    )

    return HTMLResponse(
        '<p class="text-green-600 text-sm">Income allocation saved successfully.</p>'
    )


@router.get("/api/income")
async def api_get_income(request: Request, db: Session = Depends(get_db)):
    allocation = db.query(IncomeAllocation).first()
    if not allocation:
        return JSONResponse({"detail": "No income allocation found"}, status_code=404)
    return IncomeAllocationResponse.model_validate(allocation)


@router.post("/api/income")
async def api_post_income(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = IncomeAllocationCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    fund_allocations = [
        {"sinking_fund_id": fa.sinking_fund_id, "allocation_amount": fa.allocation_amount}
        for fa in data.sinking_fund_allocations
    ]

    bills_fixed = data.bills_fund_fixed_amount
    if data.bills_fund_allocation_type.value == "recommended":
        bills_fixed = None

    allocation, created = _upsert_allocation(
        db,
        data.monthly_income_amount,
        data.monthly_budget_allocation,
        data.bills_fund_allocation_type.value,
        bills_fixed,
        fund_allocations,
    )

    response = IncomeAllocationResponse.model_validate(allocation)
    status_code = 201 if created else 200
    return JSONResponse(response.model_dump(mode="json"), status_code=status_code)
