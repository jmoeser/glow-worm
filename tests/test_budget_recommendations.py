"""Tests for budget recommendation service and UI surfaces."""

from datetime import datetime
from decimal import Decimal

from app.config import TIMEZONE
from app.models import Budget
from app.schemas import BudgetRecommendationSummary
from app.services.budget_recommendations import (
    compute_recommendations,
    recommendation_summary,
)


def _month_year():
    now = datetime.now(TIMEZONE)
    return now.month, now.year


def _prev_month(month: int, year: int, steps: int = 1) -> tuple[int, int]:
    m, y = month, year
    for _ in range(steps):
        if m == 1:
            m, y = 12, y - 1
        else:
            m -= 1
    return m, y


def _add_budget(db, category_id, month, year, allocated, spent):
    b = Budget(
        category_id=category_id,
        month=month,
        year=year,
        allocated_amount=allocated,
        spent_amount=spent,
        fund_balance=0,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


class TestComputeRecommendations:
    def test_average_of_up_to_six_months(self, db_session, sample_budget_categories):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        current = _add_budget(db_session, cat.id, month, year, allocated=400, spent=0)

        # 6 completed months: 100, 200, 300, 400, 500, 600 → avg 350
        spends = [100, 200, 300, 400, 500, 600]
        for i, spent in enumerate(spends, start=1):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=400, spent=spent)

        recs = compute_recommendations(db_session, month, year)
        assert len(recs) == 1
        rec = recs[0]
        assert rec.budget_id == current.id
        assert rec.category_id == cat.id
        assert rec.recommended == Decimal("350.00")
        assert rec.delta == Decimal("-50.00")
        assert rec.direction == "lower"
        assert rec.months_used == 6

    def test_uses_fewer_months_when_history_short(
        self, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=0)

        # 3 months: 200, 300, 400 → avg 300 → raise by 200
        for i, spent in enumerate([200, 300, 400], start=1):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=100, spent=spent)

        recs = compute_recommendations(db_session, month, year)
        assert len(recs) == 1
        assert recs[0].recommended == Decimal("300.00")
        assert recs[0].months_used == 3
        assert recs[0].direction == "raise"
        assert recs[0].delta == Decimal("200.00")

    def test_excludes_current_month_spend_from_sample(
        self, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=9999)

        for i, spent in enumerate([200, 200], start=1):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=100, spent=spent)

        recs = compute_recommendations(db_session, month, year)
        assert len(recs) == 1
        # If current month were included, avg would be much higher
        assert recs[0].recommended == Decimal("200.00")
        assert recs[0].sample_spends == [Decimal("200.00"), Decimal("200.00")]

    def test_no_recommendation_with_fewer_than_two_months(
        self, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=0)

        m, y = _prev_month(month, year, 1)
        _add_budget(db_session, cat.id, m, y, allocated=100, spent=500)

        assert compute_recommendations(db_session, month, year) == []

    def test_threshold_filters_small_deltas(self, db_session, sample_budget_categories):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        # allocated 100; avg spend 105 → delta 5 < max(20, 10) → skip
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=0)
        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=100, spent=105)

        assert compute_recommendations(db_session, month, year) == []

    def test_threshold_uses_ten_percent_of_allocated(
        self, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        # allocated 500; 10% = 50 > 20; avg 530 → delta 30 < 50 → skip
        _add_budget(db_session, cat.id, month, year, allocated=500, spent=0)
        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=500, spent=530)

        assert compute_recommendations(db_session, month, year) == []

    def test_both_raise_and_lower(self, db_session, sample_budget_categories):
        month, year = _month_year()
        groceries, transport = sample_budget_categories[0], sample_budget_categories[1]

        _add_budget(db_session, groceries.id, month, year, allocated=100, spent=0)
        _add_budget(db_session, transport.id, month, year, allocated=400, spent=0)

        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, groceries.id, m, y, allocated=100, spent=250)
            _add_budget(db_session, transport.id, m, y, allocated=400, spent=200)

        recs = compute_recommendations(db_session, month, year)
        by_name = {r.category_name: r for r in recs}
        assert by_name["Groceries"].direction == "raise"
        assert by_name["Transport"].direction == "lower"

    def test_sorted_by_absolute_delta_descending(
        self, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        g, t = sample_budget_categories[0], sample_budget_categories[1]
        _add_budget(db_session, g.id, month, year, allocated=100, spent=0)
        _add_budget(db_session, t.id, month, year, allocated=100, spent=0)

        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, g.id, m, y, allocated=100, spent=150)  # +50
            _add_budget(db_session, t.id, m, y, allocated=100, spent=300)  # +200

        recs = compute_recommendations(db_session, month, year)
        assert [r.category_name for r in recs] == ["Transport", "Groceries"]

    def test_no_current_budget_row_means_no_rec(
        self, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        for i in range(1, 4):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=100, spent=200)

        assert compute_recommendations(db_session, month, year) == []

    def test_missing_history_months_omitted_not_zeroed(
        self, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=0)
        # Only months 1 and 3 ago (gap at month 2) → sample of 2: 200, 400 → avg 300
        m1, y1 = _prev_month(month, year, 1)
        m3, y3 = _prev_month(month, year, 3)
        _add_budget(db_session, cat.id, m1, y1, allocated=100, spent=200)
        _add_budget(db_session, cat.id, m3, y3, allocated=100, spent=400)

        recs = compute_recommendations(db_session, month, year)
        assert len(recs) == 1
        assert recs[0].months_used == 2
        assert recs[0].recommended == Decimal("300.00")


class TestRecommendationSummary:
    def test_summary_aggregates(self, db_session, sample_budget_categories):
        month, year = _month_year()
        g, t = sample_budget_categories[0], sample_budget_categories[1]
        _add_budget(db_session, g.id, month, year, allocated=100, spent=0)
        _add_budget(db_session, t.id, month, year, allocated=400, spent=0)
        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, g.id, m, y, allocated=100, spent=250)
            _add_budget(db_session, t.id, m, y, allocated=400, spent=200)

        summary = recommendation_summary(db_session, month, year)
        assert isinstance(summary, BudgetRecommendationSummary)
        assert summary.count == 2
        # groceries +150, transport -200 → net -50
        assert summary.net_delta == Decimal("-50.00")
        assert len(summary.items) == 2


class TestBudgetsPageRecommendations:
    def test_panel_shown_for_current_month_with_recs(
        self, authed_client, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=0)
        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=100, spent=250)

        response = authed_client.get("/budgets")
        assert response.status_code == 200
        assert "budget-recommendations" in response.text
        assert "Suggestions from last 6 months" in response.text
        assert "Groceries" in response.text
        assert "advisory" in response.text.lower()

    def test_panel_hidden_for_historical_month(
        self, authed_client, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=0)
        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=100, spent=250)

        # View a past month that has a budget but is not current
        past_m, past_y = _prev_month(month, year, 1)
        response = authed_client.get(f"/budgets?month={past_m}&year={past_y}")
        assert response.status_code == 200
        assert "budget-recommendations" not in response.text

    def test_panel_hidden_when_no_recommendations(self, authed_client, sample_budgets):
        # sample_budgets only has current month — no history
        response = authed_client.get("/budgets")
        assert response.status_code == 200
        assert "budget-recommendations" not in response.text


class TestDashboardRecommendationsTeaser:
    def test_teaser_shown_for_current_month(
        self, authed_client, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=0)
        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=100, spent=250)

        response = authed_client.get("/")
        assert response.status_code == 200
        assert "Budget suggestions" in response.text
        assert "look off" in response.text
        assert 'href="/budgets#budget-recommendations"' in response.text

    def test_teaser_hidden_for_historical_dashboard(
        self, authed_client, db_session, sample_budget_categories
    ):
        month, year = _month_year()
        cat = sample_budget_categories[0]
        _add_budget(db_session, cat.id, month, year, allocated=100, spent=0)
        for i in range(1, 3):
            m, y = _prev_month(month, year, i)
            _add_budget(db_session, cat.id, m, y, allocated=100, spent=250)

        past_m, past_y = _prev_month(month, year, 1)
        response = authed_client.get(f"/?month={past_m}&year={past_y}")
        assert response.status_code == 200
        assert "Budget suggestions" not in response.text

    def test_teaser_hidden_when_no_recs(self, authed_client, sample_budgets):
        response = authed_client.get("/")
        assert response.status_code == 200
        assert "Budget suggestions" not in response.text
