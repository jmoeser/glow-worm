import calendar
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session, joinedload

from app.config import TIMEZONE
from app.database import get_db
from app.middleware import get_current_user
from app.models import (
    Budget,
    IncomeAllocation,
    MonthlyUnallocatedIncome,
    SinkingFund,
    Transaction,
)
from app.schemas import DashboardSummary, SinkingFundResponse, TransactionResponse
from app.services.budget_recommendations import recommendation_summary
from app.tasks import _compute_bills_recommended
from app.templating import templates

router = APIRouter()


def _current_month_year() -> tuple[int, int]:
    now = datetime.now(TIMEZONE)
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
        Decimal(0),
    ).quantize(Decimal("0.01"))
    total_expenses = sum(
        (Decimal(str(t.amount)) for t in transactions if t.type == "expense"),
        Decimal(0),
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
        Decimal(0),
    ).quantize(Decimal("0.01"))
    budget_total_spent = sum(
        (Decimal(str(b.spent_amount)) for b in budgets),
        Decimal(0),
    ).quantize(Decimal("0.01"))
    budget_total_remaining = (budget_total_allocated - budget_total_spent).quantize(
        Decimal("0.01")
    )

    # Sinking funds (non-deleted, ordered by name)
    sinking_funds = (
        db.query(SinkingFund)
        .filter(SinkingFund.is_deleted == False)
        .order_by(SinkingFund.name)
        .all()
    )

    # Unallocated income — use recorded value if allocation has run, otherwise
    # fall back to the configured remainder from IncomeAllocation settings.
    unallocated_row = (
        db.query(MonthlyUnallocatedIncome)
        .filter(
            MonthlyUnallocatedIncome.month == month,
            MonthlyUnallocatedIncome.year == year,
        )
        .first()
    )
    if unallocated_row:
        unallocated_income = Decimal(str(unallocated_row.unallocated_amount)).quantize(
            Decimal("0.01")
        )
    else:
        allocation = db.query(IncomeAllocation).first()
        if allocation:
            income_amount = Decimal(str(allocation.monthly_income_amount))
            total_configured = Decimal(str(allocation.monthly_budget_allocation))
            # Identify Bills fund to skip it from the regular fund loop
            bills_fund_obj = (
                db.query(SinkingFund)
                .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)
                .first()
            )
            bills_fund_id = bills_fund_obj.id if bills_fund_obj else None
            for junction in allocation.sinking_fund_allocations:
                if junction.sinking_fund_id == bills_fund_id:
                    continue
                total_configured += Decimal(str(junction.allocation_amount))
            # Bills fund amount
            if allocation.bills_fund_allocation_type == "fixed":
                total_configured += Decimal(
                    str(allocation.bills_fund_fixed_amount or 0)
                )
            else:
                total_configured += _compute_bills_recommended(db)
            # Recurring transfers
            for transfer in allocation.recurring_transfers:
                total_configured += Decimal(str(transfer.amount))
            unallocated_income = (income_amount - total_configured).quantize(
                Decimal("0.01")
            )
        else:
            unallocated_income = Decimal("0.00")

    # Total net worth: sinking fund balances + unallocated income + budget remaining
    total_sinking_funds = sum(
        (Decimal(str(sf.current_balance)) for sf in sinking_funds),
        Decimal(0),
    ).quantize(Decimal("0.01"))
    total_net_worth = (
        total_sinking_funds + unallocated_income + budget_total_remaining
    ).quantize(Decimal("0.01"))

    # Daily remaining: budget remaining / days left in month
    now = datetime.now(TIMEZONE)
    if year == now.year and month == now.month:
        days_remaining = last_day - now.day + 1  # including today
    elif year < now.year or (year == now.year and month < now.month):
        days_remaining = 0  # past month
    else:
        days_remaining = last_day  # future month

    budget_daily_remaining = (
        (budget_total_remaining / days_remaining).quantize(Decimal("0.01"))
        if days_remaining > 0
        else Decimal("0.00")
    )

    now_month, now_year = _current_month_year()
    is_current_month = month == now_month and year == now_year
    budget_rec_summary = (
        recommendation_summary(db, month, year) if is_current_month else None
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
        "total_sinking_funds": total_sinking_funds,
        "total_net_worth": total_net_worth,
        "recent_transactions": recent_transactions,
        "month": month,
        "year": year,
        "month_name": calendar.month_name[month],
        "budget_daily_remaining": budget_daily_remaining,
        "days_remaining": days_remaining,
        "is_current_month": is_current_month,
        "budget_recommendations": budget_rec_summary,
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
    now_date = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"username": user.username, "now_date": now_date, **data},
    )


# ---------------------------------------------------------------------------
# Budget overdraft warning (HTMX fragment)
# ---------------------------------------------------------------------------


@router.get("/dashboard/budget-overdraft-warning", response_class=HTMLResponse)
async def budget_overdraft_warning(
    request: Request,
    budget_id: str = "",
    amount: str = "",
    transaction_type: str = "budget_expense",
    db: Session = Depends(get_db),
):
    get_current_user(request)
    if transaction_type != "budget_expense":
        return HTMLResponse("")
    try:
        bid = int(budget_id)
        amt = Decimal(amount)
    except ValueError, InvalidOperation:
        return HTMLResponse("")
    if amt <= 0:
        return HTMLResponse("")

    budget = db.query(Budget).filter(Budget.id == bid).first()
    if not budget:
        return HTMLResponse("")

    remaining = (
        Decimal(str(budget.allocated_amount))
        - Decimal(str(budget.spent_amount))
        + Decimal(str(budget.fund_balance))
    )
    if amt <= remaining:
        return HTMLResponse("")

    overdraft = (amt - remaining).quantize(Decimal("0.01"))

    allocation = db.query(IncomeAllocation).first()
    overflow_fund = None
    if allocation and allocation.overflow_sinking_fund_id:
        overflow_fund = (
            db.query(SinkingFund)
            .filter(SinkingFund.id == allocation.overflow_sinking_fund_id)
            .first()
        )

    if overflow_fund:
        msg = (
            f"This will overdraw the budget by ${overdraft:,}. "
            f"${overdraft:,} will be withdrawn from <strong>{overflow_fund.name}</strong> "
            f"at end of month."
        )
    else:
        msg = (
            f"This will overdraw the budget by ${overdraft:,}. "
            f"No overflow fund is configured &mdash; this shortfall won&rsquo;t be automatically reconciled."
        )

    return HTMLResponse(
        f'<p class="text-yellow-400 text-sm" style="margin-top:6px;">&#9888; {msg}</p>'
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
        return HTMLResponse(f'<p class="text-red-400 text-sm">{msg}</p>')

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
        total_sinking_funds=data["total_sinking_funds"],
        total_net_worth=data["total_net_worth"],
        sinking_funds=[
            SinkingFundResponse.model_validate(sf) for sf in data["sinking_funds"]
        ],
        recent_transactions=[
            TransactionResponse.model_validate(t) for t in data["recent_transactions"]
        ],
    )
    return JSONResponse(summary.model_dump(mode="json"))
