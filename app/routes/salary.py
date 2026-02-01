import json
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware import get_current_user
from app.models import SalaryAllocation, SalaryAllocationToSinkingFund, SinkingFund
from app.schemas import SalaryAllocationCreate, SalaryAllocationResponse

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _upsert_allocation(
    db: Session,
    monthly_salary_amount: Decimal,
    monthly_budget_allocation: Decimal,
    bills_fund_allocation_type: str,
    bills_fund_fixed_amount: Decimal | None,
    fund_allocations: list[dict],
) -> tuple[SalaryAllocation, bool]:
    """Create or update the single SalaryAllocation row.

    Returns (allocation, created) where created is True if a new row was inserted.
    """
    allocation = db.query(SalaryAllocation).first()
    created = allocation is None

    if created:
        allocation = SalaryAllocation(
            monthly_salary_amount=monthly_salary_amount,
            monthly_budget_allocation=monthly_budget_allocation,
            bills_fund_allocation_type=bills_fund_allocation_type,
            bills_fund_fixed_amount=bills_fund_fixed_amount,
        )
        db.add(allocation)
        db.flush()
    else:
        allocation.monthly_salary_amount = monthly_salary_amount
        allocation.monthly_budget_allocation = monthly_budget_allocation
        allocation.bills_fund_allocation_type = bills_fund_allocation_type
        allocation.bills_fund_fixed_amount = bills_fund_fixed_amount
        # Delete existing junction rows
        db.query(SalaryAllocationToSinkingFund).filter(
            SalaryAllocationToSinkingFund.salary_allocation_id == allocation.id
        ).delete()

    # Insert new junction rows
    for fa in fund_allocations:
        junction = SalaryAllocationToSinkingFund(
            salary_allocation_id=allocation.id,
            sinking_fund_id=fa["sinking_fund_id"],
            allocation_amount=fa["allocation_amount"],
        )
        db.add(junction)

    db.commit()
    db.refresh(allocation)
    return allocation, created


@router.get("/salary", response_class=HTMLResponse)
async def salary_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    allocation = db.query(SalaryAllocation).first()
    sinking_funds = (
        db.query(SinkingFund).filter(SinkingFund.is_deleted == False).all()  # noqa: E712
    )

    fund_allocation_map: dict[int, float] = {}
    if allocation:
        for junction in allocation.sinking_fund_allocations:
            fund_allocation_map[junction.sinking_fund_id] = float(junction.allocation_amount)

    sinking_funds_json = json.dumps([
        {"id": f.id, "name": f.name, "color": f.color}
        for f in sinking_funds
    ])

    return templates.TemplateResponse(
        request,
        "salary.html",
        {
            "username": user.username,
            "allocation": allocation,
            "sinking_funds": sinking_funds,
            "fund_allocation_map": fund_allocation_map,
            "sinking_funds_json": sinking_funds_json,
        },
    )


@router.post("/salary", response_class=HTMLResponse)
async def salary_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    # Parse salary amount
    try:
        monthly_salary_amount = Decimal(form.get("monthly_salary_amount", "0"))
    except (InvalidOperation, TypeError):
        monthly_salary_amount = Decimal("0")

    if monthly_salary_amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Salary must be greater than zero.</p>'
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
        monthly_salary_amount,
        monthly_budget_allocation,
        bills_fund_allocation_type,
        bills_fund_fixed_amount,
        fund_allocations,
    )

    return HTMLResponse(
        '<p class="text-green-600 text-sm">Salary allocation saved successfully.</p>'
    )


@router.get("/api/salary")
async def api_get_salary(request: Request, db: Session = Depends(get_db)):
    allocation = db.query(SalaryAllocation).first()
    if not allocation:
        return JSONResponse({"detail": "No salary allocation found"}, status_code=404)
    return SalaryAllocationResponse.model_validate(allocation)


@router.post("/api/salary")
async def api_post_salary(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = SalaryAllocationCreate(**body)
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
        data.monthly_salary_amount,
        data.monthly_budget_allocation,
        data.bills_fund_allocation_type.value,
        bills_fixed,
        fund_allocations,
    )

    response = SalaryAllocationResponse.model_validate(allocation)
    status_code = 201 if created else 200
    return JSONResponse(response.model_dump(mode="json"), status_code=status_code)
