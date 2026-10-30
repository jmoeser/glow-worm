import re
from datetime import date, datetime, timedelta

from app.config import TIMEZONE
from app.models import SinkingFund, Transaction


class TestSinkingFundsPageGet:
    def test_renders_page_with_table(self, authed_client):
        response = authed_client.get("/sinking-funds")
        assert response.status_code == 200
        assert "Sinking Funds" in response.text
        assert "Name" in response.text
        assert "Description" in response.text
        assert "Balance" in response.text

    def test_lists_active_funds(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/sinking-funds")
        assert response.status_code == 200
        assert "Bills" in response.text
        assert "Savings" in response.text

    def test_excludes_deleted_funds(
        self, authed_client, db_session, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        fund.is_deleted = True
        db_session.commit()
        response = authed_client.get("/sinking-funds")
        assert f"fund-row-{fund.id}" not in response.text
        assert f"fund-row-{sample_sinking_funds[1].id}" in response.text

    def test_shows_add_form(self, authed_client):
        response = authed_client.get("/sinking-funds")
        assert "Add New Fund" in response.text
        assert 'name="name"' in response.text
        assert 'name="current_balance"' in response.text
        assert 'name="color"' in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/sinking-funds", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestSinkingFundsPagePost:
    def test_creates_new_fund(self, authed_client, db_session):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Emergency",
                "description": "For emergencies",
                "color": "#FF5733",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        fund = (
            db_session.query(SinkingFund)
            .filter(SinkingFund.name == "Emergency")
            .first()
        )
        assert fund is not None
        assert fund.color == "#FF5733"
        assert fund.description == "For emergencies"

    def test_returns_updated_table_body(self, authed_client, sample_sinking_funds):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Holiday",
                "color": "#00AAFF",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Holiday" in response.text
        # Existing funds should also be in the refreshed table body
        assert "Bills" in response.text

    def test_error_on_missing_name(self, authed_client):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "",
                "color": "#FF0000",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_error_on_missing_color(self, authed_client):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Test",
                "color": "",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_current_balance_defaults_to_zero(self, authed_client, db_session):
        authed_client.post(
            "/sinking-funds",
            data={
                "name": "New Fund",
                "color": "#123456",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        fund = (
            db_session.query(SinkingFund).filter(SinkingFund.name == "New Fund").first()
        )
        assert fund is not None
        assert float(fund.current_balance) == 0

    def test_creates_fund_with_initial_balance(self, authed_client, db_session):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Starter",
                "current_balance": "250.00",
                "color": "#AABB00",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        fund = (
            db_session.query(SinkingFund).filter(SinkingFund.name == "Starter").first()
        )
        assert fund is not None
        assert float(fund.current_balance) == 250.0

    def test_creates_fund_with_negative_balance(self, authed_client, db_session):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Overdrawn",
                "current_balance": "-120.50",
                "color": "#FF0000",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        fund = (
            db_session.query(SinkingFund)
            .filter(SinkingFund.name == "Overdrawn")
            .first()
        )
        assert fund is not None
        assert float(fund.current_balance) == -120.5

    def test_error_on_invalid_initial_balance(self, authed_client):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Bad Balance",
                "current_balance": "xyz",
                "color": "#FF0000",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Invalid" in response.text

    def test_403_without_csrf(self, authed_client):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Test",
                "color": "#FF0000",
            },
        )
        assert response.status_code == 403


class TestSinkingFundsEditGet:
    def test_returns_edit_form_row(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}/edit")
        assert response.status_code == 200
        assert f'value="{fund.name}"' in response.text
        assert 'name="color"' in response.text

    def test_404_for_nonexistent_fund(self, authed_client):
        response = authed_client.get("/sinking-funds/99999/edit")
        assert response.status_code == 404


class TestSinkingFundsEditPost:
    def test_updates_fund_fields(self, authed_client, db_session, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.post(
            f"/sinking-funds/{fund.id}",
            data={
                "name": "Updated Bills",
                "description": "Updated description",
                "color": "#AABBCC",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(fund)
        assert fund.name == "Updated Bills"
        assert fund.description == "Updated description"
        assert fund.color == "#AABBCC"

    def test_returns_updated_row(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.post(
            f"/sinking-funds/{fund.id}",
            data={
                "name": "Updated Bills",
                "color": "#AABBCC",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert "Updated Bills" in response.text

    def test_404_for_nonexistent_fund(self, authed_client):
        response = authed_client.post(
            "/sinking-funds/99999",
            data={"name": "X"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestSinkingFundsDelete:
    def test_soft_deletes_fund(self, authed_client, db_session, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.delete(
            f"/sinking-funds/{fund.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(fund)
        assert fund.is_deleted is True

    def test_returns_empty_response(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.delete(
            f"/sinking-funds/{fund.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.text == ""

    def test_404_for_nonexistent_fund(self, authed_client):
        response = authed_client.delete(
            "/sinking-funds/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_403_without_csrf(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.delete(f"/sinking-funds/{fund.id}")
        assert response.status_code == 403

    def test_cannot_delete_system_fund(
        self, authed_client, db_session, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        fund.is_system = True
        db_session.commit()
        response = authed_client.delete(
            f"/sinking-funds/{fund.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 400
        assert "cannot be deleted" in response.text
        db_session.refresh(fund)
        assert fund.is_deleted is False


class TestApiFundsList:
    def test_returns_json_list(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/api/sinking-funds")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        names = {f["name"] for f in data}
        assert "Bills" in names
        assert "Savings" in names

    def test_excludes_deleted_funds(
        self, authed_client, db_session, sample_sinking_funds
    ):
        sample_sinking_funds[0].is_deleted = True
        db_session.commit()
        response = authed_client.get("/api/sinking-funds")
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Savings"

    def test_unauthenticated_redirects(self, client):
        response = client.get("/api/sinking-funds", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestApiFundsCreate:
    def test_creates_fund_returns_201(self, authed_client, db_session):
        response = authed_client.post(
            "/api/sinking-funds",
            json={
                "name": "Holiday",
                "description": "Holiday savings",
                "color": "#FF5733",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Holiday"
        assert data["is_deleted"] is False
        assert data["is_system"] is False

    def test_creates_fund_with_initial_balance(self, authed_client, db_session):
        response = authed_client.post(
            "/api/sinking-funds",
            json={
                "name": "Pre-funded",
                "current_balance": "500.00",
                "color": "#00FF00",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert float(data["current_balance"]) == 500.0

    def test_creates_fund_with_negative_balance(self, authed_client, db_session):
        response = authed_client.post(
            "/api/sinking-funds",
            json={
                "name": "In Debt",
                "current_balance": "-200.00",
                "color": "#FF0000",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert float(data["current_balance"]) == -200.0

    def test_422_on_validation_error(self, authed_client):
        response = authed_client.post(
            "/api/sinking-funds",
            json={"name": ""},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422

    def test_api_csrf_exempt(self, authed_client):
        """API routes are CSRF-exempt (they use Bearer token auth instead)."""
        response = authed_client.post(
            "/api/sinking-funds",
            json={
                "name": "Test",
                "color": "#FF0000",
            },
        )
        assert response.status_code == 201


class TestApiFundsGet:
    def test_returns_single_fund(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/api/sinking-funds/{fund.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Bills"
        assert data["id"] == fund.id

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.get("/api/sinking-funds/99999")
        assert response.status_code == 404


class TestApiFundsUpdate:
    def test_updates_and_returns_200(
        self, authed_client, db_session, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        response = authed_client.put(
            f"/api/sinking-funds/{fund.id}",
            json={"name": "Updated Bills"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Bills"

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.put(
            "/api/sinking-funds/99999",
            json={"name": "X"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_422_on_validation_error(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.put(
            f"/api/sinking-funds/{fund.id}",
            json={"color": "not-a-color"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422


class TestApiFundsDelete:
    def test_soft_deletes_and_returns_200(
        self, authed_client, db_session, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        response = authed_client.delete(
            f"/api/sinking-funds/{fund.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(fund)
        assert fund.is_deleted is True

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.delete(
            "/api/sinking-funds/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_cannot_delete_system_fund(
        self, authed_client, db_session, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        fund.is_system = True
        db_session.commit()
        response = authed_client.delete(
            f"/api/sinking-funds/{fund.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 400
        assert "cannot be deleted" in response.json()["detail"]
        db_session.refresh(fund)
        assert fund.is_deleted is False


class TestBillsRecommendedAllocation:
    def test_recommended_shown_for_bills_fund(
        self, authed_client, sample_sinking_funds, sample_bills
    ):
        response = authed_client.get("/sinking-funds")
        assert response.status_code == 200
        assert "Rec:" in response.text

    def test_recommended_calculation_correct(
        self, authed_client, sample_sinking_funds, sample_bills
    ):
        # sample_bills: Rent $2400/monthly + Internet $89/monthly
        # Both monthly => annual = (2400+89)*12 = 29868, recommended = 29868/12 = 2489.00
        response = authed_client.get("/sinking-funds")
        assert "2,489.00" in response.text


class TestBufferWarningRemoved:
    def test_sinking_funds_page_has_no_30_day_banner(
        self, authed_client, db_session, sample_sinking_funds, sample_bills
    ):
        from datetime import datetime, timedelta

        from app.config import TIMEZONE

        soon = (datetime.now(TIMEZONE).date() + timedelta(days=5)).isoformat()
        for bill in sample_bills:
            bill.next_due_date = soon
        db_session.commit()

        response = authed_client.get("/sinking-funds")
        assert "Buffer Warning" not in response.text
        assert "bills due within the next 30 days" not in response.text

    def test_low_balance_is_reported_on_the_dashboard(
        self, authed_client, db_session, sample_sinking_funds, sample_bills
    ):
        from datetime import datetime, timedelta

        from app.config import TIMEZONE

        soon = (datetime.now(TIMEZONE).date() + timedelta(days=5)).isoformat()
        for bill in sample_bills:
            bill.next_due_date = soon
        sample_sinking_funds[0].current_balance = 0
        db_session.commit()

        dashboard = authed_client.get("/")
        assert 'id="bills-cover-banner"' in dashboard.text
        assert "Bills fund may run short in the next 3 months" in dashboard.text
        assert "of bills due by" in dashboard.text
        assert "See upcoming bills" in dashboard.text
        funds = authed_client.get("/sinking-funds")
        assert "Buffer Warning" not in funds.text


class TestSystemFundProtection:
    def test_delete_button_hidden_for_system_fund(
        self, authed_client, db_session, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        fund.is_system = True
        db_session.commit()
        response = authed_client.get("/sinking-funds")
        assert response.status_code == 200
        # Delete button should not appear for the system fund's row
        assert f'hx-delete="/sinking-funds/{fund.id}"' not in response.text

    def test_delete_button_shown_for_non_system_fund(
        self, authed_client, db_session, sample_sinking_funds
    ):
        non_system = sample_sinking_funds[1]  # "Savings"
        response = authed_client.get("/sinking-funds")
        assert response.status_code == 200
        assert f'hx-delete="/sinking-funds/{non_system.id}"' in response.text

    def test_is_system_in_api_response(
        self, authed_client, db_session, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        fund.is_system = True
        db_session.commit()
        response = authed_client.get(f"/api/sinking-funds/{fund.id}")
        assert response.status_code == 200
        assert response.json()["is_system"] is True


class TestSinkingFundDetail:
    def test_renders_detail_page(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}")
        assert response.status_code == 200
        assert fund.name in response.text

    def test_404_for_nonexistent_fund(self, authed_client):
        response = authed_client.get("/sinking-funds/99999")
        assert response.status_code == 404

    def test_unauthenticated_redirects(self, client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = client.get(f"/sinking-funds/{fund.id}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_shows_transactions_for_fund(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[0]
        txn = Transaction(
            date="2026-03-10",
            description="Test contribution",
            amount=100.00,
            category_id=sample_category.id,
            type="income",
            transaction_type="contribution",
            sinking_fund_id=fund.id,
        )
        db_session.add(txn)
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund.id}?month=3&year=2026")
        assert response.status_code == 200
        assert "Test contribution" in response.text
        assert "100.00" in response.text

    def test_excludes_transactions_from_other_funds(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund1, fund2 = sample_sinking_funds
        txn = Transaction(
            date="2026-03-10",
            description="Other fund transaction",
            amount=50.00,
            category_id=sample_category.id,
            type="expense",
            transaction_type="withdrawal",
            sinking_fund_id=fund2.id,
        )
        db_session.add(txn)
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund1.id}?month=3&year=2026")
        assert response.status_code == 200
        assert "Other fund transaction" not in response.text

    def test_excludes_transactions_outside_month(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[0]
        txn = Transaction(
            date="2026-02-15",
            description="Last month",
            amount=200.00,
            category_id=sample_category.id,
            type="income",
            transaction_type="contribution",
            sinking_fund_id=fund.id,
        )
        db_session.add(txn)
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund.id}?month=3&year=2026")
        assert response.status_code == 200
        assert "Last month" not in response.text

    def test_empty_state_message(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}?month=3&year=2026")
        assert response.status_code == 200
        assert "No transactions" in response.text

    def test_month_navigation_links(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}?month=3&year=2026")
        assert f"/sinking-funds/{fund.id}?month=2&year=2026" in response.text
        assert f"/sinking-funds/{fund.id}?month=4&year=2026" in response.text

    def test_month_navigation_wraps_year(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}?month=1&year=2026")
        assert f"/sinking-funds/{fund.id}?month=12&year=2025" in response.text

    def test_summary_totals(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[0]
        db_session.add_all(
            [
                Transaction(
                    date="2026-03-05",
                    description="Contribution",
                    amount=500.00,
                    category_id=sample_category.id,
                    type="income",
                    transaction_type="contribution",
                    sinking_fund_id=fund.id,
                ),
                Transaction(
                    date="2026-03-20",
                    description="Withdrawal",
                    amount=150.00,
                    category_id=sample_category.id,
                    type="expense",
                    transaction_type="withdrawal",
                    sinking_fund_id=fund.id,
                ),
            ]
        )
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund.id}?month=3&year=2026")
        assert response.status_code == 200
        assert "500.00" in response.text
        assert "150.00" in response.text
        assert "350.00" in response.text  # net

    def test_income_allocation_shows_as_positive(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[0]
        db_session.add(
            Transaction(
                date="2026-03-01",
                description="Income allocation to Bills fund",
                amount=300.00,
                category_id=sample_category.id,
                type="transfer",
                transaction_type="income_allocation",
                sinking_fund_id=fund.id,
            )
        )
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund.id}?month=3&year=2026")
        assert response.status_code == 200
        # Should appear with + sign and green colour, not red
        assert "+$300.00" in response.text

    def test_fund_names_link_to_detail(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get("/sinking-funds")
        assert f'href="/sinking-funds/{fund.id}"' in response.text


class TestSinkingFundForecast:
    def test_renders_forecast_page(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]  # Bills fund
        response = authed_client.get(f"/sinking-funds/{fund.id}/forecast")
        assert response.status_code == 200
        assert "Forecast" in response.text
        assert fund.name in response.text

    def test_404_for_nonexistent_fund(self, authed_client):
        response = authed_client.get("/sinking-funds/99999/forecast")
        assert response.status_code == 404

    def test_unauthenticated_redirects(self, client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = client.get(
            f"/sinking-funds/{fund.id}/forecast", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_shows_12_month_rows(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}/forecast")
        assert response.status_code == 200
        # 12 months appear — check for several distinct month names
        text = response.text
        assert "January" in text or "February" in text or "March" in text

    def test_shows_bill_names_in_forecast(
        self, authed_client, db_session, sample_sinking_funds, sample_bills
    ):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}/forecast")
        assert response.status_code == 200
        # Both sample bills should appear somewhere in the 12-month forecast
        assert "Rent" in response.text
        assert "Internet" in response.text

    def test_detail_page_shows_forecast_tab_for_bills(
        self, authed_client, sample_sinking_funds
    ):
        bills_fund = next(f for f in sample_sinking_funds if f.name == "Bills")
        response = authed_client.get(f"/sinking-funds/{bills_fund.id}")
        assert response.status_code == 200
        assert f"/sinking-funds/{bills_fund.id}/forecast" in response.text

    def test_detail_page_no_forecast_tab_for_non_bills(
        self, authed_client, sample_sinking_funds
    ):
        savings_fund = next(f for f in sample_sinking_funds if f.name == "Savings")
        response = authed_client.get(f"/sinking-funds/{savings_fund.id}")
        assert response.status_code == 200
        assert f"/sinking-funds/{savings_fund.id}/forecast" not in response.text


class TestSinkingFundHistory:
    def test_renders_history_page(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}/history")
        assert response.status_code == 200
        assert "History" in response.text
        assert fund.name in response.text

    def test_404_for_nonexistent_fund(self, authed_client):
        response = authed_client.get("/sinking-funds/99999/history")
        assert response.status_code == 404

    def test_unauthenticated_redirects(self, client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = client.get(
            f"/sinking-funds/{fund.id}/history", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_shows_12_month_rows(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}/history")
        assert response.status_code == 200
        # At least a few distinct month names should appear
        text = response.text
        months_found = sum(
            1
            for m in [
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            ]
            if m in text
        )
        assert months_found >= 12

    def test_aggregates_income_and_expenses(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[0]
        db_session.add_all(
            [
                Transaction(
                    date="2026-03-05",
                    description="March contribution",
                    amount=500.00,
                    category_id=sample_category.id,
                    type="income",
                    transaction_type="contribution",
                    sinking_fund_id=fund.id,
                ),
                Transaction(
                    date="2026-03-20",
                    description="March withdrawal",
                    amount=150.00,
                    category_id=sample_category.id,
                    type="expense",
                    transaction_type="withdrawal",
                    sinking_fund_id=fund.id,
                ),
                Transaction(
                    date="2026-02-10",
                    description="Feb contribution",
                    amount=400.00,
                    category_id=sample_category.id,
                    type="income",
                    transaction_type="contribution",
                    sinking_fund_id=fund.id,
                ),
            ]
        )
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund.id}/history")
        assert response.status_code == 200
        assert "500.00" in response.text
        assert "150.00" in response.text
        assert "400.00" in response.text

    def test_closing_balance_reconstructed_from_current(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[0]
        fund.current_balance = 1000.00
        db_session.add(
            Transaction(
                date="2026-03-01",
                description="March contribution",
                amount=300.00,
                category_id=sample_category.id,
                type="income",
                transaction_type="contribution",
                sinking_fund_id=fund.id,
            )
        )
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund.id}/history")
        assert response.status_code == 200
        # Current month closing balance = current_balance = 1000.00
        assert "1,000.00" in response.text
        # Previous month closing balance = 1000 - 300 = 700.00
        assert "700.00" in response.text

    def test_excludes_transactions_outside_12_months(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[0]
        # Transaction 2 years ago — should not appear
        db_session.add(
            Transaction(
                date="2024-01-15",
                description="Old transaction",
                amount=999.00,
                category_id=sample_category.id,
                type="income",
                transaction_type="contribution",
                sinking_fund_id=fund.id,
            )
        )
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund.id}/history")
        assert response.status_code == 200
        assert "Old transaction" not in response.text

    def test_empty_state_message(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.get(f"/sinking-funds/{fund.id}/history")
        # No transactions but page should render (shows months with zero values)
        assert response.status_code == 200

    def test_history_tab_on_all_funds(self, authed_client, sample_sinking_funds):
        for fund in sample_sinking_funds:
            response = authed_client.get(f"/sinking-funds/{fund.id}")
            assert response.status_code == 200
            assert f"/sinking-funds/{fund.id}/history" in response.text

    def test_forecast_and_history_tabs_on_bills_fund(
        self, authed_client, sample_sinking_funds
    ):
        bills_fund = next(f for f in sample_sinking_funds if f.name == "Bills")
        response = authed_client.get(f"/sinking-funds/{bills_fund.id}")
        assert response.status_code == 200
        assert f"/sinking-funds/{bills_fund.id}/forecast" in response.text
        assert f"/sinking-funds/{bills_fund.id}/history" in response.text

    def test_month_links_point_to_detail_page(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[0]
        db_session.add(
            Transaction(
                date="2026-03-10",
                description="Some transaction",
                amount=100.00,
                category_id=sample_category.id,
                type="income",
                transaction_type="contribution",
                sinking_fund_id=fund.id,
            )
        )
        db_session.commit()

        response = authed_client.get(f"/sinking-funds/{fund.id}/history")
        assert response.status_code == 200
        # Expandable row for March 2026 should link to the detail page
        assert f"/sinking-funds/{fund.id}?month=3&year=2026" in response.text


def _runway_block(html: str) -> str:
    match = re.search(r'<div id="fund-runway".*?</div>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _age_fund(db_session, fund, balance):
    fund.created_at = datetime(2020, 1, 1)
    fund.current_balance = balance
    db_session.commit()


def _flow(db_session, fund, category_id, *, amount, txn_type):
    today = datetime.now(TIMEZONE).date()
    previous_end = date(today.year, today.month, 1) - timedelta(days=1)
    when = date(previous_end.year, previous_end.month, 15).isoformat()
    db_session.add(
        Transaction(
            date=when,
            description="Runway row",
            amount=amount,
            category_id=category_id,
            type=txn_type,
            transaction_type="regular" if txn_type == "expense" else "contribution",
            sinking_fund_id=fund.id,
        )
    )
    db_session.commit()


class TestFundRunwayPage:
    def test_running_down_warning_and_card(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        short = sample_sinking_funds[1]
        _age_fund(db_session, short, 400)
        _flow(
            db_session,
            short,
            sample_category.id,
            amount=1200,
            txn_type="expense",
        )
        response = authed_client.get(f"/sinking-funds/{short.id}")
        block = _runway_block(response.text)
        assert response.text.index('id="fund-runway"') < response.text.index(
            "gw-month-nav"
        )
        assert 'aria-label="Fund runway"' in block
        assert "gw-warning-banner" in block
        assert "gw-card" not in block
        assert "At recent spending, this fund runs out in about 2 months" in block

        long = SinkingFund(
            name="Car",
            color="#2255AA",
            current_balance=1800,
            created_at=datetime(2020, 1, 1),
        )
        db_session.add(long)
        db_session.commit()
        _flow(db_session, long, sample_category.id, amount=600, txn_type="expense")
        calm = authed_client.get(f"/sinking-funds/{long.id}")
        calm_block = _runway_block(calm.text)
        assert 'class="gw-card gw-section"' in calm_block
        assert "gw-warning-banner" not in calm_block
        assert "runs out in about" in calm_block

    def test_payoff_uses_the_card_and_stuck_debt_uses_the_warning(
        self, authed_client, db_session, sample_category
    ):
        paying = SinkingFund(
            name="Loan",
            color="#884400",
            current_balance=-500,
            created_at=datetime(2020, 1, 1),
        )
        stuck = SinkingFund(
            name="Family",
            color="#440088",
            current_balance=-1500,
            created_at=datetime(2020, 1, 1),
        )
        db_session.add_all([paying, stuck])
        db_session.commit()
        _flow(
            db_session,
            paying,
            sample_category.id,
            amount=1200,
            txn_type="transfer",
        )

        payoff = _runway_block(authed_client.get(f"/sinking-funds/{paying.id}").text)
        assert "back to zero in about" in payoff
        assert 'class="gw-card gw-section"' in payoff
        assert "gw-warning-banner" not in payoff

        warning = _runway_block(authed_client.get(f"/sinking-funds/{stuck.id}").text)
        assert "in the red" in warning
        assert "being paid down" in warning
        assert "gw-warning-banner" in warning
        assert "gw-card" not in warning

    def test_runway_ignores_the_viewed_month(
        self, authed_client, db_session, sample_sinking_funds, sample_category
    ):
        fund = sample_sinking_funds[1]
        _age_fund(db_session, fund, 400)
        _flow(db_session, fund, sample_category.id, amount=1200, txn_type="expense")
        january = authed_client.get(f"/sinking-funds/{fund.id}?month=1&year=2024")
        august = authed_client.get(f"/sinking-funds/{fund.id}?month=8&year=2026")
        assert _runway_block(january.text) == _runway_block(august.text)

    def test_bills_fund_points_at_forecast(self, authed_client, sample_sinking_funds):
        bills = next(fund for fund in sample_sinking_funds if fund.name == "Bills")
        block = _runway_block(authed_client.get(f"/sinking-funds/{bills.id}").text)
        assert "Bills come in lumps, so runway isn't shown here." in block
        assert f'href="/sinking-funds/{bills.id}/forecast"' in block
        assert ">Forecast</a>" in block
        assert "runs out" not in block
        assert "in the red" not in block

    def test_list_and_dashboard_omit_runway(self, authed_client, sample_sinking_funds):
        listing = authed_client.get("/sinking-funds")
        dashboard = authed_client.get("/")
        assert listing.status_code == 200
        assert dashboard.status_code == 200
        assert "fund-runway" not in listing.text
        assert "fund-runway" not in dashboard.text
