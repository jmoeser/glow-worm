from datetime import datetime
from decimal import Decimal

import pytz

from app.models import Budget, Category, MonthlyUnallocatedIncome, SinkingFund, Transaction

BRISBANE = pytz.timezone("Australia/Brisbane")


def _current_month_year():
    now = datetime.now(BRISBANE)
    return now.month, now.year


class TestDashboardPageGet:
    def test_renders_page(self, authed_client):
        response = authed_client.get("/")
        assert response.status_code == 200
        assert "Dashboard" in response.text or "Total Income" in response.text

    def test_defaults_to_current_month(self, authed_client):
        month, year = _current_month_year()
        import calendar
        month_name = calendar.month_name[month]
        response = authed_client.get("/")
        assert response.status_code == 200
        assert month_name in response.text
        assert str(year) in response.text

    def test_financial_summary_with_data(self, authed_client, sample_transactions):
        response = authed_client.get("/?month=1&year=2026")
        assert response.status_code == 200
        assert "5000.00" in response.text  # income
        assert "75.50" in response.text    # expense
        assert "4924.50" in response.text  # net

    def test_budget_overview(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.get(f"/?month={month}&year={year}")
        assert response.status_code == 200
        assert "Budget Overview" in response.text
        assert "800.00" in response.text  # allocated: 600 + 200
        assert "230.00" in response.text  # spent: 150 + 80
        assert "570.00" in response.text  # remaining: 800 - 230

    def test_sinking_funds_list(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/")
        assert response.status_code == 200
        assert "Bills" in response.text
        assert "Savings" in response.text

    def test_recent_transactions(self, authed_client, sample_transactions):
        response = authed_client.get("/?month=1&year=2026")
        assert response.status_code == 200
        assert "Groceries shopping" in response.text
        assert "Monthly income" in response.text

    def test_empty_month_shows_zeros(self, authed_client):
        response = authed_client.get("/?month=6&year=2030")
        assert response.status_code == 200
        assert "0.00" in response.text
        assert "No transactions this month." in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_month_year_params(self, authed_client, sample_transactions):
        response = authed_client.get("/?month=1&year=2026")
        assert response.status_code == 200
        assert "January" in response.text
        assert "2026" in response.text

    def test_unallocated_income_displayed(self, authed_client, db_session):
        month, year = _current_month_year()
        row = MonthlyUnallocatedIncome(
            month=month, year=year, unallocated_amount=350.75,
        )
        db_session.add(row)
        db_session.commit()

        response = authed_client.get(f"/?month={month}&year={year}")
        assert response.status_code == 200
        assert "350.75" in response.text

    def test_transaction_limit_10(self, authed_client, db_session):
        cat = Category(name="Test", type="expense", color="#123456", is_budget_category=False)
        db_session.add(cat)
        db_session.commit()
        db_session.refresh(cat)

        for i in range(15):
            txn = Transaction(
                date=f"2026-03-{(i % 28) + 1:02d}",
                description=f"txn-{i}",
                amount=10.00,
                category_id=cat.id,
                type="expense",
                transaction_type="regular",
            )
            db_session.add(txn)
        db_session.commit()

        response = authed_client.get("/?month=3&year=2026")
        assert response.status_code == 200
        # Count transaction rows in the table body
        # At most 10 rows should appear
        count = response.text.count("txn-")
        assert count == 10


class TestApiDashboard:
    def test_json_structure(self, authed_client):
        response = authed_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()
        assert "total_income" in data
        assert "total_expenses" in data
        assert "net" in data
        assert "unallocated_income" in data
        assert "budget_total_allocated" in data
        assert "budget_total_spent" in data
        assert "budget_total_remaining" in data
        assert "sinking_funds" in data
        assert "recent_transactions" in data

    def test_defaults_to_current_month(self, authed_client):
        response = authed_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()
        # With no data, totals should be zero-like strings
        assert data["total_income"] == "0.00"

    def test_income_expense_totals(self, authed_client, sample_transactions):
        response = authed_client.get("/api/dashboard?month=1&year=2026")
        assert response.status_code == 200
        data = response.json()
        assert data["total_income"] == "5000.00"
        assert data["total_expenses"] == "75.50"
        assert data["net"] == "4924.50"

    def test_budget_totals(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.get(f"/api/dashboard?month={month}&year={year}")
        assert response.status_code == 200
        data = response.json()
        assert data["budget_total_allocated"] == "800.00"
        assert data["budget_total_spent"] == "230.00"
        assert data["budget_total_remaining"] == "570.00"

    def test_sinking_funds_in_response(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()
        assert len(data["sinking_funds"]) == 2
        names = [sf["name"] for sf in data["sinking_funds"]]
        assert "Bills" in names
        assert "Savings" in names

    def test_unallocated_income_default(self, authed_client):
        response = authed_client.get("/api/dashboard?month=6&year=2030")
        assert response.status_code == 200
        data = response.json()
        assert data["unallocated_income"] == "0.00"

    def test_unallocated_income_from_db(self, authed_client, db_session):
        month, year = _current_month_year()
        row = MonthlyUnallocatedIncome(
            month=month, year=year, unallocated_amount=123.45,
        )
        db_session.add(row)
        db_session.commit()

        response = authed_client.get(f"/api/dashboard?month={month}&year={year}")
        assert response.status_code == 200
        data = response.json()
        assert data["unallocated_income"] == "123.45"

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/api/dashboard", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_empty_month(self, authed_client):
        response = authed_client.get("/api/dashboard?month=6&year=2030")
        assert response.status_code == 200
        data = response.json()
        assert data["total_income"] == "0.00"
        assert data["total_expenses"] == "0.00"
        assert data["net"] == "0.00"
        assert data["recent_transactions"] == []
