from decimal import Decimal

from app.models import RecurringBill, SinkingFund


class TestSinkingFundsPageGet:
    def test_renders_page_with_table(self, authed_client):
        response = authed_client.get("/sinking-funds")
        assert response.status_code == 200
        assert "Sinking Funds" in response.text
        assert "Name" in response.text
        assert "Description" in response.text
        assert "Monthly Allocation" in response.text
        assert "Balance" in response.text

    def test_lists_active_funds(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/sinking-funds")
        assert response.status_code == 200
        assert "Bills" in response.text
        assert "Savings" in response.text

    def test_excludes_deleted_funds(self, authed_client, db_session, sample_sinking_funds):
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
        assert 'name="monthly_allocation"' in response.text
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
                "monthly_allocation": "200.00",
                "color": "#FF5733",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        fund = db_session.query(SinkingFund).filter(SinkingFund.name == "Emergency").first()
        assert fund is not None
        assert float(fund.monthly_allocation) == 200.0
        assert fund.color == "#FF5733"
        assert fund.description == "For emergencies"

    def test_returns_updated_table_body(self, authed_client, sample_sinking_funds):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Holiday",
                "monthly_allocation": "100.00",
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
                "monthly_allocation": "100.00",
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
                "monthly_allocation": "100.00",
                "color": "",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_error_on_invalid_allocation(self, authed_client):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Test",
                "monthly_allocation": "abc",
                "color": "#FF0000",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Invalid" in response.text

    def test_current_balance_defaults_to_zero(self, authed_client, db_session):
        authed_client.post(
            "/sinking-funds",
            data={
                "name": "New Fund",
                "monthly_allocation": "50.00",
                "color": "#123456",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        fund = db_session.query(SinkingFund).filter(SinkingFund.name == "New Fund").first()
        assert fund is not None
        assert float(fund.current_balance) == 0

    def test_creates_fund_with_initial_balance(self, authed_client, db_session):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Starter",
                "monthly_allocation": "100.00",
                "current_balance": "250.00",
                "color": "#AABB00",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        fund = db_session.query(SinkingFund).filter(SinkingFund.name == "Starter").first()
        assert fund is not None
        assert float(fund.current_balance) == 250.0

    def test_creates_fund_with_negative_balance(self, authed_client, db_session):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Overdrawn",
                "monthly_allocation": "50.00",
                "current_balance": "-120.50",
                "color": "#FF0000",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        fund = db_session.query(SinkingFund).filter(SinkingFund.name == "Overdrawn").first()
        assert fund is not None
        assert float(fund.current_balance) == -120.5

    def test_error_on_invalid_initial_balance(self, authed_client):
        response = authed_client.post(
            "/sinking-funds",
            data={
                "name": "Bad Balance",
                "monthly_allocation": "50.00",
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
                "monthly_allocation": "100.00",
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
        assert 'name="monthly_allocation"' in response.text
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
                "monthly_allocation": "500.00",
                "color": "#AABBCC",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(fund)
        assert fund.name == "Updated Bills"
        assert fund.description == "Updated description"
        assert float(fund.monthly_allocation) == 500.0
        assert fund.color == "#AABBCC"

    def test_returns_updated_row(self, authed_client, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.post(
            f"/sinking-funds/{fund.id}",
            data={
                "name": "Updated Bills",
                "monthly_allocation": "500.00",
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

    def test_excludes_deleted_funds(self, authed_client, db_session, sample_sinking_funds):
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
                "monthly_allocation": "150.00",
                "color": "#FF5733",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Holiday"
        assert float(data["monthly_allocation"]) == 150.0
        assert data["is_deleted"] is False

    def test_creates_fund_with_initial_balance(self, authed_client, db_session):
        response = authed_client.post(
            "/api/sinking-funds",
            json={
                "name": "Pre-funded",
                "monthly_allocation": "100.00",
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
                "monthly_allocation": "50.00",
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

    def test_403_without_csrf(self, authed_client):
        response = authed_client.post(
            "/api/sinking-funds",
            json={
                "name": "Test",
                "monthly_allocation": "100",
                "color": "#FF0000",
            },
        )
        assert response.status_code == 403


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
    def test_updates_and_returns_200(self, authed_client, db_session, sample_sinking_funds):
        fund = sample_sinking_funds[0]
        response = authed_client.put(
            f"/api/sinking-funds/{fund.id}",
            json={"name": "Updated Bills", "monthly_allocation": "300"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Bills"
        assert float(data["monthly_allocation"]) == 300.0

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
            json={"monthly_allocation": "-10"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422


class TestApiFundsDelete:
    def test_soft_deletes_and_returns_200(self, authed_client, db_session, sample_sinking_funds):
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
        assert "2489.00" in response.text


class TestBufferWarning:
    def test_warning_shown_when_balance_low(
        self, authed_client, db_session, sample_sinking_funds, sample_bills
    ):
        # Bills fund has balance 0, bills due soon => warning
        # Set bill next_due_date to be within 30 days
        from datetime import date, timedelta

        soon = (date.today() + timedelta(days=5)).isoformat()
        for bill in sample_bills:
            bill.next_due_date = soon
        db_session.commit()

        response = authed_client.get("/sinking-funds")
        assert "Buffer Warning" in response.text

    def test_no_warning_when_balance_sufficient(
        self, authed_client, db_session, sample_sinking_funds, sample_bills
    ):
        from datetime import date, timedelta

        soon = (date.today() + timedelta(days=5)).isoformat()
        for bill in sample_bills:
            bill.next_due_date = soon
        db_session.commit()

        # Set Bills fund balance high enough
        bills_fund = sample_sinking_funds[0]  # "Bills"
        bills_fund.current_balance = 10000
        db_session.commit()

        response = authed_client.get("/sinking-funds")
        assert "Buffer Warning" not in response.text
