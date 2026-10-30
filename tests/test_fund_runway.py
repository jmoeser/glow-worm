"""Fund runway and debt payoff from the last complete months."""

import logging
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models import (
    Budget,
    Category,
    IncomeAllocation,
    IncomeAllocationToSinkingFund,
    RecurringBill,
    SecondaryIncomeAllocation,
    SecondaryIncomeAllocationRule,
    SinkingFund,
    Transaction,
)
from app.routes.income import _record_secondary_income
from app.services.fund_runway import balance_direction, compute_fund_runway
from app.tasks import process_due_bills, process_income_allocation

TODAY = date(2026, 10, 29)
OLD = datetime(2020, 1, 1)


def _fund(db, balance, *, name="Holiday", created_at=OLD, deleted=False):
    fund = SinkingFund(
        name=name,
        color="#336699",
        current_balance=balance,
        is_deleted=deleted,
        created_at=created_at,
    )
    db.add(fund)
    db.commit()
    db.refresh(fund)
    return fund


def _txn(db, category_id, fund, **kwargs):
    txn = Transaction(
        date=kwargs.pop("date", "2026-05-15"),
        description=kwargs.pop("description", "row"),
        category_id=category_id,
        sinking_fund_id=fund.id,
        type=kwargs.pop("type", "expense"),
        transaction_type=kwargs.pop("transaction_type", "regular"),
        is_paid=kwargs.pop("is_paid", True),
        **kwargs,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def _run(db, fund, **kwargs):
    kwargs.setdefault("today", TODAY)
    return compute_fund_runway(db, fund, **kwargs)


def _cats(db):
    income = Category(name="Salary", type="income", color="#00FF00")
    transfer = Category(name="Transfer", type="transfer", color="#888888")
    expense = Category(name="Groceries", type="expense", color="#22C55E")
    db.add_all([income, transfer, expense])
    db.commit()
    return income, transfer, expense


class TestWindowDirectionHistory:
    def test_window_is_last_six_complete_months(self, db_session, sample_category):
        fund = _fund(db_session, 5000)
        _txn(db_session, sample_category.id, fund, date="2026-03-31", amount=9)
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-04-01",
            amount=10,
            type="income",
        )
        _txn(db_session, sample_category.id, fund, date="2026-09-30", amount=20)
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-10-01",
            amount=30,
            type="transfer",
        )

        result = _run(db_session, fund)
        assert result.window_start == date(2026, 4, 1)
        assert result.window_end == date(2026, 9, 30)
        assert result.months_used == 6
        assert result.total_in == Decimal("10.00")
        assert result.total_out == Decimal("20.00")

        january = compute_fund_runway(db_session, fund, today=date(2027, 1, 15))
        assert january.window_start == date(2026, 7, 1)
        assert january.window_end == date(2026, 12, 31)
        assert january.total_in == Decimal("30.00")
        assert january.total_out == Decimal("20.00")

    def test_window_months_override_shrinks_the_window(
        self, db_session, sample_category
    ):
        fund = _fund(db_session, 100)
        _txn(db_session, sample_category.id, fund, date="2026-06-30", amount=50)
        _txn(db_session, sample_category.id, fund, date="2026-07-01", amount=70)
        result = _run(db_session, fund, window_months=3)
        assert result.window_start == date(2026, 7, 1)
        assert result.window_end == date(2026, 9, 30)
        assert result.total_out == Decimal("70.00")

    def test_current_partial_month_is_excluded(self, db_session, sample_category):
        fund = _fund(db_session, 5000)
        _txn(db_session, sample_category.id, fund, date="2026-04-10", amount=600)
        _txn(db_session, sample_category.id, fund, date="2026-10-29", amount=5000)
        result = _run(db_session, fund)
        assert result.total_out == Decimal("600.00")
        assert result.avg_out == Decimal("100.00")

    def test_direction_follows_type_not_transaction_type(
        self, db_session, sample_category
    ):
        fund = _fund(db_session, 5000)
        rows = [
            ("income", "regular", 10),
            ("transfer", "income_allocation", 20),
            ("transfer", "secondary_income_allocation", 30),
            ("transfer", "contribution", 40),
            ("expense", "withdrawal", 5),
            ("expense", "budget_transfer", 7),
            ("expense", "regular", 9),
        ]
        for txn_type, transaction_type, amount in rows:
            _txn(
                db_session,
                sample_category.id,
                fund,
                date="2026-04-02",
                amount=amount,
                type=txn_type,
                transaction_type=transaction_type,
            )
            assert balance_direction(txn_type) == (
                1 if txn_type in ("income", "transfer") else -1
            )

        result = _run(db_session, fund)
        assert result.total_in == Decimal("100.00")
        assert result.total_out == Decimal("21.00")

    def test_primary_allocation_and_surplus_sweep_are_inflows(self, db_session):
        _cats(db_session)
        savings = _fund(db_session, 1000, name="Savings")
        overflow = _fund(db_session, 0, name="Overflow")
        budget_cat = Category(
            name="Groceries",
            type="expense",
            color="#22C55E",
            is_budget_category=True,
        )
        db_session.add(budget_cat)
        db_session.flush()
        allocation = IncomeAllocation(
            monthly_income_amount=5000,
            monthly_budget_allocation=0,
            bills_fund_allocation_type="fixed",
            bills_fund_fixed_amount=0,
            overflow_sinking_fund_id=overflow.id,
        )
        db_session.add(allocation)
        db_session.flush()
        db_session.add(
            IncomeAllocationToSinkingFund(
                income_allocation_id=allocation.id,
                sinking_fund_id=savings.id,
                allocation_amount=500,
            )
        )
        db_session.add(
            Budget(
                category_id=budget_cat.id,
                month=5,
                year=2026,
                allocated_amount=200,
                spent_amount=80,
                fund_balance=0,
            )
        )
        db_session.commit()

        with patch("app.tasks._today", return_value=date(2026, 6, 1)):
            process_income_allocation(db=db_session)

        savings_runway = _run(db_session, savings)
        overflow_runway = _run(db_session, overflow)
        assert savings_runway.total_in == Decimal("500.00")
        assert savings_runway.total_out == Decimal("0.00")
        assert savings_runway.net_burn < 0
        assert overflow_runway.total_in == Decimal("120.00")
        assert overflow_runway.total_out == Decimal("0.00")
        assert overflow_runway.net_burn < 0

    def test_budget_shortfall_is_an_outflow(self, db_session):
        _cats(db_session)
        overflow = _fund(db_session, 1000, name="Overflow")
        budget_cat = Category(
            name="Groceries",
            type="expense",
            color="#22C55E",
            is_budget_category=True,
        )
        db_session.add(budget_cat)
        db_session.flush()
        allocation = IncomeAllocation(
            monthly_income_amount=5000,
            monthly_budget_allocation=0,
            bills_fund_allocation_type="fixed",
            bills_fund_fixed_amount=0,
            overflow_sinking_fund_id=overflow.id,
        )
        db_session.add(allocation)
        db_session.flush()
        db_session.add(
            Budget(
                category_id=budget_cat.id,
                month=5,
                year=2026,
                allocated_amount=100,
                spent_amount=250,
                fund_balance=0,
            )
        )
        db_session.commit()

        with patch("app.tasks._today", return_value=date(2026, 6, 1)):
            process_income_allocation(db=db_session)

        result = _run(db_session, overflow)
        assert result.total_out == Decimal("150.00")
        assert result.total_in == Decimal("0.00")
        assert result.net_burn > 0

    def test_secondary_allocation_and_surplus_are_inflows(self, db_session):
        _cats(db_session)
        goal = _fund(db_session, 0, name="Goal")
        overflow = _fund(db_session, 0, name="Rainy day")
        alloc = SecondaryIncomeAllocation(
            label="Side", overflow_sinking_fund_id=overflow.id
        )
        db_session.add(alloc)
        db_session.flush()
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=goal.id,
                goal_amount=200,
                sort_order=1,
            )
        )
        db_session.commit()

        _record_secondary_income(db_session, Decimal("250.00"), "2026-05-01", "Side")

        goal_runway = _run(db_session, goal)
        overflow_runway = _run(db_session, overflow)
        assert goal_runway.total_in == Decimal("200.00")
        assert goal_runway.total_out == Decimal("0.00")
        assert goal_runway.net_burn < 0
        assert overflow_runway.total_in == Decimal("50.00")
        assert overflow_runway.total_out == Decimal("0.00")
        assert overflow_runway.net_burn < 0

    def test_bill_autopay_row_is_an_outflow(self, db_session):
        """The Bills page shows no burn figure, so the sign is the row itself."""
        expense = Category(name="Power", type="expense", color="#FF0000")
        db_session.add(expense)
        db_session.flush()
        bills = _fund(db_session, 5000, name="Bills")
        db_session.add(
            RecurringBill(
                name="Rent",
                amount=80,
                debtor_provider="Landlord",
                start_date="2026-01-01",
                frequency="monthly",
                category_id=expense.id,
                next_due_date="2026-06-15",
            )
        )
        db_session.commit()

        with patch("app.tasks._today", return_value=date(2026, 6, 15)):
            process_due_bills(db=db_session)

        txn = (
            db_session.query(Transaction)
            .filter(Transaction.sinking_fund_id == bills.id)
            .one()
        )
        assert txn.type == "expense"
        assert txn.transaction_type == "regular"
        assert balance_direction(txn.type) == -1
        assert _run(db_session, bills).state == "bills"

    def test_interfund_transfer_burns_source_and_feeds_destination(
        self, authed_client, db_session, transfer_category
    ):
        source = _fund(db_session, 2000, name="Source")
        dest = _fund(db_session, 100, name="Dest")
        response = authed_client.post(
            "/transactions/fund-transfer",
            data={
                "date": "2026-05-15",
                "amount": "600.00",
                "from_fund_id": str(source.id),
                "to_fund_id": str(dest.id),
                "month": "5",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200

        db_session.expire_all()
        source = db_session.query(SinkingFund).filter_by(id=source.id).one()
        dest = db_session.query(SinkingFund).filter_by(id=dest.id).one()
        src = _run(db_session, source)
        dst = _run(db_session, dest)
        assert src.total_out == Decimal("600.00")
        assert src.total_in == Decimal("0.00")
        assert dst.total_in == Decimal("600.00")
        assert dst.total_out == Decimal("0.00")
        assert src.net_burn == Decimal("100.00")
        assert dst.net_burn == Decimal("-100.00")

    def test_unpaid_rows_are_counted(self, db_session, sample_category):
        fund = _fund(db_session, 1000)
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-04-04",
            amount=60,
            is_paid=False,
        )
        result = _run(db_session, fund)
        assert result.total_out == Decimal("60.00")

    def test_empty_months_count_as_zero(self, db_session, sample_category):
        fund = _fund(db_session, 5000)
        _txn(db_session, sample_category.id, fund, date="2026-04-04", amount=600)
        result = _run(db_session, fund)
        assert result.months_used == 6
        assert result.avg_out == Decimal("100.00")

    def test_young_fund_uses_complete_months_only(self, db_session, sample_category):
        fund = _fund(db_session, 1000, created_at=datetime(2026, 7, 15))
        _txn(db_session, sample_category.id, fund, date="2026-08-15", amount=400)
        result = _run(db_session, fund)
        assert result.months_used == 2
        assert result.window_start == date(2026, 8, 1)
        assert result.avg_out == Decimal("200.00")
        assert result.detail is not None
        assert "Last 2 months" in result.detail
        assert "(fund is newer than 6 months)" in result.detail

    def test_created_on_the_first_counts_that_month(self, db_session, sample_category):
        """1 Aug is inside Apr–Sep, so August and September both count (N = 2).

        The checklist says N = 3. That includes the current month, which §5.2
        excludes. A fund created on 2 Aug drops August and has only September.
        """
        on_the_first = _fund(
            db_session, 1000, name="First", created_at=datetime(2026, 8, 1)
        )
        _txn(
            db_session, sample_category.id, on_the_first, date="2026-08-15", amount=600
        )
        counted = _run(db_session, on_the_first)
        assert counted.months_used == 2
        assert counted.window_start == date(2026, 8, 1)
        assert counted.total_out == Decimal("600.00")

        day_after = _fund(
            db_session, 1000, name="Second", created_at=datetime(2026, 8, 2)
        )
        _txn(db_session, sample_category.id, day_after, date="2026-08-15", amount=600)
        skipped = _run(db_session, day_after)
        assert skipped.months_used == 1
        assert skipped.state == "too_new"

    def test_too_new_when_fewer_than_two_full_months(self, db_session, sample_category):
        """10 Sep starts counting in October, and October is the current month.

        The checklist says N = 1. §5.2 excludes October, so N = 0. Either way
        the fund is too new to show a number.
        """
        mid_september = _fund(db_session, 100, created_at=datetime(2026, 9, 10))
        result = _run(db_session, mid_september)
        assert result.months_used == 0
        assert result.state == "too_new"
        assert result.headline == (
            "Runway shows once this fund has 2 full months of history."
        )
        assert result.is_warning is False
        assert result.detail is None

        this_month = _fund(
            db_session, 100, name="New", created_at=datetime(2026, 10, 10)
        )
        assert _run(db_session, this_month).state == "too_new"
        on_the_first = _fund(
            db_session, 100, name="Opened", created_at=datetime(2026, 10, 1)
        )
        assert _run(db_session, on_the_first).months_used == 0
        assert _run(db_session, on_the_first).state == "too_new"

    def test_backdated_row_extends_history(self, db_session, sample_category):
        young = _fund(db_session, 1000, name="Young", created_at=datetime(2026, 8, 15))
        assert _run(db_session, young).months_used == 1

        fund = _fund(
            db_session, 1000, name="Backdated", created_at=datetime(2026, 8, 15)
        )
        _txn(db_session, sample_category.id, fund, date="2026-04-01", amount=600)
        result = _run(db_session, fund)
        assert result.months_used == 6
        assert result.window_start == date(2026, 4, 1)
        assert result.total_out == Decimal("600.00")

    def test_naive_created_at_is_treated_as_utc(self, db_session):
        # 1 Aug 2026 20:00 UTC is 2 Aug in Brisbane, so August is partial.
        fund = _fund(db_session, 100, created_at=datetime(2026, 8, 1, 20, 0))
        result = _run(db_session, fund)
        assert result.months_used == 1
        assert result.state == "too_new"

    def test_unparseable_earliest_date_is_ignored(
        self, db_session, sample_category, caplog
    ):
        fund = _fund(db_session, 100, created_at=datetime(2026, 8, 15))
        _txn(db_session, sample_category.id, fund, date="0000-bad", amount=10)
        with caplog.at_level(logging.DEBUG, logger="app.services.fund_runway"):
            result = _run(db_session, fund)
        assert result.months_used == 1
        assert result.state == "too_new"
        assert "Ignoring unparseable transaction date" in caplog.text

    def test_future_dated_row_is_outside_the_rate(self, db_session, sample_category):
        fund = _fund(db_session, 1000)
        _txn(db_session, sample_category.id, fund, date="2026-09-15", amount=100)
        _txn(db_session, sample_category.id, fund, date="2026-11-01", amount=9999)
        result = _run(db_session, fund)
        assert result.total_out == Decimal("100.00")

    def test_rows_on_other_funds_are_ignored(self, db_session, sample_category):
        fund = _fund(db_session, 1000, name="This")
        other = _fund(db_session, 1000, name="Other")
        _txn(db_session, sample_category.id, fund, date="2026-05-01", amount=40)
        _txn(db_session, sample_category.id, other, date="2026-05-01", amount=400)
        result = _run(db_session, fund)
        assert result.total_out == Decimal("40.00")


class TestPositiveBalance:
    def test_running_down_example(self, db_session, sample_category):
        fund = _fund(db_session, 2000)
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-04-02",
            amount=1800,
            type="income",
        )
        _txn(db_session, sample_category.id, fund, date="2026-05-02", amount=6600)
        result = _run(db_session, fund)
        assert result.state == "running_down"
        assert result.net_burn == Decimal("800.00")
        assert result.runway_months == Decimal("2.5")
        assert result.run_out_date == date(2027, 1, 13)
        assert result.is_warning is True
        assert result.is_capped is False
        assert result.months_to_zero is None
        assert result.run_out_date is not None
        assert result.headline == (
            "At recent spending, this fund runs out in about 2 months "
            "(around January 2027)."
        )
        assert result.detail == (
            "Last 6 months: $300 in, $1,100 out a month on average."
        )

    def test_warning_threshold(self, db_session, sample_category):
        steady = _fund(db_session, 600, name="Steady")
        _txn(db_session, sample_category.id, steady, date="2026-04-02", amount=600)
        at_six = _run(db_session, steady)
        assert at_six.runway_months == Decimal("6.0")
        assert at_six.is_warning is False
        assert "about 6 months" in at_six.headline

        short = _fund(db_session, 590, name="Short")
        _txn(db_session, sample_category.id, short, date="2026-04-02", amount=600)
        under = _run(db_session, short)
        assert under.runway_months == Decimal("5.9")
        assert under.is_warning is True
        assert "about 5 months" in under.headline

    def test_less_than_a_month_and_one_month_singular(
        self, db_session, sample_category
    ):
        soon = _fund(db_session, 50, name="Soon")
        _txn(db_session, sample_category.id, soon, date="2026-04-02", amount=600)
        under = _run(db_session, soon)
        assert under.is_warning is True
        assert under.headline == (
            "At recent spending, this fund runs out in less than a month."
        )

        one = _fund(db_session, 100, name="One")
        _txn(db_session, sample_category.id, one, date="2026-04-02", amount=600)
        month = _run(db_session, one)
        assert month.runway_months == Decimal("1.0")
        assert month.is_warning is True
        assert "about 1 month" in month.headline
        assert "1 months" not in month.headline

    def test_runway_cap(self, db_session, sample_category):
        huge = _fund(db_session, 12001, name="Huge")
        _txn(db_session, sample_category.id, huge, date="2026-04-02", amount=600)
        capped = _run(db_session, huge)
        assert capped.is_capped is True
        assert capped.is_warning is False
        assert capped.run_out_date is None
        assert capped.headline == (
            "At recent spending, this fund lasts more than 10 years."
        )

        exact = _fund(db_session, 12000, name="Exact")
        _txn(db_session, sample_category.id, exact, date="2026-04-02", amount=600)
        at_cap = _run(db_session, exact)
        assert at_cap.is_capped is False
        assert at_cap.run_out_date is not None
        assert "about 120 months" in at_cap.headline

    def test_growing(self, db_session, sample_category):
        fund = _fund(db_session, 500)
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-04-02",
            amount=1200,
            type="transfer",
            transaction_type="contribution",
        )
        _txn(db_session, sample_category.id, fund, date="2026-05-02", amount=600)
        result = _run(db_session, fund)
        assert result.state == "growing"
        assert result.net_burn < 0
        assert result.runway_months is None
        assert result.run_out_date is None
        assert result.is_warning is False
        assert result.headline == (
            "This fund is growing: contributions have covered spending "
            "over the last 6 months."
        )
        assert result.detail == ("Last 6 months: $200 in, $100 out a month on average.")

    def test_flat(self, db_session, sample_category):
        fund = _fund(db_session, 40)
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-04-02",
            amount=600,
            type="income",
        )
        _txn(db_session, sample_category.id, fund, date="2026-05-02", amount=600)
        result = _run(db_session, fund)
        assert result.state == "flat"
        assert result.net_burn == Decimal("0.00")
        assert result.detail is None
        assert result.headline == (
            "Money in has matched money out over the last 6 months."
        )

    def test_no_activity(self, db_session):
        fund = _fund(db_session, 40)
        result = _run(db_session, fund)
        assert result.state == "no_activity"
        assert result.detail is None
        assert result.headline == "No money in or out over the last 6 months."

    def test_empty_while_burning_and_growing_at_zero(self, db_session, sample_category):
        empty = _fund(db_session, 0, name="Empty")
        _txn(db_session, sample_category.id, empty, date="2026-04-02", amount=600)
        burning = _run(db_session, empty)
        assert burning.state == "empty"
        assert burning.is_warning is True
        assert burning.headline == (
            "This fund is empty and more is going out than coming in."
        )
        assert burning.detail is not None

        filling = _fund(db_session, 0, name="Filling")
        _txn(
            db_session,
            sample_category.id,
            filling,
            date="2026-04-02",
            amount=600,
            type="income",
        )
        growing = _run(db_session, filling)
        assert growing.state == "growing"
        assert growing.is_warning is False

    def test_detail_line_keeps_cents(self, db_session, sample_category):
        fund = _fund(db_session, 2000)
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-04-02",
            amount=Decimal("1803.00"),
            type="income",
        )
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-05-02",
            amount=Decimal("6603.00"),
        )
        result = _run(db_session, fund)
        assert result.detail == (
            "Last 6 months: $300.50 in, $1,100.50 out a month on average."
        )


class TestDebt:
    def _payoff(self, db, category_id, balance, total_in, name="Loan"):
        fund = _fund(db, balance, name=name)
        if total_in:
            _txn(
                db,
                category_id,
                fund,
                date="2026-04-02",
                amount=total_in,
                type="transfer",
                transaction_type="contribution",
            )
        return _run(db, fund)

    def test_paying_down(self, db_session, sample_category):
        result = self._payoff(db_session, sample_category.id, -1500, 2400)
        assert result.state == "payoff"
        assert result.net_inflow == Decimal("400.00")
        assert result.months_to_zero == Decimal("3.8")
        assert result.back_to_zero_date == date(2027, 2, 20)
        assert result.is_warning is False
        assert result.is_capped is False
        assert result.runway_months is None
        assert result.run_out_date is None
        assert result.headline == (
            "At recent repayments, this fund is back to zero in about 4 months "
            "(around February 2027)."
        )
        assert result.detail == ("Last 6 months: $400 in, $0 out a month on average.")

    def test_rounding_up(self, db_session, sample_category):
        just_over = self._payoff(db_session, sample_category.id, -301, 600, name="A")
        assert just_over.months_to_zero == Decimal("3.1")
        assert "about 4 months" in just_over.headline

        exact = self._payoff(db_session, sample_category.id, -400, 600, name="B")
        assert exact.months_to_zero == Decimal("4.0")
        assert "about 4 months" in exact.headline

        soon = self._payoff(db_session, sample_category.id, -30, 600, name="C")
        assert soon.months_to_zero == Decimal("0.3")
        assert "about 1 month" in soon.headline
        assert "1 months" not in soon.headline

    def test_payoff_cap(self, db_session, sample_category):
        capped = self._payoff(db_session, sample_category.id, -12001, 600, name="Long")
        assert capped.state == "payoff"
        assert capped.is_capped is True
        assert capped.is_warning is False
        assert capped.back_to_zero_date is None
        assert capped.headline == (
            "At recent repayments, this fund is back to zero in more than 10 years."
        )

        exact = self._payoff(db_session, sample_category.id, -12000, 600, name="Ten")
        assert exact.is_capped is False
        assert exact.back_to_zero_date is not None
        assert "about 120 months" in exact.headline

    def test_flat_debt(self, db_session, sample_category):
        fund = _fund(db_session, -1500, name="Flat debt")
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-04-02",
            amount=600,
            type="income",
        )
        _txn(db_session, sample_category.id, fund, date="2026-05-02", amount=600)
        result = _run(db_session, fund)
        assert result.state == "debt_stuck"
        assert result.is_warning is True
        assert result.headline == (
            "This fund is $1,500 in the red and isn't being paid down at the moment."
        )
        assert result.detail is not None

    def test_worsening_debt(self, db_session, sample_category):
        fund = _fund(db_session, -1500, name="Worse")
        _txn(
            db_session,
            sample_category.id,
            fund,
            date="2026-04-02",
            amount=100,
            type="income",
        )
        _txn(db_session, sample_category.id, fund, date="2026-05-02", amount=400)
        result = _run(db_session, fund)
        assert result.state == "debt_stuck"
        assert result.is_warning is True
        assert result.net_inflow < 0
        assert result.detail is not None

    def test_debt_with_no_activity_omits_the_detail_line(self, db_session):
        fund = _fund(db_session, -1500, name="Quiet debt")
        result = _run(db_session, fund)
        assert result.state == "debt_stuck"
        assert result.is_warning is True
        assert result.detail is None
        assert "$1,500 in the red" in result.headline

    def test_debt_with_short_history_is_too_new(self, db_session):
        fund = _fund(db_session, -500, created_at=datetime(2026, 9, 10))
        result = _run(db_session, fund)
        assert result.state == "too_new"
        assert result.is_warning is False
        assert result.headline == (
            "Runway shows once this fund has 2 full months of history."
        )

    def test_debt_copy_never_uses_runway_words(self, db_session, sample_category):
        cases = [
            self._payoff(db_session, sample_category.id, -1500, 2400, name="Pay"),
            self._payoff(db_session, sample_category.id, -200, 0, name="Stuck"),
            _run(
                db_session,
                _fund(
                    db_session,
                    -500,
                    name="Young debt",
                    created_at=datetime(2026, 9, 10),
                ),
            ),
        ]
        worsening = _fund(db_session, -80, name="Down")
        _txn(db_session, sample_category.id, worsening, date="2026-04-02", amount=300)
        cases.append(_run(db_session, worsening))
        for result in cases:
            assert "runs out" not in result.headline
            assert "growing" not in result.headline
            assert "overdrawn" not in result.headline

    def test_whole_dollars_and_cents_in_debt_copy(self, db_session):
        whole = _run(db_session, _fund(db_session, -1500, name="Whole"))
        cents = _run(db_session, _fund(db_session, Decimal("-1500.50"), name="Cents"))
        assert "$1,500 in the red" in whole.headline
        assert "$1,500.50 in the red" in cents.headline
        assert "$1,500.00" not in whole.headline


class TestOther:
    def test_bills_fund_has_no_figure_whatever_the_balance(
        self, db_session, sample_category
    ):
        fund = _fund(db_session, -20, name="Bills")
        _txn(db_session, sample_category.id, fund, date="2026-04-02", amount=5000)
        result = _run(db_session, fund)
        assert result.state == "bills"
        assert result.is_warning is False
        assert result.runway_months is None
        assert result.months_to_zero is None
        assert result.headline == (
            "Bills come in lumps, so runway isn't shown here. "
            "See Forecast for what's ahead."
        )
        assert result.detail is None

    def test_soft_deleted_fund_is_hidden(self, db_session, sample_category):
        fund = _fund(db_session, 2000, deleted=True)
        _txn(db_session, sample_category.id, fund, date="2026-04-02", amount=100)
        result = _run(db_session, fund)
        assert result.state == "hidden"
        assert result.headline == ""
        assert result.detail is None

        deleted_bills = _fund(db_session, -5, name="Bills", deleted=True)
        assert _run(db_session, deleted_bills).state == "hidden"


@pytest.mark.parametrize(
    ("txn_type", "expected"),
    [("income", 1), ("transfer", 1), ("expense", -1), ("other", -1)],
)
def test_balance_direction(txn_type, expected):
    assert balance_direction(txn_type) == expected
