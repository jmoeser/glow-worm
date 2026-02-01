from app.models import RecurringBill


class TestBillsPageGet:
    def test_renders_page_with_table(self, authed_client):
        response = authed_client.get("/bills")
        assert response.status_code == 200
        assert "Recurring Bills" in response.text
        assert "Name" in response.text
        assert "Provider" in response.text
        assert "Amount" in response.text
        assert "Frequency" in response.text
        assert "Next Due" in response.text

    def test_lists_active_bills(self, authed_client, sample_bills):
        response = authed_client.get("/bills")
        assert response.status_code == 200
        assert "Rent" in response.text
        assert "Internet" in response.text
        assert "Landlord" in response.text
        assert "ISP" in response.text

    def test_excludes_inactive_bills(self, authed_client, db_session, sample_bills):
        sample_bills[0].is_active = False
        db_session.commit()
        response = authed_client.get("/bills")
        # Check within the table body — "Rent" should not appear as a <td> cell
        assert ">Rent<" not in response.text
        assert ">Internet<" in response.text

    def test_shows_add_form(self, authed_client):
        response = authed_client.get("/bills")
        assert "Add New Bill" in response.text
        assert 'name="name"' in response.text
        assert 'name="amount"' in response.text

    def test_shows_category_dropdown(self, authed_client, sample_category):
        response = authed_client.get("/bills")
        assert sample_category.name in response.text
        assert f'value="{sample_category.id}"' in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/bills", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestBillsPagePost:
    def test_creates_new_bill(self, authed_client, db_session, sample_category):
        response = authed_client.post(
            "/bills",
            data={
                "name": "Power",
                "debtor_provider": "Energy Co",
                "amount": "150.00",
                "frequency": "monthly",
                "category_id": str(sample_category.id),
                "start_date": "2026-01-01",
                "next_due_date": "2026-02-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        bill = db_session.query(RecurringBill).filter(RecurringBill.name == "Power").first()
        assert bill is not None
        assert float(bill.amount) == 150.0

    def test_returns_updated_table_body(self, authed_client, sample_category, sample_bills):
        response = authed_client.post(
            "/bills",
            data={
                "name": "Water",
                "debtor_provider": "Water Co",
                "amount": "60.00",
                "frequency": "quarterly",
                "category_id": str(sample_category.id),
                "start_date": "2026-01-01",
                "next_due_date": "2026-04-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Water" in response.text
        # Existing bills should also be in the refreshed table body
        assert "Rent" in response.text

    def test_error_on_missing_required_fields(self, authed_client):
        response = authed_client.post(
            "/bills",
            data={"name": "", "debtor_provider": "", "amount": "100"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_error_on_invalid_amount(self, authed_client, sample_category):
        response = authed_client.post(
            "/bills",
            data={
                "name": "Test",
                "debtor_provider": "Someone",
                "amount": "abc",
                "frequency": "monthly",
                "category_id": str(sample_category.id),
                "start_date": "2026-01-01",
                "next_due_date": "2026-02-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Invalid amount" in response.text

    def test_403_without_csrf(self, authed_client, sample_category):
        response = authed_client.post(
            "/bills",
            data={
                "name": "Test",
                "debtor_provider": "Someone",
                "amount": "100",
                "frequency": "monthly",
                "category_id": str(sample_category.id),
                "start_date": "2026-01-01",
                "next_due_date": "2026-02-01",
            },
        )
        assert response.status_code == 403


class TestBillsEditGet:
    def test_returns_edit_form_row(self, authed_client, sample_bills):
        bill = sample_bills[0]
        response = authed_client.get(f"/bills/{bill.id}/edit")
        assert response.status_code == 200
        assert f'value="{bill.name}"' in response.text
        assert f'value="{bill.debtor_provider}"' in response.text
        assert 'name="amount"' in response.text

    def test_404_for_nonexistent_bill(self, authed_client):
        response = authed_client.get("/bills/99999/edit")
        assert response.status_code == 404


class TestBillsEditPost:
    def test_updates_bill_fields(self, authed_client, db_session, sample_bills):
        bill = sample_bills[0]
        response = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "name": "Updated Rent",
                "debtor_provider": "New Landlord",
                "amount": "2600.00",
                "frequency": "monthly",
                "next_due_date": "2026-03-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(bill)
        assert bill.name == "Updated Rent"
        assert bill.debtor_provider == "New Landlord"
        assert float(bill.amount) == 2600.0

    def test_returns_updated_row(self, authed_client, sample_bills):
        bill = sample_bills[0]
        response = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "name": "Updated Rent",
                "debtor_provider": "New Landlord",
                "amount": "2600.00",
                "frequency": "monthly",
                "next_due_date": "2026-03-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert "Updated Rent" in response.text
        assert "New Landlord" in response.text

    def test_404_for_nonexistent_bill(self, authed_client):
        response = authed_client.post(
            "/bills/99999",
            data={"name": "X"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestBillsDelete:
    def test_deactivates_bill(self, authed_client, db_session, sample_bills):
        bill = sample_bills[0]
        response = authed_client.delete(
            f"/bills/{bill.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(bill)
        assert bill.is_active is False

    def test_returns_empty_response(self, authed_client, sample_bills):
        bill = sample_bills[0]
        response = authed_client.delete(
            f"/bills/{bill.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.text == ""

    def test_404_for_nonexistent_bill(self, authed_client):
        response = authed_client.delete(
            "/bills/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_403_without_csrf(self, authed_client, sample_bills):
        bill = sample_bills[0]
        response = authed_client.delete(f"/bills/{bill.id}")
        assert response.status_code == 403


class TestApiBillsList:
    def test_returns_json_list(self, authed_client, sample_bills):
        response = authed_client.get("/api/bills")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        names = {b["name"] for b in data}
        assert "Rent" in names
        assert "Internet" in names

    def test_excludes_inactive_bills(self, authed_client, db_session, sample_bills):
        sample_bills[0].is_active = False
        db_session.commit()
        response = authed_client.get("/api/bills")
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Internet"

    def test_unauthenticated_redirects(self, client):
        response = client.get("/api/bills", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestApiBillsCreate:
    def test_creates_bill_returns_201(self, authed_client, db_session, sample_category):
        response = authed_client.post(
            "/api/bills",
            json={
                "name": "Gas",
                "amount": "120.50",
                "debtor_provider": "Gas Corp",
                "start_date": "2026-01-01",
                "frequency": "monthly",
                "category_id": sample_category.id,
                "next_due_date": "2026-02-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Gas"
        assert float(data["amount"]) == 120.50
        assert data["is_active"] is True

    def test_422_on_validation_error(self, authed_client):
        response = authed_client.post(
            "/api/bills",
            json={"name": ""},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422

    def test_403_without_csrf(self, authed_client, sample_category):
        response = authed_client.post(
            "/api/bills",
            json={
                "name": "Gas",
                "amount": "120",
                "debtor_provider": "Gas Corp",
                "start_date": "2026-01-01",
                "frequency": "monthly",
                "category_id": sample_category.id,
                "next_due_date": "2026-02-01",
            },
        )
        assert response.status_code == 403


class TestApiBillsGet:
    def test_returns_single_bill(self, authed_client, sample_bills):
        bill = sample_bills[0]
        response = authed_client.get(f"/api/bills/{bill.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Rent"
        assert data["id"] == bill.id

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.get("/api/bills/99999")
        assert response.status_code == 404


class TestApiBillsUpdate:
    def test_updates_and_returns_200(self, authed_client, db_session, sample_bills):
        bill = sample_bills[0]
        response = authed_client.put(
            f"/api/bills/{bill.id}",
            json={"name": "Updated Rent", "amount": "2600"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Rent"
        assert float(data["amount"]) == 2600.0

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.put(
            "/api/bills/99999",
            json={"name": "X"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_422_on_validation_error(self, authed_client, sample_bills):
        bill = sample_bills[0]
        response = authed_client.put(
            f"/api/bills/{bill.id}",
            json={"amount": "-10"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422


class TestApiBillsDelete:
    def test_deactivates_and_returns_200(self, authed_client, db_session, sample_bills):
        bill = sample_bills[0]
        response = authed_client.delete(
            f"/api/bills/{bill.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(bill)
        assert bill.is_active is False

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.delete(
            "/api/bills/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404
