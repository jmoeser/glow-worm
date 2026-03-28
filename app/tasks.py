"""Background tasks for automated income allocation and bill processing."""

import calendar
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.database import SessionLocal
from app.models import (
    Budget,
    Category,
    MonthlyUnallocatedIncome,
    RecurringBill,
    IncomeAllocation,
    SinkingFund,
    Transaction,
)

logger = logging.getLogger(__name__)

FREQUENCY_ANNUAL_MULTIPLIER = {
    "monthly": 12,
    "quarterly": 4,
    "yearly": 1,
    "28_days": Decimal("13.036"),
}


def _today() -> date:
    """Return today's date in the configured timezone."""
    return datetime.now(TIMEZONE).date()


def advance_due_date(current: date, frequency: str) -> date:
    """Advance a due date based on frequency, clamping to last day of month."""
    if frequency == "28_days":
        return current + timedelta(days=28)

    if frequency == "monthly":
        month = current.month + 1
        year = current.year
        if month > 12:
            month = 1
            year += 1
        day = min(current.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    if frequency == "quarterly":
        month = current.month + 3
        year = current.year
        while month > 12:
            month -= 12
            year += 1
        day = min(current.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    if frequency == "yearly":
        year = current.year + 1
        day = min(current.day, calendar.monthrange(year, current.month)[1])
        return date(year, current.month, day)

    return current + timedelta(days=30)


def _compute_bills_recommended(db: Session) -> Decimal:
    """Calculate recommended monthly bills allocation: total annual cost / 12."""
    bills = (
        db.query(RecurringBill)
        .filter(RecurringBill.is_active == True)  # noqa: E712
        .all()
    )
    total_annual = sum(
        (
            Decimal(str(b.amount))
            * Decimal(str(FREQUENCY_ANNUAL_MULTIPLIER.get(b.frequency, 1)))
            for b in bills
        ),
        Decimal("0"),
    )
    if total_annual == 0:
        return Decimal("0")
    return (total_annual / 12).quantize(Decimal("0.01"))


def generate_bills_forecast(db: Session, months: int = 12) -> list[dict]:
    """Project Bills fund cash flow over the next N months.

    Returns a list of month dicts (length == months) with keys:
        month, year, month_name, bills, total_out, contribution, closing_balance
    """
    today = _today()

    active_bills = (
        db.query(RecurringBill)
        .filter(RecurringBill.is_active == True)  # noqa: E712
        .all()
    )

    bills_fund = (
        db.query(SinkingFund)
        .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)  # noqa: E712
        .first()
    )
    running_balance = (
        Decimal(str(bills_fund.current_balance)) if bills_fund else Decimal("0")
    )

    # Determine monthly contribution from income allocation config
    allocation = db.query(IncomeAllocation).first()
    if allocation:
        if allocation.bills_fund_allocation_type == "fixed":
            monthly_contribution = Decimal(str(allocation.bills_fund_fixed_amount or 0))
        else:
            monthly_contribution = _compute_bills_recommended(db)
    else:
        monthly_contribution = Decimal("0")

    # Check if this month's contribution has already been deposited
    month_start = f"{today.year}-{today.month:02d}-01"
    month_end = f"{today.year}-{today.month:02d}-{calendar.monthrange(today.year, today.month)[1]:02d}"
    already_contributed = False
    if bills_fund:
        already_contributed = (
            db.query(Transaction)
            .filter(
                Transaction.transaction_type == "income_allocation",
                Transaction.sinking_fund_id == bills_fund.id,
                Transaction.date >= month_start,
                Transaction.date <= month_end,
            )
            .first()
        ) is not None

    # Calculate the horizon end date
    end_abs = today.month - 1 + months - 1
    horizon_end = date(
        today.year + end_abs // 12,
        end_abs % 12 + 1,
        calendar.monthrange(today.year + end_abs // 12, end_abs % 12 + 1)[1],
    )

    # Project each bill's payment dates forward into monthly buckets
    monthly_bills: dict[tuple[int, int], list[dict]] = {}
    for bill in active_bills:
        current_due = date.fromisoformat(bill.next_due_date)
        while current_due <= horizon_end:
            ym = (current_due.year, current_due.month)
            monthly_bills.setdefault(ym, []).append(
                {
                    "id": bill.id,
                    "name": bill.name,
                    "amount": Decimal(str(bill.amount)),
                    "bill_type": bill.bill_type,
                    "due_date": current_due,
                    "foreign_amount": bill.foreign_amount,
                    "foreign_currency": bill.foreign_currency,
                }
            )
            current_due = advance_due_date(current_due, bill.frequency)

    # Build forecast rows
    result = []
    for i in range(months):
        abs_month = today.month - 1 + i
        m = abs_month % 12 + 1
        y = today.year + abs_month // 12

        bills_this_month = monthly_bills.get((y, m), [])
        total_out = sum((b["amount"] for b in bills_this_month), Decimal("0"))

        # Skip contribution for current month if already processed
        contribution = (
            Decimal("0") if (i == 0 and already_contributed) else monthly_contribution
        )

        running_balance = running_balance + contribution - total_out

        result.append(
            {
                "month": m,
                "year": y,
                "month_name": calendar.month_name[m],
                "bills": bills_this_month,
                "total_out": total_out.quantize(Decimal("0.01")),
                "contribution": contribution.quantize(Decimal("0.01")),
                "closing_balance": running_balance.quantize(Decimal("0.01")),
            }
        )

    return result


def process_income_allocation(db: Session | None = None) -> None:
    """Monthly task (1st): distribute income per IncomeAllocation config.

    Args:
        db: Optional database session for testing. If None, creates one
            from SessionLocal and manages its lifecycle.
    """
    _managed = db is None
    if _managed:
        db = SessionLocal()
    assert db is not None
    try:
        today = _today()
        month, year = today.month, today.year
        date_str = today.isoformat()
        month_start = f"{year}-{month:02d}-01"
        month_end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"

        # Idempotency: skip if income already processed this month
        existing = (
            db.query(Transaction)
            .filter(
                Transaction.transaction_type == "income",
                Transaction.date >= month_start,
                Transaction.date <= month_end,
            )
            .first()
        )
        if existing:
            logger.info("Income already processed for %s-%02d, skipping", year, month)
            return

        allocation = db.query(IncomeAllocation).first()
        if not allocation:
            logger.warning("No income allocation config found, skipping")
            return

        income_amount = Decimal(str(allocation.monthly_income_amount))
        budget_amount = Decimal(str(allocation.monthly_budget_allocation))

        # Find required categories
        income_cat = (
            db.query(Category)
            .filter(Category.type == "income", Category.is_deleted == False)  # noqa: E712
            .first()
        )
        if not income_cat:
            logger.warning("No income category found, skipping income processing")
            return

        transfer_cat = (
            db.query(Category)
            .filter(Category.type == "transfer", Category.is_deleted == False)  # noqa: E712
            .first()
        )
        if not transfer_cat:
            logger.warning("No Transfer category found, skipping income processing")
            return

        # 1. Create income transaction
        db.add(
            Transaction(
                date=date_str,
                description=f"Monthly income \u2014 {today.strftime('%B %Y')}",
                amount=income_amount,
                category_id=income_cat.id,
                type="income",
                transaction_type="income",
            )
        )

        total_allocated = Decimal("0")

        # Identify the Bills fund (handled separately)
        bills_fund = (
            db.query(SinkingFund)
            .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)  # noqa: E712
            .first()
        )
        bills_fund_id = bills_fund.id if bills_fund else None

        # 2. Distribute to sinking funds per junction table (skip Bills)
        for junction in allocation.sinking_fund_allocations:
            if junction.sinking_fund_id == bills_fund_id:
                continue

            fund = (
                db.query(SinkingFund)
                .filter(
                    SinkingFund.id == junction.sinking_fund_id,
                    SinkingFund.is_deleted == False,  # noqa: E712
                )
                .first()
            )
            if not fund:
                continue

            amount = Decimal(str(junction.allocation_amount))
            if amount <= 0:
                continue

            db.add(
                Transaction(
                    date=date_str,
                    description=f"Income allocation to {fund.name}",
                    amount=amount,
                    category_id=transfer_cat.id,
                    type="transfer",
                    transaction_type="income_allocation",
                    sinking_fund_id=fund.id,
                )
            )
            fund.current_balance = float(Decimal(str(fund.current_balance)) + amount)
            total_allocated += amount

        # 3. Handle Bills fund allocation
        if bills_fund:
            if allocation.bills_fund_allocation_type == "fixed":
                bills_amount = Decimal(str(allocation.bills_fund_fixed_amount or 0))
            else:
                bills_amount = _compute_bills_recommended(db)

            if bills_amount > 0:
                db.add(
                    Transaction(
                        date=date_str,
                        description="Income allocation to Bills fund",
                        amount=bills_amount,
                        category_id=transfer_cat.id,
                        type="transfer",
                        transaction_type="income_allocation",
                        sinking_fund_id=bills_fund.id,
                    )
                )
                bills_fund.current_balance = float(
                    Decimal(str(bills_fund.current_balance)) + bills_amount
                )
                total_allocated += bills_amount

        # 3b. Recurring transfers (money that leaves the household budget entirely)
        month_name = today.strftime("%B")
        for transfer in allocation.recurring_transfers:
            transfer_amount = Decimal(str(transfer.amount))
            db.add(
                Transaction(
                    date=date_str,
                    description=f"{transfer.description} \u2014 {month_name} {year}",
                    amount=float(transfer_amount),
                    category_id=transfer_cat.id,
                    type="expense",
                    transaction_type="income_allocation",
                )
            )
            total_allocated += transfer_amount

        # 3c. Sweep prior-month budget surplus to overflow sinking fund
        prev_month = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1

        overflow_fund = None
        if allocation.overflow_sinking_fund_id:
            overflow_fund = (
                db.query(SinkingFund)
                .filter(
                    SinkingFund.id == allocation.overflow_sinking_fund_id,
                    SinkingFund.is_deleted == False,  # noqa: E712
                )
                .first()
            )

        if overflow_fund:
            prev_budgets = (
                db.query(Budget)
                .filter(Budget.month == prev_month, Budget.year == prev_year)
                .all()
            )
            total_surplus = Decimal("0")
            for pb in prev_budgets:
                surplus = (
                    Decimal(str(pb.allocated_amount))
                    - Decimal(str(pb.spent_amount))
                    + Decimal(str(pb.fund_balance))
                )
                if surplus > 0:
                    total_surplus += surplus

            if total_surplus > 0:
                prev_month_name = calendar.month_name[prev_month]
                db.add(
                    Transaction(
                        date=date_str,
                        description=f"Budget surplus sweep \u2014 {prev_month_name} {prev_year}",
                        amount=float(total_surplus),
                        category_id=transfer_cat.id,
                        type="transfer",
                        transaction_type="contribution",
                        sinking_fund_id=overflow_fund.id,
                    )
                )
                overflow_fund.current_balance = float(
                    Decimal(str(overflow_fund.current_balance)) + total_surplus
                )

        # 4. Ensure Budget rows exist for this month

        budget_cats = (
            db.query(Category)
            .filter(
                Category.is_budget_category == True,  # noqa: E712
                Category.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        for cat in budget_cats:
            existing_budget = (
                db.query(Budget)
                .filter(
                    Budget.category_id == cat.id,
                    Budget.month == month,
                    Budget.year == year,
                )
                .first()
            )
            if not existing_budget:
                prev_budget = (
                    db.query(Budget)
                    .filter(
                        Budget.category_id == cat.id,
                        Budget.month == prev_month,
                        Budget.year == prev_year,
                    )
                    .first()
                )
                allocated = prev_budget.allocated_amount if prev_budget else 0
                db.add(
                    Budget(
                        category_id=cat.id,
                        month=month,
                        year=year,
                        allocated_amount=allocated,
                        spent_amount=0,
                        fund_balance=0,
                    )
                )

        total_allocated += budget_amount

        # 5. Record unallocated income
        unallocated = income_amount - total_allocated
        existing_unalloc = (
            db.query(MonthlyUnallocatedIncome)
            .filter(
                MonthlyUnallocatedIncome.month == month,
                MonthlyUnallocatedIncome.year == year,
            )
            .first()
        )
        if existing_unalloc:
            existing_unalloc.unallocated_amount = float(unallocated)
        else:
            db.add(
                MonthlyUnallocatedIncome(
                    month=month,
                    year=year,
                    unallocated_amount=unallocated,
                )
            )

        db.commit()
        logger.info(
            "Income allocation completed for %s-%02d: "
            "income=%s, allocated=%s, unallocated=%s",
            year,
            month,
            income_amount,
            total_allocated,
            unallocated,
        )

    except Exception:
        db.rollback()
        logger.exception("Error processing income allocation")
        raise
    finally:
        if _managed:
            db.close()


def process_due_bills(db: Session | None = None) -> None:
    """Daily task: process bills due today or overdue.

    Args:
        db: Optional database session for testing. If None, creates one
            from SessionLocal and manages its lifecycle.
    """
    _managed = db is None
    if _managed:
        db = SessionLocal()
    assert db is not None
    try:
        today = _today()
        today_str = today.isoformat()

        bills_fund = (
            db.query(SinkingFund)
            .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)  # noqa: E712
            .first()
        )
        if not bills_fund:
            logger.warning("No Bills sinking fund found, skipping bill processing")
            return

        due_bills = (
            db.query(RecurringBill)
            .filter(
                RecurringBill.is_active == True,  # noqa: E712
                RecurringBill.next_due_date <= today_str,
            )
            .all()
        )

        processed = 0
        for bill in due_bills:
            # Skip variable bills — they require manual payment
            if getattr(bill, "bill_type", "fixed") == "variable":
                continue

            # Idempotency: skip if already paid today for this bill
            existing = (
                db.query(Transaction)
                .filter(
                    Transaction.recurring_bill_id == bill.id,
                    Transaction.date == today_str,
                )
                .first()
            )
            if existing:
                continue

            amount = Decimal(str(bill.amount))

            # Dual-linkage transaction: sinking_fund_id + recurring_bill_id
            db.add(
                Transaction(
                    date=today_str,
                    description=f"Auto-payment: {bill.name} to {bill.debtor_provider}",
                    amount=amount,
                    category_id=bill.category_id,
                    type="expense",
                    transaction_type="regular",
                    sinking_fund_id=bills_fund.id,
                    recurring_bill_id=bill.id,
                )
            )

            bills_fund.current_balance = float(
                Decimal(str(bills_fund.current_balance)) - amount
            )

            # Advance next_due_date
            current_due = date.fromisoformat(bill.next_due_date)
            bill.next_due_date = advance_due_date(
                current_due, bill.frequency
            ).isoformat()
            processed += 1

        db.commit()
        if processed:
            logger.info("Processed %d due bill(s) for %s", processed, today_str)

    except Exception:
        db.rollback()
        logger.exception("Error processing due bills")
        raise
    finally:
        if _managed:
            db.close()
