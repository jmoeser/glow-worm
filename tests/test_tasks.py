"""Tests for background tasks (salary allocation and bill processing)."""

import os

os.environ["DATABASE_URL"] = "sqlite:///./test-glow-worm.db"

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models import (
    Budget,
    Category,
    MonthlyUnallocatedIncome,
    RecurringBill,
    SalaryAllocation,
    SalaryAllocationToSinkingFund,
    SinkingFund,
    Transaction,
)
from app.tasks import advance_due_date, process_due_bills, process_salary_allocation


# ---------------------------------------------------------------------------
# advance_due_date
# ---------------------------------------------------------------------------


class TestAdvanceDueDate:
    def test_monthly(self):
        assert advance_due_date(date(2026, 1, 15), "monthly") == date(2026, 2, 15)

    def test_monthly_clamp_to_shorter_month(self):
        # Jan 31 -> Feb 28 (non-leap year 2026)
        assert advance_due_date(date(2026, 1, 31), "monthly") == date(2026, 2, 28)

    def test_monthly_leap_year(self):
        # Jan 31 -> Feb 29 (leap year 2028)
        assert advance_due_date(date(2028, 1, 31), "monthly") == date(2028, 2, 29)

    def test_monthly_december_to_january(self):
        assert advance_due_date(date(2026, 12, 15), "monthly") == date(2027, 1, 15)

    def test_quarterly(self):
        assert advance_due_date(date(2026, 1, 15), "quarterly") == date(2026, 4, 15)

    def test_quarterly_wrap_year(self):
        assert advance_due_date(date(2026, 11, 15), "quarterly") == date(2027, 2, 15)

    def test_quarterly_clamp(self):
        # Nov 30 + 3 months = Feb 28 (non-leap 2027)
        assert advance_due_date(date(2026, 11, 30), "quarterly") == date(2027, 2, 28)

    def test_yearly(self):
        assert advance_due_date(date(2026, 3, 15), "yearly") == date(2027, 3, 15)

    def test_yearly_leap_day(self):
        # Feb 29, 2028 (leap) -> Feb 28, 2029 (non-leap)
        assert advance_due_date(date(2028, 2, 29), "yearly") == date(2029, 2, 28)

    def test_28_days(self):
        assert advance_due_date(date(2026, 1, 1), "28_days") == date(2026, 1, 29)

    def test_28_days_crosses_month(self):
        assert advance_due_date(date(2026, 1, 15), "28_days") == date(2026, 2, 12)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def salary_setup(db_session):
    """Set up a complete salary allocation scenario."""
    income_cat = Category(name="Salary", type="income", color="#00FF00")
    expense_cat = Category(name="Bills", type="expense", color="#FF0000")
    budget_cat = Category(
        name="Groceries", type="expense", color="#22C55E", is_budget_category=True
    )
    db_session.add_all([income_cat, expense_cat, budget_cat])
    db_session.flush()

    bills_fund = SinkingFund(
        name="Bills", color="#FF0000", monthly_allocation=0, current_balance=500
    )
    savings_fund = SinkingFund(
        name="Savings", color="#00FF00", monthly_allocation=200, current_balance=1000
    )
    db_session.add_all([bills_fund, savings_fund])
    db_session.flush()

    # Recurring bill for recommended calculation: $1200/mo -> annual $14400 -> rec $1200
    bill = RecurringBill(
        name="Rent",
        amount=1200,
        debtor_provider="Landlord",
        start_date="2026-01-01",
        frequency="monthly",
        category_id=expense_cat.id,
        next_due_date="2026-02-01",
    )
    db_session.add(bill)
    db_session.flush()

    allocation = SalaryAllocation(
        monthly_salary_amount=5000,
        monthly_budget_allocation=800,
        bills_fund_allocation_type="recommended",
    )
    db_session.add(allocation)
    db_session.flush()

    # Junction: allocate $500 to Savings fund
    junction = SalaryAllocationToSinkingFund(
        salary_allocation_id=allocation.id,
        sinking_fund_id=savings_fund.id,
        allocation_amount=500,
    )
    db_session.add(junction)
    db_session.commit()

    return {
        "income_cat": income_cat,
        "expense_cat": expense_cat,
        "budget_cat": budget_cat,
        "bills_fund": bills_fund,
        "savings_fund": savings_fund,
        "bill": bill,
        "allocation": allocation,
    }


@pytest.fixture
def bills_setup(db_session):
    """Set up a bill processing scenario."""
    expense_cat = Category(name="Bills", type="expense", color="#FF0000")
    db_session.add(expense_cat)
    db_session.flush()

    bills_fund = SinkingFund(
        name="Bills", color="#FF0000", monthly_allocation=0, current_balance=5000
    )
    db_session.add(bills_fund)
    db_session.flush()

    bill_due = RecurringBill(
        name="Rent",
        amount=2400,
        debtor_provider="Landlord",
        start_date="2026-01-01",
        frequency="monthly",
        category_id=expense_cat.id,
        next_due_date="2026-02-01",
    )
    bill_future = RecurringBill(
        name="Insurance",
        amount=600,
        debtor_provider="Insurer",
        start_date="2026-01-01",
        frequency="quarterly",
        category_id=expense_cat.id,
        next_due_date="2026-04-01",
    )
    db_session.add_all([bill_due, bill_future])
    db_session.commit()

    return {
        "expense_cat": expense_cat,
        "bills_fund": bills_fund,
        "bill_due": bill_due,
        "bill_future": bill_future,
    }


# ---------------------------------------------------------------------------
# process_salary_allocation
# ---------------------------------------------------------------------------


class TestProcessSalaryAllocation:
    @patch("app.tasks._today")
    def test_happy_path(self, mock_today, db_session, salary_setup):
        mock_today.return_value = date(2026, 2, 1)

        process_salary_allocation(db=db_session)

        # Salary income transaction created
        salary_txn = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "salary")
            .first()
        )
        assert salary_txn is not None
        assert Decimal(str(salary_txn.amount)) == Decimal("5000")
        assert salary_txn.type == "income"

        # Savings fund allocation transaction
        savings_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "salary_allocation",
                Transaction.sinking_fund_id == salary_setup["savings_fund"].id,
            )
            .all()
        )
        assert len(savings_txns) == 1
        assert Decimal(str(savings_txns[0].amount)) == Decimal("500")

        # Savings fund balance increased: 1000 + 500 = 1500
        db_session.refresh(salary_setup["savings_fund"])
        assert Decimal(str(salary_setup["savings_fund"].current_balance)) == Decimal(
            "1500"
        )

        # Bills fund allocation (recommended = 1200 * 12 / 12 = 1200.00)
        bills_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "salary_allocation",
                Transaction.sinking_fund_id == salary_setup["bills_fund"].id,
            )
            .all()
        )
        assert len(bills_txns) == 1
        assert Decimal(str(bills_txns[0].amount)) == Decimal("1200.00")

        # Bills fund balance increased: 500 + 1200 = 1700
        db_session.refresh(salary_setup["bills_fund"])
        assert Decimal(str(salary_setup["bills_fund"].current_balance)) == Decimal(
            "1700"
        )

        # Budget row created for Groceries
        budget = (
            db_session.query(Budget)
            .filter(
                Budget.category_id == salary_setup["budget_cat"].id,
                Budget.month == 2,
                Budget.year == 2026,
            )
            .first()
        )
        assert budget is not None

        # Unallocated income: 5000 - 500 - 1200 - 800 = 2500
        unalloc = (
            db_session.query(MonthlyUnallocatedIncome)
            .filter(
                MonthlyUnallocatedIncome.month == 2,
                MonthlyUnallocatedIncome.year == 2026,
            )
            .first()
        )
        assert unalloc is not None
        assert Decimal(str(unalloc.unallocated_amount)) == Decimal("2500")

    @patch("app.tasks._today")
    def test_idempotent(self, mock_today, db_session, salary_setup):
        mock_today.return_value = date(2026, 2, 1)

        process_salary_allocation(db=db_session)
        process_salary_allocation(db=db_session)

        salary_txns = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "salary")
            .all()
        )
        assert len(salary_txns) == 1

    @patch("app.tasks._today")
    def test_no_config(self, mock_today, db_session):
        mock_today.return_value = date(2026, 2, 1)

        process_salary_allocation(db=db_session)

        txns = db_session.query(Transaction).all()
        assert len(txns) == 0

    @patch("app.tasks._today")
    def test_fixed_bills_allocation(self, mock_today, db_session, salary_setup):
        mock_today.return_value = date(2026, 3, 1)

        # Switch to fixed allocation
        alloc = db_session.query(SalaryAllocation).first()
        alloc.bills_fund_allocation_type = "fixed"
        alloc.bills_fund_fixed_amount = 900
        db_session.commit()

        process_salary_allocation(db=db_session)

        bills_txn = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "salary_allocation",
                Transaction.sinking_fund_id == salary_setup["bills_fund"].id,
            )
            .first()
        )
        assert bills_txn is not None
        assert Decimal(str(bills_txn.amount)) == Decimal("900")


# ---------------------------------------------------------------------------
# process_due_bills
# ---------------------------------------------------------------------------


class TestProcessDueBills:
    @patch("app.tasks._today")
    def test_processes_due_bill(self, mock_today, db_session, bills_setup):
        mock_today.return_value = date(2026, 2, 1)

        process_due_bills(db=db_session)

        # Transaction created for due bill
        txns = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bills_setup["bill_due"].id)
            .all()
        )
        assert len(txns) == 1
        assert Decimal(str(txns[0].amount)) == Decimal("2400")
        assert txns[0].sinking_fund_id == bills_setup["bills_fund"].id

        # Fund balance decreased: 5000 - 2400 = 2600
        db_session.refresh(bills_setup["bills_fund"])
        assert Decimal(str(bills_setup["bills_fund"].current_balance)) == Decimal(
            "2600"
        )

        # next_due_date advanced: 2026-02-01 + monthly = 2026-03-01
        db_session.refresh(bills_setup["bill_due"])
        assert bills_setup["bill_due"].next_due_date == "2026-03-01"

    @patch("app.tasks._today")
    def test_skips_future_bill(self, mock_today, db_session, bills_setup):
        mock_today.return_value = date(2026, 2, 1)

        process_due_bills(db=db_session)

        # No transaction for future bill (due 2026-04-01)
        txns = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bills_setup["bill_future"].id)
            .all()
        )
        assert len(txns) == 0

    @patch("app.tasks._today")
    def test_idempotent(self, mock_today, db_session, bills_setup):
        mock_today.return_value = date(2026, 2, 1)

        process_due_bills(db=db_session)
        process_due_bills(db=db_session)

        txns = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bills_setup["bill_due"].id)
            .all()
        )
        assert len(txns) == 1

    @patch("app.tasks._today")
    def test_no_bills_fund(self, mock_today, db_session):
        mock_today.return_value = date(2026, 2, 1)

        # No Bills fund exists
        process_due_bills(db=db_session)

        txns = db_session.query(Transaction).all()
        assert len(txns) == 0

    @patch("app.tasks._today")
    def test_overdue_bill(self, mock_today, db_session, bills_setup):
        """Bills overdue by several days should still be processed."""
        mock_today.return_value = date(2026, 2, 5)

        process_due_bills(db=db_session)

        txns = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bills_setup["bill_due"].id)
            .all()
        )
        assert len(txns) == 1
        assert txns[0].date == "2026-02-05"

        # next_due_date advanced from the original due date, not from today
        db_session.refresh(bills_setup["bill_due"])
        assert bills_setup["bill_due"].next_due_date == "2026-03-01"
