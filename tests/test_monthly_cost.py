from datetime import datetime
from decimal import Decimal


from app.models import Category, Transaction
from app.routes.monthly_cost import _build_monthly_cost_data

_TODAY = datetime(2026, 4, 2)


def _expense_cat(db_session, name="Food", color="#FF0000"):
    cat = Category(name=name, type="expense", color=color, is_budget_category=False)
    db_session.add(cat)
    db_session.commit()
    db_session.refresh(cat)
    return cat


def _txn(db_session, cat, date, amount, transaction_type="regular"):
    t = Transaction(
        date=date,
        amount=amount,
        category_id=cat.id,
        type="expense",
        transaction_type=transaction_type,
    )
    db_session.add(t)
    db_session.commit()
    return t


class TestMonthlyCostPage:
    def test_renders_page(self, authed_client):
        response = authed_client.get("/monthly-cost")
        assert response.status_code == 200
        assert "Monthly Cost" in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/monthly-cost", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    def test_empty_state_when_no_transactions(self, authed_client):
        response = authed_client.get("/monthly-cost")
        assert response.status_code == 200
        assert "No expense transactions" in response.text

    def test_shows_category_row(self, authed_client, db_session):
        cat = _expense_cat(db_session, "Groceries")
        _txn(db_session, cat, "2026-01-10", 120.00)
        response = authed_client.get("/monthly-cost")
        assert "Groceries" in response.text

    def test_shows_monthly_average(self, authed_client, db_session):
        cat = _expense_cat(db_session)
        # 1 transaction in Jan 2026, today is Apr 2026 → 4 months elapsed
        _txn(db_session, cat, "2026-01-01", 400.00)
        response = authed_client.get("/monthly-cost")
        assert "100.00" in response.text  # 400 / 4 months


class TestBuildMonthlyCostData:
    def test_empty_returns_zeros(self, db_session):
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["rows"] == []
        assert data["grand_total"] == Decimal("0.00")
        assert data["grand_monthly_avg"] == Decimal("0.00")
        assert data["months_elapsed"] == 0
        assert data["first_date"] is None

    def test_months_elapsed_calculation(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-01-15", 100.00)
        # Jan 2026 to Apr 2026 inclusive = 4 months
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["months_elapsed"] == 4

    def test_months_elapsed_spans_years(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2025-01-01", 100.00)
        # Jan 2025 to Apr 2026 inclusive = 16 months
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["months_elapsed"] == 16

    def test_monthly_average(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-01-01", 400.00)
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["rows"][0]["monthly_avg"] == Decimal("100.00")  # 400 / 4

    def test_grand_total_and_avg(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-01-01", 200.00)
        _txn(db_session, cat, "2026-02-01", 200.00)
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["grand_total"] == Decimal("400.00")
        assert data["grand_monthly_avg"] == Decimal("100.00")  # 400 / 4

    def test_includes_regular_expense(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-03-01", 50.00, transaction_type="regular")
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["grand_total"] == Decimal("50.00")

    def test_includes_budget_expense(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-03-01", 75.00, transaction_type="budget_expense")
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["grand_total"] == Decimal("75.00")

    def test_includes_withdrawal(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-03-01", 60.00, transaction_type="withdrawal")
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["grand_total"] == Decimal("60.00")

    def test_excludes_income(self, db_session):
        income_cat = Category(
            name="Salary", type="income", color="#00FF00", is_budget_category=False
        )
        db_session.add(income_cat)
        db_session.commit()
        db_session.add(
            Transaction(
                date="2026-01-01",
                amount=5000.00,
                category_id=income_cat.id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["rows"] == []
        assert data["grand_total"] == Decimal("0.00")

    def test_excludes_income_allocation(self, db_session):
        cat = _expense_cat(db_session)
        _txn(
            db_session, cat, "2026-01-01", 500.00, transaction_type="income_allocation"
        )
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["grand_total"] == Decimal("0.00")

    def test_excludes_contribution(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-01-01", 200.00, transaction_type="contribution")
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["grand_total"] == Decimal("0.00")

    def test_excludes_budget_transfer(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-01-01", 100.00, transaction_type="budget_transfer")
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["grand_total"] == Decimal("0.00")

    def test_sorted_by_monthly_avg_descending(self, db_session):
        cheap = _expense_cat(db_session, "Cheap", "#111")
        expensive = _expense_cat(db_session, "Expensive", "#222")
        _txn(db_session, cheap, "2026-03-01", 10.00)
        _txn(db_session, expensive, "2026-03-01", 500.00)
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["rows"][0]["category"].name == "Expensive"
        assert data["rows"][1]["category"].name == "Cheap"

    def test_percentage_of_total(self, db_session):
        cat_a = _expense_cat(db_session, "A", "#111")
        cat_b = _expense_cat(db_session, "B", "#222")
        _txn(db_session, cat_a, "2026-03-01", 300.00)
        _txn(db_session, cat_b, "2026-03-01", 100.00)
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        # A = 300/400 = 75%, B = 100/400 = 25%
        pcts = {r["category"].name: r["pct_of_total"] for r in data["rows"]}
        assert pcts["A"] == Decimal("75.0")
        assert pcts["B"] == Decimal("25.0")

    def test_first_date_is_earliest_included_transaction(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2025-06-01", 100.00)
        _txn(db_session, cat, "2026-01-01", 100.00)
        # An income transaction earlier than both — should NOT affect first_date
        income_cat = Category(
            name="Salary", type="income", color="#0F0", is_budget_category=False
        )
        db_session.add(income_cat)
        db_session.commit()
        db_session.add(
            Transaction(
                date="2024-01-01",
                amount=5000.00,
                category_id=income_cat.id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["first_date"] == "2025-06-01"

    def test_multiple_transactions_same_category_summed(self, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-03-01", 50.00)
        _txn(db_session, cat, "2026-03-15", 30.00)
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["rows"][0]["total_spent"] == Decimal("80.00")


class TestExcludeFromMonthlyCost:
    def test_excluded_category_not_in_rows(self, db_session):
        cat = Category(
            name="Investments",
            type="expense",
            color="#8B5CF6",
            is_budget_category=False,
            exclude_from_monthly_cost=True,
        )
        db_session.add(cat)
        db_session.commit()
        _txn(db_session, cat, "2026-03-01", 500.00)
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["rows"] == []
        assert data["grand_total"] == Decimal("0.00")

    def test_excluded_category_not_in_first_date(self, db_session):
        excluded = Category(
            name="Investments",
            type="expense",
            color="#8B5CF6",
            exclude_from_monthly_cost=True,
        )
        included = _expense_cat(db_session, "Groceries")
        db_session.add(excluded)
        db_session.commit()
        # Excluded category has earlier transaction — should not set first_date
        _txn(db_session, excluded, "2025-01-01", 1000.00)
        _txn(db_session, included, "2026-02-01", 200.00)
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert data["first_date"] == "2026-02-01"

    def test_included_and_excluded_mixed(self, db_session):
        normal = _expense_cat(db_session, "Food", "#FF0000")
        excluded = Category(
            name="Broker",
            type="expense",
            color="#00FF00",
            exclude_from_monthly_cost=True,
        )
        db_session.add(excluded)
        db_session.commit()
        _txn(db_session, normal, "2026-03-01", 300.00)
        _txn(db_session, excluded, "2026-03-01", 2000.00)
        data = _build_monthly_cost_data(db_session, _today=_TODAY)
        assert len(data["rows"]) == 1
        assert data["rows"][0]["category"].name == "Food"
        assert data["grand_total"] == Decimal("300.00")

    def test_excluded_flag_shown_on_page(self, authed_client, db_session):
        cat = Category(
            name="Investments",
            type="expense",
            color="#8B5CF6",
            exclude_from_monthly_cost=True,
        )
        db_session.add(cat)
        db_session.commit()
        response = authed_client.get("/categories")
        assert response.status_code == 200
        # The category row should show it is excluded
        assert "Investments" in response.text


class TestMonthlyCostApi:
    def test_returns_json(self, authed_client, db_session):
        cat = _expense_cat(db_session)
        _txn(db_session, cat, "2026-01-01", 120.00)
        response = authed_client.get("/api/monthly-cost")
        assert response.status_code == 200
        body = response.json()
        assert "rows" in body
        assert "grand_monthly_avg" in body
        assert "months_elapsed" in body
        assert "first_date" in body

    def test_api_row_fields(self, authed_client, db_session):
        cat = _expense_cat(db_session, "Groceries")
        _txn(db_session, cat, "2026-01-01", 100.00)
        response = authed_client.get("/api/monthly-cost")
        row = response.json()["rows"][0]
        assert "category_id" in row
        assert "category_name" in row
        assert "category_color" in row
        assert "total_spent" in row
        assert "monthly_avg" in row
        assert "pct_of_total" in row

    def test_api_empty_state(self, authed_client):
        response = authed_client.get("/api/monthly-cost")
        assert response.status_code == 200
        body = response.json()
        assert body["rows"] == []
        assert body["grand_total"] == 0.0
        assert body["months_elapsed"] == 0
