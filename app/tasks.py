"""Background tasks for automated income allocation and bill processing."""

import calendar
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytz
from sqlalchemy.orm import Session

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
    """Return today's date in Australia/Brisbane timezone."""
    return datetime.now(pytz.timezone("Australia/Brisbane")).date()


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


def process_income_allocation(db: Session | None = None) -> None:
    """Monthly task (1st): distribute income per IncomeAllocation config.

    Args:
        db: Optional database session for testing. If None, creates one
            from SessionLocal and manages its lifecycle.
    """
    _managed = db is None
    if _managed:
        db = SessionLocal()
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

        expense_cat = (
            db.query(Category)
            .filter(Category.type == "expense", Category.is_deleted == False)  # noqa: E712
            .first()
        )
        if not expense_cat:
            logger.warning("No expense category found, skipping income processing")
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
                    category_id=expense_cat.id,
                    type="expense",
                    transaction_type="income_allocation",
                    sinking_fund_id=fund.id,
                )
            )
            fund.current_balance = Decimal(str(fund.current_balance)) + amount
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
                        category_id=expense_cat.id,
                        type="expense",
                        transaction_type="income_allocation",
                        sinking_fund_id=bills_fund.id,
                    )
                )
                bills_fund.current_balance = (
                    Decimal(str(bills_fund.current_balance)) + bills_amount
                )
                total_allocated += bills_amount

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
                db.add(
                    Budget(
                        category_id=cat.id,
                        month=month,
                        year=year,
                        allocated_amount=0,
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
            existing_unalloc.unallocated_amount = unallocated
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

            bills_fund.current_balance = Decimal(str(bills_fund.current_balance)) - amount

            # Advance next_due_date
            current_due = date.fromisoformat(bill.next_due_date)
            bill.next_due_date = advance_due_date(current_due, bill.frequency).isoformat()
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
