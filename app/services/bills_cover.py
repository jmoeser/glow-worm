"""Dashboard Bills-fund cover check over a configurable horizon.

Compares obligations (projected recurring bills plus unpaid generated bill
expenses) with cash available from the Bills fund and expected primary
contributions, and again with secondary-income goals treated as met.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import TIMEZONE, bills_cover_horizon_months
from app.models import (
    IncomeAllocation,
    RecurringBill,
    SecondaryIncomeAllocation,
    SinkingFund,
    Transaction,
)
from app.tasks import _compute_bills_recommended, iter_bill_due_dates

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT)


def _add_months(current: date, months: int) -> date:
    """Add calendar months, clamping the day the way relativedelta does."""
    month_index = current.month - 1 + months
    year = current.year + month_index // 12
    month = month_index % 12 + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _clamp_horizon(months: int) -> int:
    return min(12, max(1, months))


@dataclass(frozen=True)
class BillsCoverResult:
    """Cover figures for the Bills fund. Money fields are quantized to cents."""

    horizon_months: int
    horizon_end: date
    obligations: Decimal
    without_cash: Decimal
    with_cash: Decimal
    short_without: Decimal
    short_with: Decimal
    projected_count: int
    unpaid_count: int
    has_secondary_goal: bool
    bills_fund_found: bool

    @property
    def show_banner(self) -> bool:
        """Banner is only for a shortfall on the path that ignores secondary income."""
        return self.bills_fund_found and self.short_without > 0


def _bills_fund(db: Session) -> SinkingFund | None:
    return (
        db.query(SinkingFund)
        .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)
        .first()
    )


def _monthly_primary(db: Session) -> Decimal:
    allocation = db.query(IncomeAllocation).first()
    if allocation is None:
        return Decimal("0.00")
    if allocation.bills_fund_allocation_type == "fixed":
        return _money(allocation.bills_fund_fixed_amount or 0)
    return _compute_bills_recommended(db)


def _primary_posted(db: Session, fund_id: int, year: int, month: int) -> bool:
    """True when this month already has an income-allocation row on the Bills fund.

    Same signal as ``generate_bills_forecast``: any ``income_allocation``
    transaction for that fund inside the calendar month.
    """
    last = calendar.monthrange(year, month)[1]
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year:04d}-{month:02d}-{last:02d}"
    posted = (
        db.query(Transaction.id)
        .filter(
            Transaction.transaction_type == "income_allocation",
            Transaction.sinking_fund_id == fund_id,
            Transaction.date >= start,
            Transaction.date <= end,
        )
        .first()
    )
    return posted is not None


def _future_allocation_dates(today: date, horizon_end: date) -> list[date]:
    """Income-allocation days (the 1st) strictly after today and on/before the end."""
    dates: list[date] = []
    year, month = today.year, today.month
    while True:
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        allocation_day = date(year, month, 1)
        if allocation_day > horizon_end:
            break
        if allocation_day > today:
            dates.append(allocation_day)
    return dates


def _primary_inflow(
    db: Session, fund_id: int, today: date, horizon_end: date
) -> Decimal:
    monthly = _monthly_primary(db)
    if monthly == 0:
        return Decimal("0.00")
    total = Decimal("0.00")
    # Today is always on or after the 1st. Include this month once if it has
    # not posted yet, then every later 1st that still falls in the horizon.
    if not _primary_posted(db, fund_id, today.year, today.month):
        total += monthly
    for _day in _future_allocation_dates(today, horizon_end):
        total += monthly
    return _money(total)


def _bills_secondary_goal(db: Session, fund_id: int) -> Decimal:
    allocation = db.query(SecondaryIncomeAllocation).first()
    if allocation is None:
        return Decimal("0.00")
    total = Decimal("0.00")
    for rule in allocation.rules:
        if rule.sinking_fund_id == fund_id:
            total += _money(rule.goal_amount)
    return _money(total)


def _secondary_recorded(db: Session, fund_id: int, year: int, month: int) -> Decimal:
    """Secondary already booked to this fund in the calendar month.

    Matches the secondary-income page: ``secondary_income_allocation`` rows
    whose date falls in that month. Surplus overflow uses a different
    transaction type and is not treated as goal progress.
    """
    prefix = f"{year:04d}-{month:02d}"
    rows = (
        db.query(Transaction)
        .filter(
            Transaction.transaction_type == "secondary_income_allocation",
            Transaction.sinking_fund_id == fund_id,
            Transaction.date.like(f"{prefix}%"),
        )
        .all()
    )
    return _money(sum((_money(row.amount) for row in rows), Decimal(0)))


def _months_intersecting(today: date, horizon_end: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    year, month = today.year, today.month
    while (year, month) <= (horizon_end.year, horizon_end.month):
        months.append((year, month))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return months


def _secondary_inflow(
    db: Session, fund_id: int, today: date, horizon_end: date, goal: Decimal
) -> Decimal:
    if goal <= 0:
        return Decimal("0.00")
    total = Decimal("0.00")
    for year, month in _months_intersecting(today, horizon_end):
        recorded = (
            _secondary_recorded(db, fund_id, year, month)
            if (year, month) == (today.year, today.month)
            else Decimal("0.00")
        )
        total += max(Decimal("0.00"), goal - recorded)
    return _money(total)


def _projected_occurrences(
    db: Session, horizon_end: date
) -> list[tuple[int, date, Decimal]]:
    bills = db.query(RecurringBill).filter(RecurringBill.is_active == True).all()
    occurrences: list[tuple[int, date, Decimal]] = []
    for bill in bills:
        amount = _money(bill.amount)
        try:
            due_dates = iter_bill_due_dates(
                bill.next_due_date, bill.frequency, horizon_end
            )
            for due in due_dates:
                occurrences.append((bill.id, due, amount))
        except ValueError:
            logger.warning(
                "Skipping bill %s with invalid next_due_date %r",
                bill.id,
                bill.next_due_date,
            )
    return occurrences


def _unpaid_bill_expenses(
    db: Session, horizon_end: date, bills_fund_id: int
) -> list[Transaction]:
    """Unpaid bill expenses on or before the horizon, including dates before today.

    Count a row when it is linked to the Bills fund or has no sinking fund.
    Expenses charged to some other fund are that fund's obligation.

    Scheduler-generated bills are stored with ``is_paid`` true (the model
    default) and the Bills balance is reduced at the same time, so they are
    not in this set. ``is_paid`` false is an explicit flag. Recording such a
    row against the Bills fund still reduces ``current_balance`` immediately,
    so the cover check can be conservative for those rows: the lower balance
    and the obligation both reflect the same expense.
    """
    rows = (
        db.query(Transaction)
        .filter(
            Transaction.is_paid == False,
            Transaction.type == "expense",
            Transaction.recurring_bill_id.is_not(None),
            Transaction.date <= horizon_end.isoformat(),
        )
        .all()
    )
    kept: list[Transaction] = []
    for row in rows:
        if row.sinking_fund_id not in (None, bills_fund_id):
            continue
        kept.append(row)
    return kept


def _obligations(
    occurrences: list[tuple[int, date, Decimal]], unpaid: list[Transaction]
) -> tuple[Decimal, int, int]:
    """Sum projections and unpaid rows, preferring the ledger for the same bill+date."""
    projected_keys = {
        (bill_id, due.isoformat()) for bill_id, due, _amount in occurrences
    }
    ledger_sums: dict[tuple[int, str], Decimal] = {}
    ledger_counts: dict[tuple[int, str], int] = {}
    extra = Decimal("0.00")
    extra_count = 0

    for row in unpaid:
        bill_id = row.recurring_bill_id
        if bill_id is None:
            continue
        try:
            due = date.fromisoformat(row.date)
        except ValueError:
            extra += _money(row.amount)
            extra_count += 1
            continue
        key = (bill_id, due.isoformat())
        amount = _money(row.amount)
        if key in projected_keys:
            ledger_sums[key] = ledger_sums.get(key, Decimal("0.00")) + amount
            ledger_counts[key] = ledger_counts.get(key, 0) + 1
        else:
            extra += amount
            extra_count += 1

    total = extra
    projected_count = 0
    unpaid_count = extra_count
    consumed: set[tuple[int, str]] = set()
    for bill_id, due, amount in occurrences:
        key = (bill_id, due.isoformat())
        if key in ledger_sums:
            if key not in consumed:
                total += ledger_sums[key]
                unpaid_count += ledger_counts[key]
                consumed.add(key)
        else:
            total += amount
            projected_count += 1
    return _money(total), projected_count, unpaid_count


def _empty(horizon_months: int, horizon_end: date) -> BillsCoverResult:
    zero = Decimal("0.00")
    return BillsCoverResult(
        horizon_months=horizon_months,
        horizon_end=horizon_end,
        obligations=zero,
        without_cash=zero,
        with_cash=zero,
        short_without=zero,
        short_with=zero,
        projected_count=0,
        unpaid_count=0,
        has_secondary_goal=False,
        bills_fund_found=False,
    )


def compute_bills_cover(
    db: Session,
    *,
    today: date | None = None,
    horizon_months: int | None = None,
) -> BillsCoverResult:
    """Return both cover paths for the Bills fund as of ``today``.

    ``horizon_months`` overrides the env setting when passed, and is clamped
    to 1–12. The horizon end is ``today`` plus that many calendar months.
    """
    if today is None:
        today = datetime.now(TIMEZONE).date()
    if horizon_months is None:
        months = bills_cover_horizon_months()
    else:
        months = _clamp_horizon(horizon_months)
    horizon_end = _add_months(today, months)

    fund = _bills_fund(db)
    if fund is None:
        return _empty(months, horizon_end)

    occurrences = _projected_occurrences(db, horizon_end)
    unpaid = _unpaid_bill_expenses(db, horizon_end, fund.id)
    obligations, projected_count, unpaid_count = _obligations(occurrences, unpaid)

    balance = _money(fund.current_balance)
    primary = _primary_inflow(db, fund.id, today, horizon_end)
    goal = _bills_secondary_goal(db, fund.id)
    secondary = _secondary_inflow(db, fund.id, today, horizon_end, goal)

    without_cash = _money(balance + primary)
    with_cash = _money(without_cash + secondary)
    short_without = _money(max(Decimal(0), obligations - without_cash))
    short_with = _money(max(Decimal(0), obligations - with_cash))

    return BillsCoverResult(
        horizon_months=months,
        horizon_end=horizon_end,
        obligations=obligations,
        without_cash=without_cash,
        with_cash=with_cash,
        short_without=short_without,
        short_with=short_with,
        projected_count=projected_count,
        unpaid_count=unpaid_count,
        has_secondary_goal=goal > 0,
        bills_fund_found=True,
    )
