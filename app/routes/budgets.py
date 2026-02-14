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
from app.models import Budget, Category
from app.schemas import BudgetCreate, BudgetResponse, BudgetUpdate
from app.templating import templates

router = APIRouter()

BRISBANE = pytz.timezone("Australia/Brisbane")


def _current_month_year() -> tuple[int, int]:
    now = datetime.now(BRISBANE)
    return now.month, now.year


def _budgets_for_month(db: Session, month: int, year: int) -> list[Budget]:
    return (
        db.query(Budget)
        .options(joinedload(Budget.category))
        .filter(Budget.month == month, Budget.year == year)
        .join(Category)
        .order_by(Category.name)
        .all()
    )


def _budget_categories(db: Session) -> list[Category]:
    return (
        db.query(Category)
        .filter(
            Category.is_budget_category == True,  # noqa: E712
            Category.is_deleted == False,  # noqa: E712
        )
        .order_by(Category.name)
        .all()
    )


def _available_categories(db: Session, month: int, year: int) -> list[Category]:
    all_cats = _budget_categories(db)
    budgeted_ids = {
        b.category_id
        for b in db.query(Budget)
        .filter(Budget.month == month, Budget.year == year)
        .all()
    }
    return [c for c in all_cats if c.id not in budgeted_ids]


def _budget_context(db: Session, month: int, year: int) -> dict:
    budgets = _budgets_for_month(db, month, year)
    available = _available_categories(db, month, year)

    total_allocated = sum(
        (Decimal(str(b.allocated_amount)) for b in budgets), Decimal("0")
    ).quantize(Decimal("0.01"))
    total_spent = sum(
        (Decimal(str(b.spent_amount)) for b in budgets), Decimal("0")
    ).quantize(Decimal("0.01"))
    total_remaining = (total_allocated - total_spent).quantize(Decimal("0.01"))

    # Prev/next month navigation
    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year

    if month == 12:
        next_month, next_year = 1, year + 1
    else:
        next_month, next_year = month + 1, year

    return {
        "budgets": budgets,
        "available_categories": available,
        "month": month,
        "year": year,
        "month_name": calendar.month_name[month],
        "total_allocated": total_allocated,
        "total_spent": total_spent,
        "total_remaining": total_remaining,
        "prev_month": prev_month,
        "prev_year": prev_year,
        "next_month": next_month,
        "next_year": next_year,
    }


def _render_table_body(request: Request, db: Session, month: int, year: int) -> str:
    ctx = _budget_context(db, month, year)
    return templates.TemplateResponse(
        request,
        "budgets.html",
        {**ctx, "fragment": "table_body"},
    ).body.decode()


def _render_budget_row(request: Request, budget: Budget) -> str:
    return templates.TemplateResponse(
        request,
        "budgets.html",
        {"budget": budget, "fragment": "budget_row"},
    ).body.decode()


def _render_edit_row(request: Request, budget: Budget) -> str:
    return templates.TemplateResponse(
        request,
        "budgets.html",
        {"budget": budget, "fragment": "edit_row"},
    ).body.decode()


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/budgets", response_class=HTMLResponse)
async def budgets_page(request: Request, month: int | None = None, year: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request)
    if month is None or year is None:
        month, year = _current_month_year()
    ctx = _budget_context(db, month, year)
    return templates.TemplateResponse(
        request,
        "budgets.html",
        {"username": user.username, **ctx},
    )


@router.post("/budgets", response_class=HTMLResponse)
async def budgets_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    raw_category_id = form.get("category_id", "")
    raw_allocated = form.get("allocated_amount", "")
    raw_month = form.get("month", "")
    raw_year = form.get("year", "")

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
        allocated_amount = Decimal(raw_allocated)
    except (InvalidOperation, TypeError):
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Invalid allocated amount.</p>'
        )

    if allocated_amount < 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Allocated amount must be zero or greater.</p>'
        )

    try:
        month = int(raw_month)
        year = int(raw_year)
    except (ValueError, TypeError):
        month, year = _current_month_year()

    # Check for duplicate
    existing = (
        db.query(Budget)
        .filter(
            Budget.category_id == category_id,
            Budget.month == month,
            Budget.year == year,
        )
        .first()
    )
    if existing:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">A budget for this category already exists this month.</p>'
        )

    budget = Budget(
        category_id=category_id,
        month=month,
        year=year,
        allocated_amount=allocated_amount,
        spent_amount=0,
        fund_balance=0,
    )
    db.add(budget)
    db.commit()

    return HTMLResponse(_render_table_body(request, db, month, year))


@router.get("/budgets/{budget_id}/edit", response_class=HTMLResponse)
async def budgets_edit_form(request: Request, budget_id: int, db: Session = Depends(get_db)):
    budget = (
        db.query(Budget)
        .options(joinedload(Budget.category))
        .filter(Budget.id == budget_id)
        .first()
    )
    if not budget:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_render_edit_row(request, budget))


@router.post("/budgets/{budget_id}", response_class=HTMLResponse)
async def budgets_update(request: Request, budget_id: int, db: Session = Depends(get_db)):
    budget = (
        db.query(Budget)
        .options(joinedload(Budget.category))
        .filter(Budget.id == budget_id)
        .first()
    )
    if not budget:
        return HTMLResponse("Not found", status_code=404)

    form = await request.form()
    raw_allocated = form.get("allocated_amount", "")

    try:
        allocated_amount = Decimal(raw_allocated)
        if allocated_amount >= 0:
            budget.allocated_amount = allocated_amount
    except (InvalidOperation, TypeError):
        pass

    db.commit()
    db.refresh(budget)

    return HTMLResponse(_render_budget_row(request, budget))


@router.delete("/budgets/{budget_id}", response_class=HTMLResponse)
async def budgets_delete(request: Request, budget_id: int, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if not budget:
        return HTMLResponse("Not found", status_code=404)
    db.delete(budget)
    db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------


@router.get("/api/budgets")
async def api_list_budgets(request: Request, month: int | None = None, year: int | None = None, db: Session = Depends(get_db)):
    if month is None or year is None:
        month, year = _current_month_year()
    budgets = _budgets_for_month(db, month, year)
    return [BudgetResponse.model_validate(b).model_dump(mode="json") for b in budgets]


@router.post("/api/budgets")
async def api_create_budget(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = BudgetCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # Check for duplicate
    existing = (
        db.query(Budget)
        .filter(
            Budget.category_id == data.category_id,
            Budget.month == data.month,
            Budget.year == data.year,
        )
        .first()
    )
    if existing:
        return JSONResponse(
            {"detail": "A budget for this category already exists for this month."},
            status_code=409,
        )

    budget = Budget(
        category_id=data.category_id,
        month=data.month,
        year=data.year,
        allocated_amount=data.allocated_amount,
        spent_amount=data.spent_amount,
        fund_balance=data.fund_balance,
    )
    db.add(budget)
    db.commit()
    db.refresh(budget)

    response = BudgetResponse.model_validate(budget)
    return JSONResponse(response.model_dump(mode="json"), status_code=201)


@router.get("/api/budgets/{budget_id}")
async def api_get_budget(request: Request, budget_id: int, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if not budget:
        return JSONResponse({"detail": "Budget not found"}, status_code=404)
    response = BudgetResponse.model_validate(budget)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.put("/api/budgets/{budget_id}")
async def api_update_budget(request: Request, budget_id: int, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if not budget:
        return JSONResponse({"detail": "Budget not found"}, status_code=404)

    try:
        body = await request.json()
        data = BudgetUpdate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(budget, field, value)

    db.commit()
    db.refresh(budget)

    response = BudgetResponse.model_validate(budget)
    return JSONResponse(response.model_dump(mode="json"), status_code=200)


@router.delete("/api/budgets/{budget_id}")
async def api_delete_budget(request: Request, budget_id: int, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if not budget:
        return JSONResponse({"detail": "Budget not found"}, status_code=404)
    db.delete(budget)
    db.commit()
    return JSONResponse({"detail": "Budget deleted"}, status_code=200)
