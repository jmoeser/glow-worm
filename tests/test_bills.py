from decimal import Decimal

import pytest

from app.models import RecurringBill, SinkingFund, Transaction
from app.tasks import process_due_bills


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
                "start_date": "2026-01-01",
                "next_due_date": "2026-02-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        bill = db_session.query(RecurringBill).filter(RecurringBill.name == "Power").first()
        assert bill is not None
        assert float(bill.amount) == 150.0
        assert bill.category_id == sample_category.id

    def test_returns_updated_table_body(self, authed_client, sample_category, sample_bills):
        response = authed_client.post(
            "/bills",
            data={
                "name": "Water",
                "debtor_provider": "Water Co",
                "amount": "60.00",
                "frequency": "quarterly",
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

    def test_api_csrf_exempt(self, authed_client, sample_category):
        """API routes are CSRF-exempt (they use Bearer token auth instead)."""
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
        assert response.status_code == 201


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


# ---------------------------------------------------------------------------
# bill_type / variable bill tests
# ---------------------------------------------------------------------------

@pytest.fixture
def bills_fund(db_session):
    fund = SinkingFund(
        name="Bills",
        color="#FF0000",
        monthly_allocation=0,
        current_balance=500,
    )
    db_session.add(fund)
    db_session.commit()
    db_session.refresh(fund)
    return fund


@pytest.fixture
def variable_bill(db_session, sample_category):
    bill = RecurringBill(
        name="Electricity",
        amount=150,
        debtor_provider="Power Co",
        start_date="2026-01-01",
        frequency="monthly",
        category_id=sample_category.id,
        next_due_date="2026-02-01",
        bill_type="variable",
    )
    db_session.add(bill)
    db_session.commit()
    db_session.refresh(bill)
    return bill


class TestBillTypeDefault:
    def test_default_bill_type_is_fixed(self, authed_client, db_session, sample_category):
        authed_client.post(
            "/api/bills",
            json={
                "name": "Rent",
                "amount": "1000",
                "debtor_provider": "Landlord",
                "start_date": "2026-01-01",
                "frequency": "monthly",
                "category_id": sample_category.id,
                "next_due_date": "2026-02-01",
            },
        )
        bill = db_session.query(RecurringBill).filter(RecurringBill.name == "Rent").first()
        assert bill.bill_type == "fixed"

    def test_bill_type_appears_in_api_response(self, authed_client, sample_bills):
        bill = sample_bills[0]
        response = authed_client.get(f"/api/bills/{bill.id}")
        assert response.status_code == 200
        data = response.json()
        assert "bill_type" in data
        assert data["bill_type"] == "fixed"

    def test_create_variable_bill_via_api(self, authed_client, db_session, sample_category):
        response = authed_client.post(
            "/api/bills",
            json={
                "name": "Electricity",
                "amount": "150",
                "debtor_provider": "Power Co",
                "start_date": "2026-01-01",
                "frequency": "monthly",
                "category_id": sample_category.id,
                "next_due_date": "2026-02-01",
                "bill_type": "variable",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["bill_type"] == "variable"


class TestProcessDueBillsVariableSkip:
    def test_variable_bill_skipped_by_scheduler(self, db_session, bills_fund, variable_bill):
        original_due = variable_bill.next_due_date
        process_due_bills(db=db_session)

        db_session.refresh(variable_bill)
        db_session.refresh(bills_fund)

        # next_due_date should be unchanged
        assert variable_bill.next_due_date == original_due

        # No transaction should have been created
        txn = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == variable_bill.id)
            .first()
        )
        assert txn is None

    def test_fixed_bill_still_autopays(self, db_session, sample_category, bills_fund):
        fixed_bill = RecurringBill(
            name="Rent",
            amount=1000,
            debtor_provider="Landlord",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
            bill_type="fixed",
        )
        db_session.add(fixed_bill)
        db_session.commit()
        db_session.refresh(fixed_bill)

        process_due_bills(db=db_session)

        db_session.refresh(fixed_bill)
        db_session.refresh(bills_fund)

        txn = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == fixed_bill.id)
            .first()
        )
        assert txn is not None
        assert float(txn.amount) == 1000.0
        # next_due_date should have advanced
        assert fixed_bill.next_due_date != "2026-02-01"


class TestBillsPayFormGet:
    def test_returns_pay_form_for_variable_bill(self, authed_client, variable_bill):
        response = authed_client.get(f"/bills/{variable_bill.id}/pay")
        assert response.status_code == 200
        assert "Record Payment" in response.text
        assert 'name="amount"' in response.text
        assert 'name="date"' in response.text

    def test_404_for_nonexistent_bill(self, authed_client):
        response = authed_client.get("/bills/99999/pay")
        assert response.status_code == 404


class TestBillsPayPost:
    def test_records_payment_creates_transaction(
        self, authed_client, db_session, variable_bill, bills_fund
    ):
        original_due = variable_bill.next_due_date
        response = authed_client.post(
            f"/bills/{variable_bill.id}/pay",
            data={"amount": "180.50", "date": "2026-02-21"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200

        txn = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == variable_bill.id)
            .first()
        )
        assert txn is not None
        assert float(txn.amount) == 180.50
        assert txn.sinking_fund_id == bills_fund.id

        db_session.refresh(variable_bill)
        assert variable_bill.next_due_date != original_due

    def test_deducts_bills_fund_balance(
        self, authed_client, db_session, variable_bill, bills_fund
    ):
        original_balance = float(bills_fund.current_balance)
        authed_client.post(
            f"/bills/{variable_bill.id}/pay",
            data={"amount": "100.00", "date": "2026-02-21"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        db_session.refresh(bills_fund)
        assert float(bills_fund.current_balance) == original_balance - 100.00

    def test_returns_updated_bill_row(self, authed_client, variable_bill, bills_fund):
        response = authed_client.post(
            f"/bills/{variable_bill.id}/pay",
            data={"amount": "150.00", "date": "2026-02-21"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert variable_bill.name in response.text

    def test_error_if_bills_fund_not_found(self, authed_client, variable_bill):
        # No bills_fund fixture — fund doesn't exist
        response = authed_client.post(
            f"/bills/{variable_bill.id}/pay",
            data={"amount": "100.00", "date": "2026-02-21"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Bills sinking fund not found" in response.text

    def test_error_on_invalid_amount(self, authed_client, variable_bill, bills_fund):
        response = authed_client.post(
            f"/bills/{variable_bill.id}/pay",
            data={"amount": "abc", "date": "2026-02-21"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Invalid amount" in response.text

    def test_403_without_csrf(self, authed_client, variable_bill, bills_fund):
        response = authed_client.post(
            f"/bills/{variable_bill.id}/pay",
            data={"amount": "100.00", "date": "2026-02-21"},
        )
        assert response.status_code == 403


class TestApiBillsPay:
    def test_records_payment_returns_200(
        self, authed_client, db_session, variable_bill, bills_fund
    ):
        response = authed_client.post(
            f"/api/bills/{variable_bill.id}/pay",
            json={"amount": "200.00", "date": "2026-02-21"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "transaction" in data
        assert "bill" in data
        assert float(data["transaction"]["amount"]) == 200.00
        assert data["bill"]["id"] == variable_bill.id

    def test_advances_next_due_date(
        self, authed_client, db_session, variable_bill, bills_fund
    ):
        original_due = variable_bill.next_due_date
        authed_client.post(
            f"/api/bills/{variable_bill.id}/pay",
            json={"amount": "100", "date": "2026-02-21"},
        )
        db_session.refresh(variable_bill)
        assert variable_bill.next_due_date != original_due

    def test_404_for_nonexistent_bill(self, authed_client):
        response = authed_client.post(
            "/api/bills/99999/pay",
            json={"amount": "100", "date": "2026-02-21"},
        )
        assert response.status_code == 404

    def test_422_on_missing_amount(self, authed_client, variable_bill, bills_fund):
        response = authed_client.post(
            f"/api/bills/{variable_bill.id}/pay",
            json={"date": "2026-02-21"},
        )
        assert response.status_code == 422

    def test_422_on_invalid_date(self, authed_client, variable_bill, bills_fund):
        response = authed_client.post(
            f"/api/bills/{variable_bill.id}/pay",
            json={"amount": "100", "date": "not-a-date"},
        )
        assert response.status_code == 422

    def test_400_if_bills_fund_not_found(self, authed_client, variable_bill):
        # No bills_fund fixture
        response = authed_client.post(
            f"/api/bills/{variable_bill.id}/pay",
            json={"amount": "100", "date": "2026-02-21"},
        )
        assert response.status_code == 400
        assert "Bills sinking fund not found" in response.json()["detail"]
