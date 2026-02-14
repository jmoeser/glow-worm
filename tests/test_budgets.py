from datetime import datetime

import pytz

from app.models import Budget


def _current_month_year():
    now = datetime.now(pytz.timezone("Australia/Brisbane"))
    return now.month, now.year


class TestBudgetsPageGet:
    def test_renders_page_with_table(self, authed_client):
        response = authed_client.get("/budgets")
        assert response.status_code == 200
        assert "Monthly Budget" in response.text
        assert "Category" in response.text
        assert "Allocated" in response.text
        assert "Spent" in response.text
        assert "Remaining" in response.text
        assert "Fund Balance" in response.text

    def test_lists_budgets_for_current_month(self, authed_client, sample_budgets):
        response = authed_client.get("/budgets")
        assert response.status_code == 200
        assert "Groceries" in response.text
        assert "Transport" in response.text

    def test_month_navigation_links(self, authed_client):
        response = authed_client.get("/budgets?month=6&year=2026")
        assert response.status_code == 200
        assert "June 2026" in response.text
        assert "month=5" in response.text  # prev
        assert "year=2026" in response.text
        assert "month=7" in response.text  # next

    def test_month_year_wrapping(self, authed_client):
        # January -> prev should be December of prior year
        response = authed_client.get("/budgets?month=1&year=2026")
        assert "month=12" in response.text
        assert "year=2025" in response.text

        # December -> next should be January of next year
        response = authed_client.get("/budgets?month=12&year=2026")
        assert "month=1" in response.text
        assert "year=2027" in response.text

    def test_shows_add_form_with_available_categories(self, authed_client, sample_budget_categories):
        response = authed_client.get("/budgets")
        assert response.status_code == 200
        assert "Add Budget Category" in response.text
        assert "Groceries" in response.text
        assert "Transport" in response.text
        assert "Entertainment" in response.text

    def test_hides_categories_already_budgeted(self, authed_client, sample_budgets):
        response = authed_client.get("/budgets")
        # The dropdown options should only contain Entertainment (not Groceries/Transport)
        # Groceries and Transport appear in the table rows but not in the select dropdown
        text = response.text
        # Count occurrences of category names as option values
        # Budgeted categories should NOT appear in the select options
        select_start = text.find('<select id="budget-category"')
        select_end = text.find("</select>", select_start)
        select_html = text[select_start:select_end]
        assert "Entertainment" in select_html
        assert "Groceries" not in select_html
        assert "Transport" not in select_html

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/budgets", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestBudgetsPagePost:
    def test_creates_new_budget(self, authed_client, db_session, sample_budget_categories):
        month, year = _current_month_year()
        response = authed_client.post(
            "/budgets",
            data={
                "category_id": str(sample_budget_categories[0].id),
                "allocated_amount": "500.00",
                "month": str(month),
                "year": str(year),
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        budget = db_session.query(Budget).filter(
            Budget.category_id == sample_budget_categories[0].id,
            Budget.month == month,
            Budget.year == year,
        ).first()
        assert budget is not None
        assert float(budget.allocated_amount) == 500.0

    def test_returns_updated_table_body(self, authed_client, sample_budgets, sample_budget_categories):
        month, year = _current_month_year()
        response = authed_client.post(
            "/budgets",
            data={
                "category_id": str(sample_budget_categories[2].id),  # Entertainment
                "allocated_amount": "100.00",
                "month": str(month),
                "year": str(year),
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Entertainment" in response.text
        assert "Groceries" in response.text

    def test_error_on_missing_category(self, authed_client):
        month, year = _current_month_year()
        response = authed_client.post(
            "/budgets",
            data={
                "category_id": "",
                "allocated_amount": "100.00",
                "month": str(month),
                "year": str(year),
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_error_on_invalid_amount(self, authed_client, sample_budget_categories):
        month, year = _current_month_year()
        response = authed_client.post(
            "/budgets",
            data={
                "category_id": str(sample_budget_categories[0].id),
                "allocated_amount": "abc",
                "month": str(month),
                "year": str(year),
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Invalid" in response.text

    def test_error_on_duplicate_category_month(self, authed_client, sample_budgets, sample_budget_categories):
        month, year = _current_month_year()
        response = authed_client.post(
            "/budgets",
            data={
                "category_id": str(sample_budget_categories[0].id),  # Groceries already budgeted
                "allocated_amount": "100.00",
                "month": str(month),
                "year": str(year),
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "already exists" in response.text.lower()

    def test_403_without_csrf(self, authed_client, sample_budget_categories):
        month, year = _current_month_year()
        response = authed_client.post(
            "/budgets",
            data={
                "category_id": str(sample_budget_categories[0].id),
                "allocated_amount": "100.00",
                "month": str(month),
                "year": str(year),
            },
        )
        assert response.status_code == 403


class TestBudgetsEditGet:
    def test_returns_edit_form_row(self, authed_client, sample_budgets):
        budget = sample_budgets[0]
        response = authed_client.get(f"/budgets/{budget.id}/edit")
        assert response.status_code == 200
        assert 'name="allocated_amount"' in response.text
        assert "Groceries" in response.text

    def test_404_for_nonexistent_budget(self, authed_client):
        response = authed_client.get("/budgets/99999/edit")
        assert response.status_code == 404


class TestBudgetsEditPost:
    def test_updates_allocated_amount(self, authed_client, db_session, sample_budgets):
        budget = sample_budgets[0]
        response = authed_client.post(
            f"/budgets/{budget.id}",
            data={"allocated_amount": "750.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(budget)
        assert float(budget.allocated_amount) == 750.0

    def test_returns_updated_row(self, authed_client, sample_budgets):
        budget = sample_budgets[0]
        response = authed_client.post(
            f"/budgets/{budget.id}",
            data={"allocated_amount": "750.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert "750.00" in response.text

    def test_404_for_nonexistent_budget(self, authed_client):
        response = authed_client.post(
            "/budgets/99999",
            data={"allocated_amount": "100.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestBudgetsDelete:
    def test_hard_deletes_budget(self, authed_client, db_session, sample_budgets):
        budget = sample_budgets[0]
        budget_id = budget.id
        response = authed_client.delete(
            f"/budgets/{budget_id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.expire_all()
        assert db_session.query(Budget).filter(Budget.id == budget_id).first() is None

    def test_returns_empty_response(self, authed_client, sample_budgets):
        budget = sample_budgets[0]
        response = authed_client.delete(
            f"/budgets/{budget.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.text == ""

    def test_404_for_nonexistent_budget(self, authed_client):
        response = authed_client.delete(
            "/budgets/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_403_without_csrf(self, authed_client, sample_budgets):
        budget = sample_budgets[0]
        response = authed_client.delete(f"/budgets/{budget.id}")
        assert response.status_code == 403


class TestApiBudgetsList:
    def test_returns_json_list(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.get(f"/api/budgets?month={month}&year={year}")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_defaults_to_current_month(self, authed_client, sample_budgets):
        response = authed_client.get("/api/budgets")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_unauthenticated_redirects(self, client):
        response = client.get("/api/budgets", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestApiBudgetsCreate:
    def test_creates_budget_returns_201(self, authed_client, db_session, sample_budget_categories):
        month, year = _current_month_year()
        response = authed_client.post(
            "/api/budgets",
            json={
                "category_id": sample_budget_categories[0].id,
                "month": month,
                "year": year,
                "allocated_amount": "300.00",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert float(data["allocated_amount"]) == 300.0
        assert data["category_id"] == sample_budget_categories[0].id

    def test_422_on_validation_error(self, authed_client):
        response = authed_client.post(
            "/api/budgets",
            json={"category_id": 1},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422

    def test_409_on_duplicate(self, authed_client, sample_budgets, sample_budget_categories):
        month, year = _current_month_year()
        response = authed_client.post(
            "/api/budgets",
            json={
                "category_id": sample_budget_categories[0].id,
                "month": month,
                "year": year,
                "allocated_amount": "100.00",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 409

    def test_api_csrf_exempt(self, authed_client, sample_budget_categories):
        """API routes are CSRF-exempt (they use Bearer token auth instead)."""
        month, year = _current_month_year()
        response = authed_client.post(
            "/api/budgets",
            json={
                "category_id": sample_budget_categories[0].id,
                "month": month,
                "year": year,
                "allocated_amount": "100.00",
            },
        )
        assert response.status_code == 201


class TestApiBudgetsGet:
    def test_returns_single_budget(self, authed_client, sample_budgets):
        budget = sample_budgets[0]
        response = authed_client.get(f"/api/budgets/{budget.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == budget.id
        assert float(data["allocated_amount"]) == 600.0

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.get("/api/budgets/99999")
        assert response.status_code == 404


class TestApiBudgetsUpdate:
    def test_updates_and_returns_200(self, authed_client, db_session, sample_budgets):
        budget = sample_budgets[0]
        response = authed_client.put(
            f"/api/budgets/{budget.id}",
            json={"allocated_amount": "800.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert float(data["allocated_amount"]) == 800.0

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.put(
            "/api/budgets/99999",
            json={"allocated_amount": "100.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_422_on_validation_error(self, authed_client, sample_budgets):
        budget = sample_budgets[0]
        response = authed_client.put(
            f"/api/budgets/{budget.id}",
            json={"allocated_amount": "-10"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422


class TestApiBudgetsDelete:
    def test_hard_deletes_and_returns_200(self, authed_client, db_session, sample_budgets):
        budget = sample_budgets[0]
        budget_id = budget.id
        response = authed_client.delete(
            f"/api/budgets/{budget_id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.expire_all()
        assert db_session.query(Budget).filter(Budget.id == budget_id).first() is None

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.delete(
            "/api/budgets/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestBudgetSummary:
    def test_summary_totals_displayed(self, authed_client, sample_budgets):
        response = authed_client.get("/budgets")
        assert response.status_code == 200
        # Groceries: 600 allocated, 150 spent + Transport: 200 allocated, 80 spent
        # Total allocated: 800, total spent: 230, remaining: 570
        assert "800.00" in response.text
        assert "230.00" in response.text
        assert "570.00" in response.text

    def test_empty_month_shows_zero_totals(self, authed_client):
        # Request a month with no budgets
        response = authed_client.get("/budgets?month=1&year=2000")
        assert response.status_code == 200
        assert "0.00" in response.text
