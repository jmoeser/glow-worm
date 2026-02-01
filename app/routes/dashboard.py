import calendar
from datetime import datetime
from decimal import Decimal

import pytz
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.middleware import get_current_user
from app.models import Budget, MonthlyUnallocatedIncome, SinkingFund, Transaction
from app.schemas import DashboardSummary, SinkingFundResponse, TransactionResponse

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

BRISBANE = pytz.timezone("Australia/Brisbane")


def _current_month_year() -> tuple[int, int]:
    now = datetime.now(BRISBANE)
    return now.month, now.year


def _dashboard_data(db: Session, month: int, year: int) -> dict:
    """Assemble all dashboard data for the given month/year."""
    start = f"{year:04d}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    end = f"{year:04d}-{month:02d}-{last_day:02d}"

    # Transactions for the month
    transactions = (
        db.query(Transaction)
        .options(
            joinedload(Transaction.category),
            joinedload(Transaction.sinking_fund),
            joinedload(Transaction.recurring_bill),
            joinedload(Transaction.budget),
        )
        .filter(Transaction.date >= start, Transaction.date <= end)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )

    total_income = sum(
        (Decimal(str(t.amount)) for t in transactions if t.type == "income"),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    total_expenses = sum(
        (Decimal(str(t.amount)) for t in transactions if t.type == "expense"),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    net = (total_income - total_expenses).quantize(Decimal("0.01"))

    recent_transactions = transactions[:10]

    # Budget totals for the month
    budgets = (
        db.query(Budget)
        .filter(Budget.month == month, Budget.year == year)
        .all()
    )
    budget_total_allocated = sum(
        (Decimal(str(b.allocated_amount)) for b in budgets),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    budget_total_spent = sum(
        (Decimal(str(b.spent_amount)) for b in budgets),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    budget_total_remaining = (budget_total_allocated - budget_total_spent).quantize(Decimal("0.01"))

    # Sinking funds (non-deleted, ordered by name)
    sinking_funds = (
        db.query(SinkingFund)
        .filter(SinkingFund.is_deleted == False)  # noqa: E712
        .order_by(SinkingFund.name)
        .all()
    )

    # Unallocated income
    unallocated_row = (
        db.query(MonthlyUnallocatedIncome)
        .filter(
            MonthlyUnallocatedIncome.month == month,
            MonthlyUnallocatedIncome.year == year,
        )
        .first()
    )
    unallocated_income = (
        Decimal(str(unallocated_row.unallocated_amount)).quantize(Decimal("0.01"))
        if unallocated_row
        else Decimal("0.00")
    )

    return {
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net": net,
        "unallocated_income": unallocated_income,
        "budget_total_allocated": budget_total_allocated,
        "budget_total_spent": budget_total_spent,
        "budget_total_remaining": budget_total_remaining,
        "sinking_funds": sinking_funds,
        "recent_transactions": recent_transactions,
        "month": month,
        "year": year,
        "month_name": calendar.month_name[month],
    }


# ---------------------------------------------------------------------------
# HTML route
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    month: int | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request)
    if month is None or year is None:
        month, year = _current_month_year()
    data = _dashboard_data(db, month, year)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"username": user.username, **data},
    )


# ---------------------------------------------------------------------------
# JSON API route
# ---------------------------------------------------------------------------


@router.get("/api/dashboard")
async def api_dashboard(
    request: Request,
    month: int | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    if month is None or year is None:
        month, year = _current_month_year()
    data = _dashboard_data(db, month, year)
    summary = DashboardSummary(
        total_income=data["total_income"],
        total_expenses=data["total_expenses"],
        net=data["net"],
        unallocated_income=data["unallocated_income"],
        budget_total_allocated=data["budget_total_allocated"],
        budget_total_spent=data["budget_total_spent"],
        budget_total_remaining=data["budget_total_remaining"],
        sinking_funds=[SinkingFundResponse.model_validate(sf) for sf in data["sinking_funds"]],
        recent_transactions=[TransactionResponse.model_validate(t) for t in data["recent_transactions"]],
    )
    return JSONResponse(summary.model_dump(mode="json"))
