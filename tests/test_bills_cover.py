"""Bills fund cover check: horizon obligations vs primary and secondary cash."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import TIMEZONE, bills_cover_horizon_months
from app.models import (
    IncomeAllocation,
    RecurringBill,
    SecondaryIncomeAllocation,
    SecondaryIncomeAllocationRule,
    SinkingFund,
    Transaction,
)
from app.services.bills_cover import BillsCoverResult, compute_bills_cover
from app.tasks import advance_due_date


def _bill(db, category_id, *, name, amount, frequency, next_due, active=True):
    bill = RecurringBill(
        name=name,
        amount=amount,
        debtor_provider="Provider",
        start_date="2026-01-01",
        frequency=frequency,
        category_id=category_id,
        next_due_date=next_due,
        is_active=active,
    )
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return bill


def _txn(db, category_id, **kwargs):
    txn = Transaction(
        description=kwargs.pop("description", "bill"),
        category_id=category_id,
        type=kwargs.pop("type", "expense"),
        transaction_type=kwargs.pop("transaction_type", "regular"),
        is_paid=kwargs.pop("is_paid", True),
        **kwargs,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def _fixed_allocation(db, amount):
    alloc = IncomeAllocation(
        monthly_income_amount=5000,
        monthly_budget_allocation=0,
        bills_fund_allocation_type="fixed",
        bills_fund_fixed_amount=amount,
    )
    db.add(alloc)
    db.commit()
    return alloc


class TestHorizon:
    def test_default_horizon_is_three_calendar_months(self, db_session, monkeypatch):
        monkeypatch.delenv("BILLS_COVER_HORIZON_MONTHS", raising=False)
        assert bills_cover_horizon_months() == 3
        result = compute_bills_cover(db_session, today=date(2026, 1, 15))
        assert result.horizon_months == 3
        assert result.horizon_end == date(2026, 4, 15)

    def test_add_months_clamps_short_months(self, db_session):
        result = compute_bills_cover(
            db_session, today=date(2026, 1, 31), horizon_months=1
        )
        assert result.horizon_end == date(2026, 2, 28)
        leap = compute_bills_cover(
            db_session, today=date(2028, 1, 31), horizon_months=1
        )
        assert leap.horizon_end == date(2028, 2, 29)

    def test_env_overrides_horizon(self, db_session, monkeypatch):
        monkeypatch.setenv("BILLS_COVER_HORIZON_MONTHS", "6")
        result = compute_bills_cover(db_session, today=date(2026, 1, 15))
        assert result.horizon_months == 6
        assert result.horizon_end == date(2026, 7, 15)

    def test_env_clamps_and_rejects_junk(self, monkeypatch, caplog):
        monkeypatch.setenv("BILLS_COVER_HORIZON_MONTHS", "0")
        assert bills_cover_horizon_months() == 1
        assert "outside 1-12" in caplog.text

        monkeypatch.setenv("BILLS_COVER_HORIZON_MONTHS", "99")
        assert bills_cover_horizon_months() == 12

        caplog.clear()
        monkeypatch.setenv("BILLS_COVER_HORIZON_MONTHS", "nope")
        assert bills_cover_horizon_months() == 3
        assert "Invalid BILLS_COVER_HORIZON_MONTHS" in caplog.text

    def test_argument_is_clamped(self, db_session):
        low = compute_bills_cover(db_session, today=date(2026, 5, 1), horizon_months=0)
        high = compute_bills_cover(
            db_session, today=date(2026, 5, 1), horizon_months=24
        )
        assert low.horizon_months == 1
        assert high.horizon_months == 12


class TestNoBillsFund:
    def test_missing_fund_hides_banner(self, db_session, sample_category):
        _bill(
            db_session,
            sample_category.id,
            name="Rent",
            amount=100,
            frequency="monthly",
            next_due="2026-01-20",
        )
        result = compute_bills_cover(db_session, today=date(2026, 1, 15))
        assert result.bills_fund_found is False
        assert result.show_banner is False
        assert result.obligations == Decimal("0.00")

    def test_soft_deleted_fund_is_missing(self, db_session, sample_category):
        db_session.add(
            SinkingFund(
                name="Bills", color="#FF0000", current_balance=0, is_deleted=True
            )
        )
        db_session.commit()
        result = compute_bills_cover(db_session, today=date(2026, 1, 15))
        assert result.bills_fund_found is False
        assert result.show_banner is False


class TestQuarterlyOutsideThirtyDays:
    def test_bursty_quarterly_bill_60_to_90_days_out_is_short(
        self, db_session, sample_category, sample_sinking_funds
    ):
        """The old 30-day buffer missed a cluster that 3 months still catches.

        A $50 bill due in 5 days is covered by a $200 balance, so a next-due-date
        check limited to 30 days stays quiet. A $800 quarterly due 64 days out
        is inside the 3-month horizon and makes the safe path short.
        """
        today = date(2026, 1, 15)
        bills_fund = sample_sinking_funds[0]
        bills_fund.current_balance = 200
        db_session.commit()

        near = _bill(
            db_session,
            sample_category.id,
            name="Internet",
            amount=50,
            frequency="monthly",
            next_due="2026-01-20",
        )
        quarterly = _bill(
            db_session,
            sample_category.id,
            name="Insurance",
            amount=800,
            frequency="quarterly",
            next_due="2026-03-20",
        )

        assert (date.fromisoformat(quarterly.next_due_date) - today).days == 64
        assert (date.fromisoformat(near.next_due_date) - today).days <= 30
        thirty_day_cutoff = today + timedelta(days=30)
        assert date.fromisoformat(quarterly.next_due_date) > thirty_day_cutoff

        result = compute_bills_cover(db_session, today=today, horizon_months=3)
        # Monthly: 20 Jan, 20 Feb, 20 Mar (20 Apr is after 15 Apr). Quarterly: 20 Mar.
        assert result.horizon_end == date(2026, 4, 15)
        assert result.obligations == Decimal("950.00")
        assert result.without_cash == Decimal("200.00")
        assert result.with_cash == Decimal("200.00")
        assert result.short_without == Decimal("750.00")
        assert result.short_with == Decimal("750.00")
        assert result.show_banner is True
        assert result.has_secondary_goal is False
        assert result.projected_count == 4
        assert result.unpaid_count == 0


class TestSecondaryAndPrimary:
    def test_no_secondary_goal_keeps_both_figures_equal(
        self, db_session, sample_category, sample_sinking_funds
    ):
        sample_sinking_funds[0].current_balance = 10
        db_session.commit()
        _bill(
            db_session,
            sample_category.id,
            name="Rent",
            amount=100,
            frequency="monthly",
            next_due="2026-03-20",
        )
        result = compute_bills_cover(
            db_session, today=date(2026, 3, 10), horizon_months=3
        )
        assert result.has_secondary_goal is False
        assert result.with_cash == result.without_cash
        assert result.short_with == result.short_without
        assert result.show_banner is True

    def test_secondary_goal_does_not_change_the_safe_figure(
        self, db_session, sample_category, sample_sinking_funds
    ):
        bills = sample_sinking_funds[0]
        savings = sample_sinking_funds[1]
        bills.current_balance = 0
        db_session.commit()
        _bill(
            db_session,
            sample_category.id,
            name="Rent",
            amount=100,
            frequency="yearly",
            next_due="2026-03-20",
        )
        alloc = SecondaryIncomeAllocation(label="Partner")
        db_session.add(alloc)
        db_session.flush()
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=bills.id,
                goal_amount=400,
                sort_order=1,
            )
        )
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=bills.id,
                goal_amount=200,
                sort_order=2,
            )
        )
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=savings.id,
                goal_amount=9999,
                sort_order=3,
            )
        )
        db_session.commit()

        today = date(2026, 3, 10)
        result = compute_bills_cover(db_session, today=today, horizon_months=3)
        # One yearly bill. Months Mar–Jun, goal 600, nothing recorded yet.
        assert result.obligations == Decimal("100.00")
        assert result.without_cash == Decimal("0.00")
        assert result.has_secondary_goal is True
        assert result.with_cash == Decimal("2400.00")
        assert result.short_without == Decimal("100.00")
        assert result.short_with == Decimal("0.00")
        assert result.show_banner is True

    def test_current_month_progress_reduces_only_that_month(
        self, db_session, sample_category, sample_sinking_funds
    ):
        bills = sample_sinking_funds[0]
        bills.current_balance = 50
        db_session.commit()
        alloc = SecondaryIncomeAllocation(label="Partner")
        db_session.add(alloc)
        db_session.flush()
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=bills.id,
                goal_amount=600,
                sort_order=1,
            )
        )
        db_session.commit()
        _txn(
            db_session,
            sample_category.id,
            date="2026-03-05",
            amount=250,
            type="transfer",
            transaction_type="secondary_income_allocation",
            sinking_fund_id=bills.id,
            is_paid=True,
        )
        # A future-month recording does not reduce that month's full goal.
        _txn(
            db_session,
            sample_category.id,
            date="2026-04-02",
            amount=600,
            type="transfer",
            transaction_type="secondary_income_allocation",
            sinking_fund_id=bills.id,
            is_paid=True,
        )
        # Last month does not reduce this month.
        _txn(
            db_session,
            sample_category.id,
            date="2026-02-20",
            amount=600,
            type="transfer",
            transaction_type="secondary_income_allocation",
            sinking_fund_id=bills.id,
            is_paid=True,
        )

        result = compute_bills_cover(
            db_session, today=date(2026, 3, 18), horizon_months=3
        )
        # Mar remaining 350 + Apr 600 + May 600 + Jun 600. Balance stays 50
        # because these transactions were inserted without the route's adjuster.
        assert result.horizon_end == date(2026, 6, 18)
        assert result.without_cash == Decimal("50.00")
        assert result.with_cash == Decimal("2200.00")
        assert result.show_banner is False

    def test_primary_already_posted_is_not_counted_again(
        self, db_session, sample_category, sample_sinking_funds
    ):
        bills = sample_sinking_funds[0]
        bills.current_balance = 50
        db_session.commit()
        _fixed_allocation(db_session, 100)
        _txn(
            db_session,
            sample_category.id,
            date="2026-03-01",
            amount=100,
            type="transfer",
            transaction_type="income_allocation",
            sinking_fund_id=bills.id,
        )
        posted = compute_bills_cover(
            db_session, today=date(2026, 3, 18), horizon_months=3
        )
        # 1sts still in horizon: 1 Apr, 1 May, 1 Jun. March already posted.
        assert posted.horizon_end == date(2026, 6, 18)
        assert posted.without_cash == Decimal("350.00")

    def test_primary_not_yet_posted_includes_this_month(
        self, db_session, sample_sinking_funds
    ):
        sample_sinking_funds[0].current_balance = 50
        db_session.commit()
        _fixed_allocation(db_session, 100)
        pending = compute_bills_cover(
            db_session, today=date(2026, 3, 18), horizon_months=3
        )
        assert pending.without_cash == Decimal("450.00")

    def test_recommended_primary_matches_annualised_bills(
        self, db_session, sample_category, sample_sinking_funds
    ):
        bills = sample_sinking_funds[0]
        bills.current_balance = 0
        db_session.commit()
        _bill(
            db_session,
            sample_category.id,
            name="Rent",
            amount=120,
            frequency="monthly",
            next_due="2026-01-20",
        )
        db_session.add(
            IncomeAllocation(
                monthly_income_amount=5000,
                monthly_budget_allocation=0,
                bills_fund_allocation_type="recommended",
            )
        )
        db_session.commit()
        _txn(
            db_session,
            sample_category.id,
            date="2026-01-01",
            amount=120,
            type="transfer",
            transaction_type="income_allocation",
            sinking_fund_id=bills.id,
        )
        result = compute_bills_cover(
            db_session, today=date(2026, 1, 20), horizon_months=1
        )
        # Recommended monthly amount is 120. January is posted, so only 1 Feb
        # remains. Occurrences on 20 Jan and 20 Feb are both inside 20 Feb.
        assert result.obligations == Decimal("240.00")
        assert result.without_cash == Decimal("120.00")
        assert result.short_without == Decimal("120.00")
        assert result.show_banner is True

    def test_allocation_on_the_horizon_end_is_included(
        self, db_session, sample_category, sample_sinking_funds
    ):
        bills = sample_sinking_funds[0]
        bills.current_balance = 0
        db_session.commit()
        _fixed_allocation(db_session, 80)
        _txn(
            db_session,
            sample_category.id,
            date="2026-01-01",
            amount=80,
            type="transfer",
            transaction_type="income_allocation",
            sinking_fund_id=bills.id,
        )
        result = compute_bills_cover(
            db_session, today=date(2026, 1, 1), horizon_months=1
        )
        # Posted today, so January is skipped. 1 Feb equals the horizon end.
        assert result.horizon_end == date(2026, 2, 1)
        assert result.without_cash == Decimal("80.00")

    def test_banner_hidden_when_safe_path_covers(
        self, db_session, sample_category, sample_sinking_funds
    ):
        bills = sample_sinking_funds[0]
        bills.current_balance = 0
        db_session.commit()
        _bill(
            db_session,
            sample_category.id,
            name="Rent",
            amount=100,
            frequency="yearly",
            next_due="2026-02-01",
        )
        alloc = SecondaryIncomeAllocation(label="Partner")
        db_session.add(alloc)
        db_session.flush()
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=bills.id,
                goal_amount=50,
                sort_order=1,
            )
        )
        db_session.commit()
        _fixed_allocation(db_session, 100)
        result = compute_bills_cover(
            db_session, today=date(2026, 1, 20), horizon_months=1
        )
        # Bill 100. Primary: January not posted + 1 Feb = 200. Secondary is extra.
        assert result.obligations == Decimal("100.00")
        assert result.without_cash == Decimal("200.00")
        assert result.with_cash > result.without_cash
        assert result.short_without == Decimal("0.00")
        assert result.show_banner is False


class TestUnpaidGeneratedBills:
    def test_unpaid_rows_count_once_and_replace_the_projection(
        self, db_session, sample_category, sample_sinking_funds
    ):
        bills = sample_sinking_funds[0]
        savings = sample_sinking_funds[1]
        bills.current_balance = 0
        db_session.commit()
        bill = _bill(
            db_session,
            sample_category.id,
            name="Power",
            amount=100,
            frequency="monthly",
            next_due="2026-03-20",
        )
        # Ledger amount wins over the $100 projection for 20 Mar.
        _txn(
            db_session,
            sample_category.id,
            date="2026-03-20",
            amount=80,
            recurring_bill_id=bill.id,
            sinking_fund_id=bills.id,
            is_paid=False,
        )
        # Past unpaid still owed, and it is not a projected due date.
        _txn(
            db_session,
            sample_category.id,
            date="2026-02-01",
            amount=40,
            recurring_bill_id=bill.id,
            sinking_fund_id=bills.id,
            is_paid=False,
        )
        # No fund link still counts.
        _txn(
            db_session,
            sample_category.id,
            date="2026-03-25",
            amount=15,
            recurring_bill_id=bill.id,
            sinking_fund_id=None,
            is_paid=False,
        )
        # Paid row does not replace the April projection.
        _txn(
            db_session,
            sample_category.id,
            date="2026-04-20",
            amount=100,
            recurring_bill_id=bill.id,
            sinking_fund_id=bills.id,
            is_paid=True,
        )
        # Another fund's unpaid bill is not a Bills obligation, even on a due date.
        _txn(
            db_session,
            sample_category.id,
            date="2026-05-20",
            amount=500,
            recurring_bill_id=bill.id,
            sinking_fund_id=savings.id,
            is_paid=False,
        )
        # After the horizon.
        _txn(
            db_session,
            sample_category.id,
            date="2026-07-01",
            amount=999,
            recurring_bill_id=bill.id,
            sinking_fund_id=bills.id,
            is_paid=False,
        )

        result = compute_bills_cover(
            db_session, today=date(2026, 3, 10), horizon_months=3
        )
        # Mar ledger 80 + Apr 100 + May 100 + past 40 + extra 15. Jun 20 is past 10 Jun.
        assert result.horizon_end == date(2026, 6, 10)
        assert result.obligations == Decimal("335.00")
        assert result.projected_count == 2
        assert result.unpaid_count == 3
        assert result.short_without == Decimal("335.00")

    def test_overdue_next_due_date_is_still_an_obligation(
        self, db_session, sample_category, sample_sinking_funds
    ):
        _bill(
            db_session,
            sample_category.id,
            name="Late",
            amount=100,
            frequency="monthly",
            next_due="2026-02-10",
        )
        result = compute_bills_cover(
            db_session, today=date(2026, 3, 10), horizon_months=1
        )
        # 10 Feb (overdue), 10 Mar, 10 Apr (horizon end).
        assert result.horizon_end == date(2026, 4, 10)
        assert result.obligations == Decimal("300.00")
        assert result.projected_count == 3

    def test_invalid_next_due_date_is_skipped(
        self, db_session, sample_category, sample_sinking_funds, caplog
    ):
        _bill(
            db_session,
            sample_category.id,
            name="Broken",
            amount=500,
            frequency="monthly",
            next_due="not-a-date",
        )
        _bill(
            db_session,
            sample_category.id,
            name="Ok",
            amount=30,
            frequency="yearly",
            next_due="2026-03-12",
        )
        result = compute_bills_cover(
            db_session, today=date(2026, 3, 10), horizon_months=3
        )
        assert result.obligations == Decimal("30.00")
        assert "invalid next_due_date" in caplog.text

    def test_inactive_bill_is_not_projected(
        self, db_session, sample_category, sample_sinking_funds
    ):
        _bill(
            db_session,
            sample_category.id,
            name="Old",
            amount=400,
            frequency="monthly",
            next_due="2026-03-20",
            active=False,
        )
        result = compute_bills_cover(
            db_session, today=date(2026, 3, 10), horizon_months=3
        )
        assert result.obligations == Decimal("0.00")
        assert result.show_banner is False

    def test_occurrences_follow_advance_due_date(
        self, db_session, sample_category, sample_sinking_funds
    ):
        today = date(2026, 1, 15)
        horizon_end = date(2026, 4, 15)
        _bill(
            db_session,
            sample_category.id,
            name="Fortnightish",
            amount=10,
            frequency="28_days",
            next_due=today.isoformat(),
        )
        expected_dates = []
        current = today
        while current <= horizon_end:
            expected_dates.append(current)
            current = advance_due_date(current, "28_days")

        result = compute_bills_cover(db_session, today=today, horizon_months=3)
        assert result.horizon_end == horizon_end
        assert result.projected_count == len(expected_dates)
        assert result.obligations == Decimal("10.00") * len(expected_dates)


def _cover(**overrides) -> BillsCoverResult:
    fields = {
        "horizon_months": 3,
        "horizon_end": date(2027, 1, 29),
        "obligations": Decimal("920.00"),
        "without_cash": Decimal("100.00"),
        "with_cash": Decimal("1700.00"),
        "short_without": Decimal("820.00"),
        "short_with": Decimal("0.00"),
        "projected_count": 4,
        "unpaid_count": 0,
        "has_secondary_goal": True,
        "bills_fund_found": True,
        "fund_balance": Decimal("100.00"),
        "primary_in": Decimal("0.00"),
    }
    fields.update(overrides)
    return BillsCoverResult(**fields)


class TestBannerCopy:
    def test_example_shortfall_with_room_if_the_goal_is_met(self):
        result = _cover()
        assert result.headline == "Bills fund may run short in the next 3 months"
        assert result.summary_line == (
            "$920 of bills due by 29 January 2027, and the Bills fund has $100."
        )
        assert result.outlook_line == (
            "Without secondary income you'll be $820 short. "
            "If secondary income reaches the Bills goal, you'll be covered with "
            "$780 to spare."
        )

    def test_one_month_uses_the_singular(self):
        result = _cover(horizon_months=1, horizon_end=date(2026, 11, 29))
        assert result.headline == "Bills fund may run short in the next 1 month"
        assert "29 November 2026" in result.summary_line

    def test_regular_contributions_are_mentioned_only_when_they_add_cash(self):
        quiet = _cover()
        assert "regular contributions" not in quiet.summary_line
        with_primary = _cover(
            fund_balance=Decimal("100.00"),
            primary_in=Decimal("300.00"),
            without_cash=Decimal("400.00"),
        )
        assert with_primary.summary_line == (
            "$920 of bills due by 29 January 2027, "
            "and the Bills fund has $100 plus $300 in regular contributions."
        )

    def test_cents_keep_the_usual_format(self):
        result = _cover(
            obligations=Decimal("10.50"),
            fund_balance=Decimal("1.25"),
            without_cash=Decimal("1.25"),
            with_cash=Decimal("1.25"),
            short_without=Decimal("9.25"),
            short_with=Decimal("9.25"),
            has_secondary_goal=False,
        )
        assert "$10.50 of bills" in result.summary_line
        assert "has $1.25." in result.summary_line
        assert "you'll be $9.25 short." in result.outlook_line

    def test_still_short_when_the_goal_does_not_cover(self):
        result = _cover(
            with_cash=Decimal("400.00"),
            short_with=Decimal("520.00"),
        )
        assert result.outlook_line.endswith(
            "If secondary income reaches the Bills goal, you'll still be $520 short."
        )

    def test_exact_cover_does_not_invent_spare_cash(self):
        result = _cover(with_cash=Decimal("920.00"), short_with=Decimal("0.00"))
        assert result.outlook_line.endswith(
            "If secondary income reaches the Bills goal, you'll be covered."
        )

    def test_no_goal_says_so_in_plain_language(self):
        result = _cover(
            has_secondary_goal=False,
            with_cash=Decimal("100.00"),
            short_with=Decimal("820.00"),
        )
        assert result.outlook_line == (
            "Without secondary income you'll be $820 short. "
            "No secondary income goal is set for the Bills fund."
        )


class TestDashboardBanner:
    def test_dashboard_shows_both_figures_only_when_short(
        self, authed_client, db_session, sample_category, sample_sinking_funds
    ):
        today = datetime.now(TIMEZONE).date()
        due = (today + timedelta(days=75)).isoformat()
        bills = sample_sinking_funds[0]
        bills.current_balance = 0
        db_session.commit()
        _bill(
            db_session,
            sample_category.id,
            name="Insurance",
            amount=500,
            frequency="quarterly",
            next_due=due,
        )
        cover = compute_bills_cover(db_session)
        short = authed_client.get("/")
        visible = short.text.replace("&#39;", "'")
        assert 'id="bills-cover-banner"' in visible
        assert cover.headline in visible
        assert cover.summary_line in visible
        assert cover.outlook_line in visible
        assert 'href="/bills"' in short.text
        assert "See upcoming bills" in short.text
        assert "obligations" not in short.text
        assert "not guaranteed" not in short.text
        assert "projected bill" not in short.text
        assert "No secondary income goal is set for the Bills fund." in short.text

        funds = authed_client.get("/sinking-funds")
        assert "Buffer Warning" not in funds.text

        bills.current_balance = 10000
        db_session.commit()
        covered = authed_client.get("/")
        assert "bills-cover-banner" not in covered.text

    def test_optimistic_path_is_labelled_when_it_covers(
        self, authed_client, db_session, sample_category, sample_sinking_funds
    ):
        today = datetime.now(TIMEZONE).date()
        bills = sample_sinking_funds[0]
        bills.current_balance = 0
        db_session.commit()
        _bill(
            db_session,
            sample_category.id,
            name="Rent",
            amount=100,
            frequency="yearly",
            next_due=(today + timedelta(days=10)).isoformat(),
        )
        alloc = SecondaryIncomeAllocation(label="Partner")
        db_session.add(alloc)
        db_session.flush()
        db_session.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=bills.id,
                goal_amount=1000,
                sort_order=1,
            )
        )
        db_session.commit()

        cover = compute_bills_cover(db_session)
        response = authed_client.get("/")
        visible = response.text.replace("&#39;", "'")
        assert cover.outlook_line in visible
        assert "you'll be covered with" in visible
        assert "to spare." in response.text
        assert "(OK)" not in response.text
        assert "not guaranteed" not in response.text
        assert "No secondary income goal is set" not in response.text

    def test_no_banner_without_a_bills_fund(
        self, authed_client, db_session, sample_category
    ):
        today = datetime.now(TIMEZONE).date()
        _bill(
            db_session,
            sample_category.id,
            name="Rent",
            amount=100,
            frequency="monthly",
            next_due=today.isoformat(),
        )
        response = authed_client.get("/")
        assert "bills-cover-banner" not in response.text


@pytest.mark.parametrize(
    ("today", "due", "included"),
    [
        (date(2026, 1, 15), "2026-04-15", True),
        (date(2026, 1, 15), "2026-04-16", False),
    ],
)
def test_horizon_end_is_inclusive(
    db_session, sample_category, sample_sinking_funds, today, due, included
):
    _bill(
        db_session,
        sample_category.id,
        name="Edge",
        amount=25,
        frequency="yearly",
        next_due=due,
    )
    result = compute_bills_cover(db_session, today=today, horizon_months=3)
    if included:
        assert result.obligations == Decimal("25.00")
    else:
        assert result.obligations == Decimal("0.00")
