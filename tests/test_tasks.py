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
    IncomeAllocation,
    IncomeAllocationRecurringTransfer,
    IncomeAllocationToSinkingFund,
    MonthlyUnallocatedIncome,
    RecurringBill,
    SecondaryIncomeAllocation,
    SecondaryIncomeAllocationRule,
    SinkingFund,
    Transaction,
)
from app.services.bills_cover import compute_bills_cover
from app.tasks import (
    advance_due_date,
    generate_bills_forecast,
    iter_bill_due_dates,
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


class TestIterBillDueDates:
    def test_walks_with_advance_due_date(self):
        horizon_end = date(2026, 4, 15)
        walked = list(iter_bill_due_dates("2026-01-15", "28_days", horizon_end))
        expected = []
        current = date(2026, 1, 15)
        while current <= horizon_end:
            expected.append(current)
            current = advance_due_date(current, "28_days")
        assert walked == expected
        assert walked[0] == date(2026, 1, 15)
        assert walked[-1] <= horizon_end

    def test_includes_horizon_end_and_stops_after(self):
        assert list(
            iter_bill_due_dates("2026-01-15", "quarterly", date(2026, 4, 15))
        ) == [
            date(2026, 1, 15),
            date(2026, 4, 15),
        ]
        assert list(
            iter_bill_due_dates("2026-01-15", "quarterly", date(2026, 4, 14))
        ) == [
            date(2026, 1, 15),
        ]


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
        assert Decimal(str(income_txn.amount)) == Decimal(5000)
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
        assert Decimal(str(savings_txns[0].amount)) == Decimal(500)
        assert savings_txns[0].type == "transfer"
        assert savings_txns[0].category_id == income_setup["transfer_cat"].id

        # Savings fund balance increased: 1000 + 500 = 1500
        db_session.refresh(income_setup["savings_fund"])
        assert Decimal(str(income_setup["savings_fund"].current_balance)) == Decimal(
            1500
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
        assert Decimal(str(income_setup["bills_fund"].current_balance)) == Decimal(1700)

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
        assert Decimal(str(unalloc.unallocated_amount)) == Decimal(2500)

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
        assert Decimal(str(bills_txn.amount)) == Decimal(900)

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
        assert Decimal(str(budget.allocated_amount)) == Decimal(350)

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
        assert Decimal(str(budget.allocated_amount)) == Decimal(0)

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
        assert Decimal(str(transfer_txns[0].amount)) == Decimal(200)
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
        assert Decimal(str(unalloc.unallocated_amount)) == Decimal(2200)


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
        assert Decimal(str(overflow_fund.current_balance)) == Decimal(200)

        sweep_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "contribution",
                Transaction.sinking_fund_id == overflow_fund.id,
            )
            .all()
        )
        assert len(sweep_txns) == 1
        assert Decimal(str(sweep_txns[0].amount)) == Decimal(200)
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
        assert Decimal(str(overflow_fund.current_balance)) == Decimal(200)

    @patch("app.tasks._today")
    def test_overspent_category_triggers_shortfall_withdrawal(
        self, mock_today, db_session, income_setup
    ):
        """A solely overspent category withdraws the shortfall from the overflow fund."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=200
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id

        # allocated=200, spent=300 → total_surplus=-100 → withdraw 100
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
        assert Decimal(str(overflow_fund.current_balance)) == Decimal(100)

        withdrawal_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "withdrawal",
                Transaction.sinking_fund_id == overflow_fund.id,
            )
            .all()
        )
        assert len(withdrawal_txns) == 1
        assert Decimal(str(withdrawal_txns[0].amount)) == Decimal(100)

    @patch("app.tasks._today")
    def test_overspent_category_offsets_positive_surplus(
        self, mock_today, db_session, income_setup
    ):
        """Overspent categories reduce the swept amount rather than being ignored."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=0
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id

        extra_cat = Category(
            name="Dining", type="expense", color="#F59E0B", is_budget_category=True
        )
        db_session.add(extra_cat)
        db_session.flush()

        # Groceries: allocated=500, spent=400 → surplus=+100
        # Dining: allocated=200, spent=300 → surplus=-100
        # Net surplus = 0 → neither sweep nor withdrawal
        db_session.add_all(
            [
                Budget(
                    category_id=income_setup["budget_cat"].id,
                    month=1,
                    year=2026,
                    allocated_amount=500,
                    spent_amount=400,
                    fund_balance=0,
                ),
                Budget(
                    category_id=extra_cat.id,
                    month=1,
                    year=2026,
                    allocated_amount=200,
                    spent_amount=300,
                    fund_balance=0,
                ),
            ]
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        db_session.refresh(overflow_fund)
        assert Decimal(str(overflow_fund.current_balance)) == Decimal(0)

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
    def test_mixed_categories_partial_offset(
        self, mock_today, db_session, income_setup
    ):
        """Overspent categories partially reduce a net-positive surplus sweep."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=0
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id

        extra_cat = Category(
            name="Dining", type="expense", color="#F59E0B", is_budget_category=True
        )
        db_session.add(extra_cat)
        db_session.flush()

        # Groceries: allocated=500, spent=300 → surplus=+200
        # Dining: allocated=200, spent=300 → surplus=-100
        # Net surplus = 100 → sweep $100, not $200
        db_session.add_all(
            [
                Budget(
                    category_id=income_setup["budget_cat"].id,
                    month=1,
                    year=2026,
                    allocated_amount=500,
                    spent_amount=300,
                    fund_balance=0,
                ),
                Budget(
                    category_id=extra_cat.id,
                    month=1,
                    year=2026,
                    allocated_amount=200,
                    spent_amount=300,
                    fund_balance=0,
                ),
            ]
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        db_session.refresh(overflow_fund)
        assert Decimal(str(overflow_fund.current_balance)) == Decimal(100)

        sweep_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "contribution",
                Transaction.sinking_fund_id == overflow_fund.id,
            )
            .all()
        )
        assert len(sweep_txns) == 1
        assert Decimal(str(sweep_txns[0].amount)) == Decimal(100)

    @patch("app.tasks._today")
    def test_shortfall_withdrawn_from_overflow_fund(
        self, mock_today, db_session, income_setup
    ):
        """Net overspend withdraws the shortfall from the overflow fund."""
        mock_today.return_value = date(2026, 2, 1)

        overflow_fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=500
        )
        db_session.add(overflow_fund)
        db_session.flush()

        allocation = income_setup["allocation"]
        allocation.overflow_sinking_fund_id = overflow_fund.id

        # allocated=200, spent=350 → total_surplus=-150
        db_session.add(
            Budget(
                category_id=income_setup["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=200,
                spent_amount=350,
                fund_balance=0,
            )
        )
        db_session.commit()

        process_income_allocation(db=db_session)

        db_session.refresh(overflow_fund)
        assert Decimal(str(overflow_fund.current_balance)) == Decimal(350)

        withdrawal_txns = (
            db_session.query(Transaction)
            .filter(
                Transaction.transaction_type == "withdrawal",
                Transaction.sinking_fund_id == overflow_fund.id,
            )
            .all()
        )
        assert len(withdrawal_txns) == 1
        assert Decimal(str(withdrawal_txns[0].amount)) == Decimal(150)
        assert withdrawal_txns[0].type == "expense"
        assert "shortfall" in withdrawal_txns[0].description.lower()

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
        assert Decimal(str(txns[0].amount)) == Decimal(2400)
        assert txns[0].sinking_fund_id == bills_setup["bills_fund"].id

        # Fund balance decreased: 5000 - 2400 = 2600
        db_session.refresh(bills_setup["bills_fund"])
        assert Decimal(str(bills_setup["bills_fund"].current_balance)) == Decimal(2600)

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
            assert row["contribution_received"] is False
            assert row["received_contribution"] == Decimal("0.00")
            assert row["configured_contribution"] == Decimal("0.00")

    @patch("app.tasks._today")
    def test_already_contributed_shows_received_sum_without_adding_it(
        self, mock_today, db_session, forecast_setup
    ):
        """A deposit already in the balance is shown, and not added again.

        The ledger sum can differ from the configured monthly amount. The
        closing balance still starts from current_balance and adds only
        contributions that have not landed yet.
        """
        mock_today.return_value = date(2026, 3, 18)
        transfer_cat = Category(name="Transfer", type="transfer", color="#888888")
        db_session.add(transfer_cat)
        db_session.flush()

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
        bills_fund = forecast_setup["bills_fund"]
        for amount, day in ((400, "2026-03-01"), (250, "2026-03-02")):
            db_session.add(
                Transaction(
                    date=day,
                    description="Income allocation to Bills fund",
                    amount=amount,
                    category_id=transfer_cat.id,
                    type="transfer",
                    transaction_type="income_allocation",
                    sinking_fund_id=bills_fund.id,
                )
            )
        # Last month, and a different fund this month, are not this deposit.
        other = SinkingFund(name="Savings", color="#00FF00", current_balance=0)
        db_session.add(other)
        db_session.flush()
        db_session.add(
            Transaction(
                date="2026-02-01",
                description="Income allocation to Bills fund",
                amount=600,
                category_id=transfer_cat.id,
                type="transfer",
                transaction_type="income_allocation",
                sinking_fund_id=bills_fund.id,
            )
        )
        db_session.add(
            Transaction(
                date="2026-03-01",
                description="Income allocation to Savings",
                amount=900,
                category_id=transfer_cat.id,
                type="transfer",
                transaction_type="income_allocation",
                sinking_fund_id=other.id,
            )
        )
        db_session.commit()

        result = generate_bills_forecast(db_session)

        assert result[0]["contribution_received"] is True
        assert result[0]["received_contribution"] == Decimal("650.00")
        assert result[0]["contribution"] == Decimal("0.00")
        assert result[0]["configured_contribution"] == Decimal("600.00")
        # 1000 current balance already includes the deposit: 1000 + 0 - 100.
        assert result[0]["closing_balance"] == Decimal("900.00")

        for row in result[1:]:
            assert row["contribution_received"] is False
            assert row["received_contribution"] == Decimal("0.00")
            assert row["contribution"] == Decimal("600.00")
            assert row["configured_contribution"] == Decimal("600.00")
        # Next month still adds the configured amount once: 900 + 600 - 100.
        assert result[1]["closing_balance"] == Decimal("1400.00")

        cover = compute_bills_cover(
            db_session, today=date(2026, 3, 18), horizon_months=3
        )
        # March is already posted, so only 1 Apr, 1 May, and 1 Jun remain.
        assert cover.primary_in == Decimal("1800.00")
        assert cover.fund_balance == Decimal("1000.00")
        assert cover.without_cash == Decimal("2800.00")

    @patch("app.tasks._today")
    def test_not_yet_contributed_still_adds_this_month(
        self, mock_today, db_session, forecast_setup
    ):
        """No Bills deposit this month: contribution is still to come."""
        mock_today.return_value = date(2026, 3, 18)
        result = generate_bills_forecast(db_session)

        assert result[0]["contribution_received"] is False
        assert result[0]["received_contribution"] == Decimal("0.00")
        assert result[0]["contribution"] == Decimal("600.00")
        assert result[0]["configured_contribution"] == Decimal("600.00")
        # 1000 + 600, and nothing is due.
        assert result[0]["closing_balance"] == Decimal("1600.00")
        assert all(row["contribution_received"] is False for row in result)

        cover = compute_bills_cover(
            db_session, today=date(2026, 3, 18), horizon_months=3
        )
        # March has not posted, so it is included with Apr, May, and Jun.
        assert cover.primary_in == Decimal("2400.00")
        assert cover.without_cash == Decimal("3400.00")

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
        assert result[0]["configured_contribution"] == Decimal("1200.00")
        assert result[0]["contribution_received"] is False


_FORECAST_LEGACY_KEYS = (
    "month",
    "year",
    "month_name",
    "bills",
    "total_out",
    "contribution",
    "contribution_received",
    "received_contribution",
    "configured_contribution",
    "closing_balance",
)


def _legacy_forecast(rows):
    return [{key: row[key] for key in _FORECAST_LEGACY_KEYS} for row in rows]


def _add_secondary_rules(db_session, amounts_by_fund):
    alloc = SecondaryIncomeAllocation(label="Partner")
    db_session.add(alloc)
    db_session.flush()
    sort_order = 1
    for fund_id, amounts in amounts_by_fund:
        for amount in amounts:
            db_session.add(
                SecondaryIncomeAllocationRule(
                    secondary_income_allocation_id=alloc.id,
                    sinking_fund_id=fund_id,
                    goal_amount=amount,
                    sort_order=sort_order,
                )
            )
            sort_order += 1
    return alloc


def _ledger_row(db_session, forecast_setup, **fields):
    db_session.add(
        Transaction(
            description=fields.pop("description", "ledger row"),
            category_id=forecast_setup["expense_cat"].id,
            type=fields.pop("type", "transfer"),
            sinking_fund_id=fields.pop(
                "sinking_fund_id", forecast_setup["bills_fund"].id
            ),
            **fields,
        )
    )


class TestForecastSecondaryIncome:
    """Secondary inflow on the Bills forecast. Off leaves today's projection."""

    def _rich_ledger(self, db_session, forecast_setup):
        """Goal 100.10, with partial, future, surplus, and other-fund rows."""
        bills = forecast_setup["bills_fund"]
        bills.current_balance = 1000
        savings = SinkingFund(name="Savings", color="#00FF00", current_balance=0)
        db_session.add(savings)
        db_session.flush()
        _add_secondary_rules(
            db_session,
            ((bills.id, (Decimal("60.05"), Decimal("40.05"))), (savings.id, (500,))),
        )
        # Primary deposit already in the balance. Not secondary progress.
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-03-01",
            amount=80,
            transaction_type="income_allocation",
            description="Income allocation to Bills fund",
        )
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-03-04",
            amount=Decimal("25.00"),
            transaction_type="secondary_income_allocation",
        )
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-03-11",
            amount=Decimal("10.10"),
            transaction_type="secondary_income_allocation",
        )
        # Another fund's secondary receipt does not reduce the Bills goal.
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-03-11",
            amount=500,
            transaction_type="secondary_income_allocation",
            sinking_fund_id=savings.id,
        )
        # Future-dated secondary is already in the balance, so April keeps it out.
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-04-02",
            amount=Decimal("100.10"),
            transaction_type="secondary_income_allocation",
        )
        # Surplus uses income_allocation and is not secondary progress.
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-04-02",
            amount=Decimal("999.99"),
            transaction_type="income_allocation",
            description="Partner surplus to Bills",
        )
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-05-09",
            amount=Decimal("40.55"),
            transaction_type="secondary_income_allocation",
        )
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-06-01",
            amount=150,
            transaction_type="secondary_income_allocation",
        )
        db_session.commit()

    @patch("app.tasks._today")
    def test_off_matches_the_forecast_without_secondary(
        self, mock_today, db_session, forecast_setup
    ):
        mock_today.return_value = date(2026, 3, 18)
        self._rich_ledger(db_session, forecast_setup)

        off = generate_bills_forecast(db_session)
        explicit = generate_bills_forecast(db_session, include_secondary=False)

        assert _legacy_forecast(off) == _legacy_forecast(explicit)
        assert len(off) == 12
        for row in off:
            assert row["secondary"] == Decimal("0.00")
            assert row["secondary_received"] == Decimal("0.00")
        # March's primary deposit is already in the 1000 balance.
        assert off[0]["contribution"] == Decimal("0.00")
        assert off[0]["contribution_received"] is True
        assert off[0]["received_contribution"] == Decimal("80.00")
        assert off[0]["closing_balance"] == Decimal("1000.00")
        assert off[1]["contribution"] == Decimal("600.00")
        assert off[1]["closing_balance"] == Decimal("1600.00")
        # The April surplus does not change the primary "still to come" amount.
        assert off[1]["contribution_received"] is False
        assert off[4]["closing_balance"] == Decimal("3400.00")

    @patch("app.tasks._today")
    def test_on_adds_the_goal_and_only_the_unrecorded_remainder(
        self, mock_today, db_session, forecast_setup
    ):
        mock_today.return_value = date(2026, 3, 18)
        self._rich_ledger(db_session, forecast_setup)

        off = generate_bills_forecast(db_session)
        on = generate_bills_forecast(db_session, include_secondary=True)

        assert on[0]["secondary"] == Decimal("65.00")
        assert on[0]["secondary_received"] == Decimal("35.10")
        assert on[1]["secondary"] == Decimal("0.00")
        assert on[1]["secondary_received"] == Decimal("100.10")
        assert on[2]["secondary"] == Decimal("59.55")
        assert on[2]["secondary_received"] == Decimal("40.55")
        assert on[3]["secondary"] == Decimal("0.00")
        assert on[3]["secondary_received"] == Decimal("150.00")
        for row in on[4:]:
            assert row["secondary"] == Decimal("100.10")
            assert row["secondary_received"] == Decimal("0.00")

        added = Decimal("0.00")
        for before, after in zip(off, on, strict=True):
            added += after["secondary"]
            assert after["closing_balance"] == before["closing_balance"] + added
            assert after["total_out"] == before["total_out"]
            assert after["bills"] == before["bills"]
            assert after["contribution"] == before["contribution"]
            assert after["contribution_received"] == before["contribution_received"]
            assert after["received_contribution"] == before["received_contribution"]
            assert after["configured_contribution"] == before["configured_contribution"]
        assert on[0]["closing_balance"] == Decimal("1065.00")
        assert on[1]["closing_balance"] == Decimal("1665.00")
        assert on[2]["closing_balance"] == Decimal("2324.55")
        assert on[4]["closing_balance"] == Decimal("3624.65")

    @patch("app.tasks._today")
    def test_no_goal_or_missing_bills_fund_adds_nothing(
        self, mock_today, db_session, forecast_setup
    ):
        mock_today.return_value = date(2026, 3, 18)
        bills = forecast_setup["bills_fund"]
        bills.current_balance = 1000
        savings = SinkingFund(name="Savings", color="#00FF00", current_balance=0)
        db_session.add(savings)
        db_session.flush()
        _add_secondary_rules(db_session, ((savings.id, (400,)),))
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-03-04",
            amount=50,
            transaction_type="secondary_income_allocation",
        )
        db_session.commit()

        untouched = generate_bills_forecast(db_session)
        no_bills_goal = generate_bills_forecast(db_session, include_secondary=True)
        assert _legacy_forecast(no_bills_goal) == _legacy_forecast(untouched)
        assert all(row["secondary"] == Decimal("0.00") for row in no_bills_goal)
        assert all(
            row["secondary_received"] == Decimal("0.00") for row in no_bills_goal
        )

        alloc = db_session.query(SecondaryIncomeAllocation).one()
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=bills.id,
                goal_amount=80,
                sort_order=2,
            )
        )
        db_session.commit()
        aimed_at_bills = generate_bills_forecast(db_session, include_secondary=True)
        assert aimed_at_bills[0]["secondary"] == Decimal("30.00")

        bills.name = "Household"
        db_session.commit()
        renamed = generate_bills_forecast(db_session, include_secondary=True)
        renamed_off = generate_bills_forecast(db_session)
        assert _legacy_forecast(renamed) == _legacy_forecast(renamed_off)
        # No fund named Bills: the stored balance is not the starting point.
        assert renamed[0]["closing_balance"] == Decimal("600.00")
        assert all(row["secondary"] == Decimal("0.00") for row in renamed)
        assert all(row["secondary_received"] == Decimal("0.00") for row in renamed)

        bills.name = "Bills"
        bills.is_deleted = True
        db_session.commit()
        deleted = generate_bills_forecast(db_session, include_secondary=True)
        deleted_off = generate_bills_forecast(db_session)
        assert _legacy_forecast(deleted) == _legacy_forecast(deleted_off)
        assert deleted[0]["closing_balance"] == Decimal("600.00")
        assert all(row["secondary"] == Decimal("0.00") for row in deleted)
        assert all(row["secondary_received"] == Decimal("0.00") for row in deleted)

    @patch("app.tasks._today")
    def test_lower_goal_does_not_claw_back_the_balance(
        self, mock_today, db_session, forecast_setup
    ):
        mock_today.return_value = date(2026, 3, 18)
        bills = forecast_setup["bills_fund"]
        bills.current_balance = 500
        _add_secondary_rules(db_session, ((bills.id, (40,)),))
        _ledger_row(
            db_session,
            forecast_setup,
            date="2026-03-02",
            amount=100,
            transaction_type="secondary_income_allocation",
        )
        db_session.commit()

        result = generate_bills_forecast(db_session, include_secondary=True)
        assert result[0]["secondary"] == Decimal("0.00")
        assert result[0]["secondary_received"] == Decimal("100.00")
        # 500 already includes the 100. The extra above the new goal stays put.
        assert result[0]["closing_balance"] == Decimal("1100.00")
        assert result[1]["secondary"] == Decimal("40.00")
        assert result[1]["closing_balance"] == Decimal("1740.00")


class TestRecurringPaymentRules:
    def test_weekly_and_fortnightly_steps_and_multipliers(self):
        from app.schemas import FREQUENCY_ANNUAL_MULTIPLIER

        assert advance_due_date(date(2026, 1, 31), "weekly") == date(2026, 2, 7)
        assert advance_due_date(date(2026, 1, 31), "fortnightly") == date(2026, 2, 14)
        assert FREQUENCY_ANNUAL_MULTIPLIER["weekly"] == Decimal("52.143")
        assert FREQUENCY_ANNUAL_MULTIPLIER["fortnightly"] == Decimal("26.071")
        assert FREQUENCY_ANNUAL_MULTIPLIER["28_days"] == Decimal("13.036")

    def test_iter_stops_after_end_date(self):
        dates = list(
            iter_bill_due_dates("2026-01-15", "weekly", date(2026, 2, 28), "2026-01-20")
        )
        assert dates == [date(2026, 1, 15)]

    @patch("app.tasks._today")
    def test_scheduler_ignores_budget_rows_and_end_date(
        self, mock_today, db_session, bills_setup
    ):
        mock_today.return_value = date(2026, 2, 1)
        budget_bill = RecurringBill(
            name="HelloFresh",
            amount=80,
            debtor_provider="HelloFresh",
            start_date="2026-01-01",
            frequency="weekly",
            category_id=bills_setup["expense_cat"].id,
            next_due_date="2026-01-20",
            paid_from="budget",
        )
        ended = RecurringBill(
            name="Old power",
            amount=50,
            debtor_provider="Power",
            start_date="2025-01-01",
            frequency="monthly",
            category_id=bills_setup["expense_cat"].id,
            next_due_date="2026-02-01",
            end_date="2026-01-15",
            paid_from="bills_fund",
        )
        db_session.add_all([budget_bill, ended])
        db_session.commit()
        process_due_bills(db=db_session)
        paid_ids = {
            row.recurring_bill_id for row in db_session.query(Transaction).all()
        }
        assert bills_setup["bill_due"].id in paid_ids
        assert budget_bill.id not in paid_ids
        assert ended.id not in paid_ids
        db_session.refresh(ended)
        assert ended.next_due_date == "2026-02-01"

    @patch("app.tasks._today")
    def test_forecast_and_recommended_ignore_other_sources(
        self, mock_today, db_session, forecast_setup
    ):
        from app.tasks import _compute_bills_recommended

        mock_today.return_value = date(2026, 3, 18)
        db_session.add(
            RecurringBill(
                name="Groceries box",
                amount=100,
                debtor_provider="Shop",
                start_date="2026-01-01",
                frequency="monthly",
                category_id=forecast_setup["expense_cat"].id,
                next_due_date="2026-03-20",
                paid_from="budget",
            )
        )
        kept = RecurringBill(
            name="Power",
            amount=40,
            debtor_provider="Grid",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=forecast_setup["expense_cat"].id,
            next_due_date="2026-03-20",
            paid_from="bills_fund",
        )
        db_session.add(kept)
        db_session.commit()
        result = generate_bills_forecast(db_session)
        names = [bill["name"] for row in result for bill in row["bills"]]
        assert "Power" in names
        assert "Groceries box" not in names
        # 40 * 12 / 12 = 40
        assert _compute_bills_recommended(db_session) == Decimal("40.00")

    @patch("app.tasks._today")
    def test_overdue_variable_bills_land_in_this_month(
        self, mock_today, db_session, forecast_setup
    ):
        mock_today.return_value = date(2026, 3, 18)
        db_session.add_all(
            [
                RecurringBill(
                    name="Water",
                    amount=30,
                    debtor_provider="Utility",
                    start_date="2026-01-01",
                    frequency="monthly",
                    category_id=forecast_setup["expense_cat"].id,
                    next_due_date="2026-02-10",
                    bill_type="variable",
                    paid_from="bills_fund",
                ),
                RecurringBill(
                    name="Rent",
                    amount=100,
                    debtor_provider="Landlord",
                    start_date="2026-01-01",
                    frequency="monthly",
                    category_id=forecast_setup["expense_cat"].id,
                    next_due_date="2026-02-10",
                    bill_type="fixed",
                    paid_from="bills_fund",
                ),
            ]
        )
        db_session.commit()
        plain = generate_bills_forecast(db_session)
        assert all(not bill.get("overdue") for row in plain for bill in row["bills"])
        caught = generate_bills_forecast(db_session, include_overdue=True)
        overdue = [bill for bill in caught[0]["bills"] if bill.get("overdue")]
        assert [bill["name"] for bill in overdue] == ["Water"]
        assert caught[0]["total_out"] == plain[0]["total_out"] + Decimal("30.00")


class TestOccurrenceAutoPay:
    def _patch(self, monkeypatch, holder):
        monkeypatch.setattr("app.tasks._today", lambda: holder["day"])
        monkeypatch.setattr(
            "app.services.recurring_payments._today", lambda: holder["day"]
        )

    def _bill(self, db, bills_setup, **kwargs):
        values = {
            "name": "Power",
            "amount": 100,
            "debtor_provider": "Grid",
            "start_date": "2026-01-01",
            "frequency": "monthly",
            "category_id": bills_setup["expense_cat"].id,
            "next_due_date": "2026-02-01",
            "paid_from": "bills_fund",
            "bill_type": "fixed",
        }
        values.update(kwargs)
        bill = RecurringBill(**values)
        db.add(bill)
        db.commit()
        db.refresh(bill)
        return bill

    def test_twice_the_same_day_is_one_payment(
        self, db_session, bills_setup, monkeypatch
    ):
        holder = {"day": date(2026, 2, 1)}
        self._patch(monkeypatch, holder)
        start = Decimal(str(bills_setup["bills_fund"].current_balance))
        process_due_bills(db=db_session)
        process_due_bills(db=db_session)
        db_session.rollback()
        rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bills_setup["bill_due"].id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].occurrence_due_date == "2026-02-01"
        db_session.refresh(bills_setup["bills_fund"])
        assert Decimal(
            str(bills_setup["bills_fund"].current_balance)
        ) == start - Decimal(2400)
        db_session.refresh(bills_setup["bill_due"])
        assert bills_setup["bill_due"].next_due_date == "2026-03-01"

    def test_two_periods_overdue_pays_one_per_day(
        self, db_session, bills_setup, monkeypatch
    ):
        holder = {"day": date(2026, 4, 1)}
        self._patch(monkeypatch, holder)
        bill = self._bill(
            db_session, bills_setup, next_due_date="2026-02-01", amount=30
        )
        process_due_bills(db=db_session)
        process_due_bills(db=db_session)
        db_session.rollback()
        rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].occurrence_due_date == "2026-02-01"
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-03-01"
        holder["day"] = date(2026, 4, 2)
        process_due_bills(db=db_session)
        db_session.rollback()
        rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .order_by(Transaction.id)
            .all()
        )
        assert [row.occurrence_due_date for row in rows] == ["2026-02-01", "2026-03-01"]
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-04-01"

    def test_hand_entered_row_does_not_stop_auto_pay(
        self, authed_client, db_session, bills_setup, monkeypatch
    ):
        holder = {"day": date(2026, 2, 1)}
        self._patch(monkeypatch, holder)
        bill = bills_setup["bill_due"]
        other = Category(name="Other", type="expense", color="#123456")
        db_session.add(other)
        db_session.commit()
        created = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-02-01",
                "amount": "15.00",
                "category_id": bills_setup["expense_cat"].id,
                "type": "expense",
                "transaction_type": "regular",
                "sinking_fund_id": bills_setup["bills_fund"].id,
                "recurring_bill_id": bill.id,
                "description": "Bank debit",
            },
        )
        assert created.status_code == 201
        hand_id = created.json()["id"]
        assert created.json()["occurrence_due_date"] is None
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-02-01"
        process_due_bills(db=db_session)
        db_session.rollback()
        auto = (
            db_session.query(Transaction)
            .filter(
                Transaction.recurring_bill_id == bill.id,
                Transaction.occurrence_due_date.isnot(None),
            )
            .one()
        )
        assert auto.occurrence_due_date == "2026-02-01"
        edited = authed_client.put(
            f"/api/transactions/{hand_id}",
            json={
                "amount": "18.00",
                "sinking_fund_id": bills_setup["bills_fund"].id,
                "recurring_bill_id": bill.id,
                "description": "Still a note",
            },
        )
        assert edited.status_code == 200
        db_session.refresh(db_session.get(Transaction, hand_id))
        hand = db_session.get(Transaction, hand_id)
        assert hand.occurrence_due_date is None
        assert Decimal(str(hand.amount)) == Decimal("18.00")
        assert hand.recurring_bill_id == bill.id
        assert hand.sinking_fund_id == bills_setup["bills_fund"].id

    def test_startup_and_cron_the_same_day_pay_once(
        self, db_session, bills_setup, monkeypatch
    ):
        holder = {"day": date(2026, 2, 1)}
        self._patch(monkeypatch, holder)
        process_due_bills(db=db_session)
        process_due_bills(db=db_session)
        db_session.rollback()
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bills_setup["bill_due"].id)
            .count()
            == 1
        )

    def test_garbage_cursor_fails_one_bill(self, db_session, bills_setup, monkeypatch):
        holder = {"day": date(2026, 2, 1)}
        self._patch(monkeypatch, holder)
        bad = self._bill(
            db_session, bills_setup, name="Bad", next_due_date="not-a-date"
        )
        good = self._bill(
            db_session, bills_setup, name="Good", next_due_date="2026-02-01", amount=80
        )
        start = Decimal(5000)
        # The fixture bill_due is also due and will be paid. Use a fresh fund balance read.
        process_due_bills(db=db_session)
        db_session.rollback()
        db_session.refresh(bad)
        assert bad.next_due_date == "not-a-date"
        good_rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == good.id)
            .all()
        )
        assert len(good_rows) == 1
        assert good_rows[0].occurrence_due_date == "2026-02-01"
        db_session.refresh(bills_setup["bills_fund"])
        # 2400 fixture bill + 80 good bill. Bad bill moved nothing.
        assert Decimal(
            str(bills_setup["bills_fund"].current_balance)
        ) == start - Decimal(2480)

    def test_insert_failure_keeps_the_earlier_bill(
        self, db_session, bills_setup, monkeypatch
    ):
        holder = {"day": date(2026, 2, 1)}
        self._patch(monkeypatch, holder)
        first = self._bill(
            db_session, bills_setup, name="First", next_due_date="2026-02-01", amount=10
        )
        second = self._bill(
            db_session,
            bills_setup,
            name="Second",
            next_due_date="2026-02-01",
            amount=20,
        )
        # Silence the fixture bill so the balance assertion is about these two.
        bills_setup["bill_due"].next_due_date = "2026-05-01"
        db_session.commit()

        def boom(bill):
            if bill.id == second.id:
                raise RuntimeError("insert failed")

        monkeypatch.setattr("app.services.recurring_payments.payment_insert_hook", boom)
        process_due_bills(db=db_session)
        db_session.rollback()
        first_rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == first.id)
            .all()
        )
        assert len(first_rows) == 1
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == second.id)
            .count()
            == 0
        )
        db_session.refresh(bills_setup["bills_fund"])
        assert Decimal(str(bills_setup["bills_fund"].current_balance)) == Decimal(4990)
        db_session.refresh(second)
        assert second.next_due_date == "2026-02-01"

    def test_delete_auto_payment_then_manual_pay_same_day(
        self, authed_client, db_session, bills_setup, monkeypatch
    ):
        holder = {"day": date(2026, 2, 1)}
        self._patch(monkeypatch, holder)
        bill = self._bill(
            db_session, bills_setup, next_due_date="2026-02-01", amount=50, name="Phone"
        )
        bills_setup["bill_due"].is_active = False
        db_session.commit()
        start = Decimal(str(bills_setup["bills_fund"].current_balance))
        process_due_bills(db=db_session)
        db_session.rollback()
        auto = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .one()
        )
        deleted = authed_client.delete(f"/api/transactions/{auto.id}")
        assert deleted.status_code == 200
        db_session.refresh(bills_setup["bills_fund"])
        assert Decimal(str(bills_setup["bills_fund"].current_balance)) == start
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-03-01"
        process_due_bills(db=db_session)
        db_session.rollback()
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .count()
            == 0
        )
        edited = authed_client.put(
            f"/api/bills/{bill.id}",
            json={
                "next_due_date": "2026-02-01",
                "expected_next_due_date": "2026-03-01",
            },
        )
        assert edited.status_code == 200
        paid = authed_client.post(
            f"/api/bills/{bill.id}/pay",
            json={"amount": "50.00", "date": "2026-02-01"},
        )
        assert paid.status_code == 200
        assert paid.json()["transaction"]["occurrence_due_date"] == "2026-02-01"
        process_due_bills(db=db_session)
        db_session.rollback()
        rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].description.startswith("Bill payment:")

    def test_unknown_frequency_fails_only_that_bill(
        self, db_session, bills_setup, monkeypatch
    ):
        """The 06:00 job and the boot catch-up both call process_due_bills."""
        from app.scheduler import register_jobs
        from app.schemas import UnknownFrequencyError

        recorded: list[tuple] = []

        class _Recorder:
            def add_job(self, func, trigger, **kwargs):
                recorded.append((kwargs.get("id"), func))

        register_jobs(_Recorder())
        by_id = dict(recorded)
        assert by_id["bill_processing"] is process_due_bills
        assert by_id["bill_processing_catchup"] is process_due_bills

        holder = {"day": date(2026, 2, 1)}
        self._patch(monkeypatch, holder)
        bills_setup["bill_due"].is_active = False
        bad = self._bill(
            db_session,
            bills_setup,
            name="Mystery",
            frequency="nope",
            next_due_date="2026-02-01",
            amount=70,
        )
        good = self._bill(
            db_session,
            bills_setup,
            name="Known",
            next_due_date="2026-02-01",
            amount=15,
        )
        with pytest.raises(UnknownFrequencyError):
            advance_due_date(date(2026, 2, 1), "nope")
        start = Decimal(str(bills_setup["bills_fund"].current_balance))
        process_due_bills(db=db_session)
        db_session.rollback()
        db_session.refresh(bad)
        db_session.refresh(good)
        assert bad.next_due_date == "2026-02-01"
        assert good.next_due_date == "2026-03-01"
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bad.id)
            .count()
            == 0
        )
        paid = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == good.id)
            .one()
        )
        assert paid.occurrence_due_date == "2026-02-01"
        db_session.refresh(bills_setup["bills_fund"])
        assert Decimal(
            str(bills_setup["bills_fund"].current_balance)
        ) == start - Decimal(15)
