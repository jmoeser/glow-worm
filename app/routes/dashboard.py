import calendar
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pytz
from fastapi import APIRouter, Depends, Form, Request
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
        .options(joinedload(Budget.category))
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
        "budgets": budgets,
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
    now_date = datetime.now(BRISBANE).strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"username": user.username, "now_date": now_date, **data},
    )


# ---------------------------------------------------------------------------
# Quick expense
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.post("/dashboard/quick-expense", response_class=HTMLResponse)
async def quick_expense(
    request: Request,
    budget_id: str = Form(""),
    amount: str = Form(""),
    date: str = Form(""),
    month: str = Form(""),
    year: str = Form(""),
    db: Session = Depends(get_db),
):
    get_current_user(request)

    def _error(msg: str) -> HTMLResponse:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm">{msg}</p>'
        )

    # Validate budget_id
    if not budget_id:
        return _error("Budget is required.")
    try:
        budget_id_int = int(budget_id)
    except ValueError:
        return _error("Invalid budget.")

    # Validate amount
    if not amount:
        return _error("Amount is required.")
    try:
        amt = Decimal(amount)
    except InvalidOperation:
        return _error("Invalid amount.")
    if amt <= 0:
        return _error("Amount must be greater than zero.")

    # Validate date
    if not date or not _DATE_RE.match(date):
        return _error("A valid date (YYYY-MM-DD) is required.")

    # Look up budget
    budget = (
        db.query(Budget)
        .options(joinedload(Budget.category))
        .filter(Budget.id == budget_id_int)
        .first()
    )
    if not budget:
        return _error("Budget not found.")

    # Create expense transaction
    txn = Transaction(
        date=date,
        description=f"Quick expense – {budget.category.name}",
        amount=float(amt),
        category_id=budget.category_id,
        type="expense",
        transaction_type="budget_expense",
        budget_id=budget.id,
    )
    db.add(txn)

    # Increment spent_amount
    budget.spent_amount = float(
        (Decimal(str(budget.spent_amount)) + amt).quantize(Decimal("0.01"))
    )
    db.commit()

    # Redirect back to dashboard for the same month/year
    redirect_month = month or str(budget.month)
    redirect_year = year or str(budget.year)
    response = HTMLResponse("")
    response.headers["HX-Redirect"] = f"/?month={redirect_month}&year={redirect_year}"
    return response


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
