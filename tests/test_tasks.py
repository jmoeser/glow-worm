"""Tests for background tasks (income allocation and bill processing)."""

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
    IncomeAllocation,
    IncomeAllocationRecurringTransfer,
    IncomeAllocationToSinkingFund,
    SinkingFund,
    Transaction,
)
from app.tasks import (
    advance_due_date,
    generate_bills_forecast,
    process_due_bills,
    process_income_allocation,
)


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
def income_setup(db_session):
    """Set up a complete income allocation scenario."""
    income_cat = Category(name="Salary", type="income", color="#00FF00")
    expense_cat = Category(name="Bills", type="expense", color="#FF0000")
    transfer_cat = Category(name="Transfer", type="transfer", color="#6B7280")
    budget_cat = Category(
        name="Groceries", type="expense", color="#22C55E", is_budget_category=True
    )
    db_session.add_all([income_cat, expense_cat, transfer_cat, budget_cat])
    db_session.flush()

    bills_fund = SinkingFund(name="Bills", color="#FF0000", current_balance=500)
    savings_fund = SinkingFund(name="Savings", color="#00FF00", current_balance=1000)
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

    allocation = IncomeAllocation(
        monthly_income_amount=5000,
        monthly_budget_allocation=800,
        bills_fund_allocation_type="recommended",
    )
    db_session.add(allocation)
    db_session.flush()

    # Junction: allocate $500 to Savings fund
    junction = IncomeAllocationToSinkingFund(
        income_allocation_id=allocation.id,
        sinking_fund_id=savings_fund.id,
        allocation_amount=500,
    )
    db_session.add(junction)
    db_session.commit()

    return {
        "income_cat": income_cat,
        "expense_cat": expense_cat,
        "transfer_cat": transfer_cat,
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

    bills_fund = SinkingFund(name="Bills", color="#FF0000", current_balance=5000)
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
# process_income_allocation
# ---------------------------------------------------------------------------


class TestProcessIncomeAllocation:
    @patch("app.tasks._today")
    def test_happy_path(self, mock_today, db_session, income_setup):
        mock_today.return_value = date(2026, 2, 1)

        process_income_allocation(db=db_session)

        # Income transaction created
        income_txn = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "income")
            .first()
        )
        assert income_txn is not None
        assert Decimal(str(income_txn.amount)) == Decimal("5000")
        assert income_txn.type == "income"

        # Savings fund allocation transaction
        savings_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "income_allocation",
                Transaction.sinking_fund_id == income_setup["savings_fund"].id,
            )
            .all()
        )
        assert len(savings_txns) == 1
        assert Decimal(str(savings_txns[0].amount)) == Decimal("500")
        assert savings_txns[0].type == "transfer"
        assert savings_txns[0].category_id == income_setup["transfer_cat"].id

        # Savings fund balance increased: 1000 + 500 = 1500
        db_session.refresh(income_setup["savings_fund"])
        assert Decimal(str(income_setup["savings_fund"].current_balance)) == Decimal(
            "1500"
        )

        # Bills fund allocation (recommended = 1200 * 12 / 12 = 1200.00)
        bills_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "income_allocation",
                Transaction.sinking_fund_id == income_setup["bills_fund"].id,
            )
            .all()
        )
        assert len(bills_txns) == 1
        assert Decimal(str(bills_txns[0].amount)) == Decimal("1200.00")
        assert bills_txns[0].type == "transfer"
        assert bills_txns[0].category_id == income_setup["transfer_cat"].id

        # Bills fund balance increased: 500 + 1200 = 1700
        db_session.refresh(income_setup["bills_fund"])
        assert Decimal(str(income_setup["bills_fund"].current_balance)) == Decimal(
            "1700"
        )

        # Budget row created for Groceries
        budget = (
            db_session.query(Budget)
            .filter(
                Budget.category_id == income_setup["budget_cat"].id,
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
    def test_idempotent(self, mock_today, db_session, income_setup):
        mock_today.return_value = date(2026, 2, 1)

        process_income_allocation(db=db_session)
        process_income_allocation(db=db_session)

        income_txns = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "income")
            .all()
        )
        assert len(income_txns) == 1

    @patch("app.tasks._today")
    def test_no_config(self, mock_today, db_session):
        mock_today.return_value = date(2026, 2, 1)

        process_income_allocation(db=db_session)

        txns = db_session.query(Transaction).all()
        assert len(txns) == 0

    @patch("app.tasks._today")
    def test_fixed_bills_allocation(self, mock_today, db_session, income_setup):
        mock_today.return_value = date(2026, 3, 1)

        # Switch to fixed allocation
        alloc = db_session.query(IncomeAllocation).first()
        alloc.bills_fund_allocation_type = "fixed"
        alloc.bills_fund_fixed_amount = 900
        db_session.commit()

        process_income_allocation(db=db_session)

        bills_txn = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "income_allocation",
                Transaction.sinking_fund_id == income_setup["bills_fund"].id,
            )
            .first()
        )
        assert bills_txn is not None
        assert Decimal(str(bills_txn.amount)) == Decimal("900")

    @patch("app.tasks._today")
    def test_budget_carries_forward_allocated_amount(
        self, mock_today, db_session, income_setup
    ):
        """Budget allocated_amount should be copied from previous month's budget."""
        # Seed a January budget row with a known allocated amount
        db_session.add(
            Budget(
                category_id=income_setup["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=350,
                spent_amount=0,
                fund_balance=0,
            )
        )
        db_session.commit()

        mock_today.return_value = date(2026, 2, 1)
        process_income_allocation(db=db_session)

        budget = (
            db_session.query(Budget)
            .filter(
                Budget.category_id == income_setup["budget_cat"].id,
                Budget.month == 2,
                Budget.year == 2026,
            )
            .first()
        )
        assert budget is not None
        assert Decimal(str(budget.allocated_amount)) == Decimal("350")

    @patch("app.tasks._today")
    def test_budget_defaults_to_zero_when_no_previous_month(
        self, mock_today, db_session, income_setup
    ):
        """Budget allocated_amount defaults to 0 when no prior month budget exists."""
        mock_today.return_value = date(2026, 2, 1)
        process_income_allocation(db=db_session)

        budget = (
            db_session.query(Budget)
            .filter(
                Budget.category_id == income_setup["budget_cat"].id,
                Budget.month == 2,
                Budget.year == 2026,
            )
            .first()
        )
        assert budget is not None
        assert Decimal(str(budget.allocated_amount)) == Decimal("0")

    @patch("app.tasks._today")
    def test_process_income_allocation_creates_transfer_transactions(
        self, mock_today, db_session, income_setup
    ):
        """Recurring transfers produce expense transactions with income_allocation type."""
        mock_today.return_value = date(2026, 2, 1)

        allocation = income_setup["allocation"]
        db_session.add(
            IncomeAllocationRecurringTransfer(
                income_allocation_id=allocation.id,
                description="Monthly transfer out",
                amount=200,
            )
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        transfer_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "income_allocation",
                Transaction.type == "expense",
            )
            .all()
        )
        assert len(transfer_txns) == 1
        assert Decimal(str(transfer_txns[0].amount)) == Decimal("200")
        assert "Monthly transfer out" in transfer_txns[0].description
        assert transfer_txns[0].category_id == income_setup["transfer_cat"].id

    @patch("app.tasks._today")
    def test_transfer_amount_deducted_from_unallocated(
        self, mock_today, db_session, income_setup
    ):
        """Recurring transfer amounts reduce the unallocated remainder."""
        mock_today.return_value = date(2026, 2, 1)

        allocation = income_setup["allocation"]
        db_session.add(
            IncomeAllocationRecurringTransfer(
                income_allocation_id=allocation.id,
                description="External savings",
                amount=300,
            )
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        # Without transfer: 5000 - 500 (savings) - 1200 (bills) - 800 (budget) = 2500
        # With $300 transfer: 2500 - 300 = 2200
        unalloc = (
            db_session.query(MonthlyUnallocatedIncome)
            .filter(
                MonthlyUnallocatedIncome.month == 2,
                MonthlyUnallocatedIncome.year == 2026,
            )
            .first()
        )
        assert unalloc is not None
        assert Decimal(str(unalloc.unallocated_amount)) == Decimal("2200")


# ---------------------------------------------------------------------------
# surplus sweep
# ---------------------------------------------------------------------------


class TestBudgetSurplusSweep:
    @patch("app.tasks._today")
    def test_surplus_swept_to_overflow_fund(self, mock_today, db_session, income_setup):
        """Prior-month budget surplus is contributed to the overflow fund."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=0
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id

        # January budget: allocated=500, spent=300, fund_balance=0 → surplus=200
        db_session.add(
            Budget(
                category_id=income_setup["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=500,
                spent_amount=300,
                fund_balance=0,
            )
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        db_session.refresh(overflow_fund)
        assert Decimal(str(overflow_fund.current_balance)) == Decimal("200")

        sweep_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "contribution",
                Transaction.sinking_fund_id == overflow_fund.id,
            )
            .all()
        )
        assert len(sweep_txns) == 1
        assert Decimal(str(sweep_txns[0].amount)) == Decimal("200")
        assert "surplus sweep" in sweep_txns[0].description.lower()
        assert sweep_txns[0].type == "transfer"

    @patch("app.tasks._today")
    def test_surplus_includes_fund_balance(self, mock_today, db_session, income_setup):
        """Effective surplus includes existing fund_balance."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=0
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id

        # allocated=500, spent=400, fund_balance=100 → surplus = 100 + 100 = 200
        db_session.add(
            Budget(
                category_id=income_setup["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=500,
                spent_amount=400,
                fund_balance=100,
            )
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        db_session.refresh(overflow_fund)
        assert Decimal(str(overflow_fund.current_balance)) == Decimal("200")

    @patch("app.tasks._today")
    def test_overspent_category_excluded(self, mock_today, db_session, income_setup):
        """Overspent categories (negative surplus) are not included in the sweep."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=0
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id

        # Category overspent: allocated=200, spent=300 → surplus=-100 (excluded)
        db_session.add(
            Budget(
                category_id=income_setup["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=200,
                spent_amount=300,
                fund_balance=0,
            )
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        db_session.refresh(overflow_fund)
        assert Decimal(str(overflow_fund.current_balance)) == Decimal("0")

        sweep_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "contribution",
                Transaction.sinking_fund_id == overflow_fund.id,
            )
            .all()
        )
        assert len(sweep_txns) == 0

    @patch("app.tasks._today")
    def test_no_sweep_when_overflow_fund_not_configured(
        self, mock_today, db_session, income_setup
    ):
        """No sweep occurs when overflow_sinking_fund_id is None."""
        mock_today.return_value = date(2026, 2, 1)

        db_session.add(
            Budget(
                category_id=income_setup["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=500,
                spent_amount=300,
                fund_balance=0,
            )
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        contribution_txns = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "contribution")
            .all()
        )
        assert len(contribution_txns) == 0

    @patch("app.tasks._today")
    def test_no_sweep_when_total_surplus_is_zero(
        self, mock_today, db_session, income_setup
    ):
        """No sweep transaction is created when all categories are fully spent."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=0
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id

        # Exactly spent: surplus=0
        db_session.add(
            Budget(
                category_id=income_setup["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=500,
                spent_amount=500,
                fund_balance=0,
            )
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        sweep_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "contribution",
                Transaction.sinking_fund_id == overflow_fund.id,
            )
            .all()
        )
        assert len(sweep_txns) == 0

    @patch("app.tasks._today")
    def test_no_prev_budgets_no_sweep(self, mock_today, db_session, income_setup):
        """No sweep occurs when there are no prior-month budget rows."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=0
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id
        db_session.commit()

        process_income_allocation(db=db_session)

        sweep_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "contribution",
                Transaction.sinking_fund_id == overflow_fund.id,
            )
            .all()
        )
        assert len(sweep_txns) == 0


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


# ---------------------------------------------------------------------------
# generate_bills_forecast
# ---------------------------------------------------------------------------


@pytest.fixture
def forecast_setup(db_session):
    """Minimal setup for forecast tests: Bills fund + income allocation."""
    expense_cat = Category(name="Bills", type="expense", color="#FF0000")
    db_session.add(expense_cat)
    db_session.flush()

    bills_fund = SinkingFund(name="Bills", color="#FF0000", current_balance=1000)
    db_session.add(bills_fund)
    db_session.flush()

    allocation = IncomeAllocation(
        monthly_income_amount=5000,
        monthly_budget_allocation=800,
        bills_fund_allocation_type="fixed",
        bills_fund_fixed_amount=600,
    )
    db_session.add(allocation)
    db_session.commit()

    return {
        "expense_cat": expense_cat,
        "bills_fund": bills_fund,
        "allocation": allocation,
    }


class TestGenerateBillsForecast:
    @patch("app.tasks._today")
    def test_returns_12_months(self, mock_today, db_session, forecast_setup):
        mock_today.return_value = date(2026, 3, 18)
        result = generate_bills_forecast(db_session)
        assert len(result) == 12

    @patch("app.tasks._today")
    def test_month_sequence(self, mock_today, db_session, forecast_setup):
        mock_today.return_value = date(2026, 3, 18)
        result = generate_bills_forecast(db_session)
        assert result[0]["month"] == 3
        assert result[0]["year"] == 2026
        assert result[0]["month_name"] == "March"
        assert result[9]["month"] == 12
        assert result[9]["year"] == 2026
        assert result[10]["month"] == 1
        assert result[10]["year"] == 2027
        assert result[11]["month"] == 2
        assert result[11]["year"] == 2027

    @patch("app.tasks._today")
    def test_monthly_bill_appears_every_month(
        self, mock_today, db_session, forecast_setup
    ):
        mock_today.return_value = date(2026, 3, 18)
        bill = RecurringBill(
            name="Electricity",
            amount=120,
            debtor_provider="Energy Co",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=forecast_setup["expense_cat"].id,
            next_due_date="2026-03-25",
        )
        db_session.add(bill)
        db_session.commit()

        result = generate_bills_forecast(db_session)

        for row in result:
            assert len(row["bills"]) == 1, (
                f"Expected bill in {row['month_name']} {row['year']}"
            )
            assert row["bills"][0]["name"] == "Electricity"
            assert row["total_out"] == Decimal("120.00")

    @patch("app.tasks._today")
    def test_yearly_bill_appears_once(self, mock_today, db_session, forecast_setup):
        mock_today.return_value = date(2026, 3, 18)
        bill = RecurringBill(
            name="Car Registration",
            amount=800,
            debtor_provider="RTA",
            start_date="2026-01-01",
            frequency="yearly",
            category_id=forecast_setup["expense_cat"].id,
            next_due_date="2026-06-01",
        )
        db_session.add(bill)
        db_session.commit()

        result = generate_bills_forecast(db_session)

        months_with_bills = [r for r in result if r["bills"]]
        assert len(months_with_bills) == 1
        assert months_with_bills[0]["month"] == 6

    @patch("app.tasks._today")
    def test_closing_balance_calculation(self, mock_today, db_session, forecast_setup):
        """Balance = previous closing + contribution - bills."""
        mock_today.return_value = date(2026, 3, 18)
        bill = RecurringBill(
            name="Internet",
            amount=100,
            debtor_provider="ISP",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=forecast_setup["expense_cat"].id,
            next_due_date="2026-03-25",
        )
        db_session.add(bill)
        db_session.commit()

        result = generate_bills_forecast(db_session)

        # Month 0: 1000 + 600 - 100 = 1500
        assert result[0]["closing_balance"] == Decimal("1500.00")
        # Month 1: 1500 + 600 - 100 = 2000
        assert result[1]["closing_balance"] == Decimal("2000.00")

    @patch("app.tasks._today")
    def test_no_bills_fund_defaults_zero_balance(self, mock_today, db_session):
        """No Bills fund → starts from zero."""
        mock_today.return_value = date(2026, 3, 18)
        result = generate_bills_forecast(db_session)
        assert result[0]["closing_balance"] == Decimal("0.00")

    @patch("app.tasks._today")
    def test_no_allocation_zero_contribution(
        self, mock_today, db_session, forecast_setup
    ):
        db_session.delete(forecast_setup["allocation"])
        db_session.commit()

        mock_today.return_value = date(2026, 3, 18)
        result = generate_bills_forecast(db_session)
        for row in result:
            assert row["contribution"] == Decimal("0.00")

    @patch("app.tasks._today")
    def test_contribution_skipped_if_already_processed(
        self, mock_today, db_session, forecast_setup
    ):
        """If the bills fund allocation already ran this month, month 0 contribution is $0."""
        mock_today.return_value = date(2026, 3, 18)
        transfer_cat = Category(name="Transfer", type="transfer", color="#888888")
        db_session.add(transfer_cat)
        db_session.flush()

        # Simulate this month's allocation already deposited
        txn = Transaction(
            date="2026-03-01",
            description="Income allocation to Bills fund",
            amount=600,
            category_id=transfer_cat.id,
            type="transfer",
            transaction_type="income_allocation",
            sinking_fund_id=forecast_setup["bills_fund"].id,
        )
        db_session.add(txn)
        db_session.commit()

        result = generate_bills_forecast(db_session)
        assert result[0]["contribution"] == Decimal("0.00")
        assert result[1]["contribution"] == Decimal("600.00")

    @patch("app.tasks._today")
    def test_inactive_bill_excluded(self, mock_today, db_session, forecast_setup):
        mock_today.return_value = date(2026, 3, 18)
        bill = RecurringBill(
            name="Old Service",
            amount=50,
            debtor_provider="Old Co",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=forecast_setup["expense_cat"].id,
            next_due_date="2026-03-25",
            is_active=False,
        )
        db_session.add(bill)
        db_session.commit()

        result = generate_bills_forecast(db_session)
        assert all(len(r["bills"]) == 0 for r in result)

    @patch("app.tasks._today")
    def test_recommended_allocation_type(self, mock_today, db_session, forecast_setup):
        """Bills fund allocation type 'recommended' uses annual cost / 12."""
        mock_today.return_value = date(2026, 3, 18)
        forecast_setup["allocation"].bills_fund_allocation_type = "recommended"
        forecast_setup["allocation"].bills_fund_fixed_amount = None
        bill = RecurringBill(
            name="Rent",
            amount=1200,
            debtor_provider="Landlord",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=forecast_setup["expense_cat"].id,
            next_due_date="2026-03-25",
        )
        db_session.add(bill)
        db_session.commit()

        result = generate_bills_forecast(db_session)
        # 1200/mo * 12 = 14400/yr / 12 = 1200/mo recommended
        assert result[1]["contribution"] == Decimal("1200.00")
