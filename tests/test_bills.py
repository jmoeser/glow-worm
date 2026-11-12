import json
from decimal import Decimal

import pytest

from app.models import RecurringBill, SinkingFund, Transaction
from app.tasks import process_due_bills


class TestBillsPageGet:
    def test_renders_page_with_table(self, authed_client):
        response = authed_client.get("/bills")
        assert response.status_code == 200
        assert "Recurring payments" in response.text
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
        assert "Add new" in response.text
        assert 'name="name"' in response.text
        assert 'name="amount"' in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/bills", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_shows_forecast_link_when_bills_fund_exists(
        self, authed_client, db_session
    ):
        fund = SinkingFund(name="Bills", color="#FF0000", current_balance=0)
        db_session.add(fund)
        db_session.commit()
        response = authed_client.get("/bills")
        assert response.status_code == 200
        assert f"/sinking-funds/{fund.id}/forecast" in response.text

    def test_no_forecast_link_without_bills_fund(self, authed_client):
        response = authed_client.get("/bills")
        assert response.status_code == 200
        assert "/forecast" not in response.text


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
        bill = (
            db_session.query(RecurringBill)
            .filter(RecurringBill.name == "Power")
            .first()
        )
        assert bill is not None
        assert float(bill.amount) == 150.0
        assert bill.category_id == sample_category.id

    def test_returns_updated_table_body(
        self, authed_client, sample_category, sample_bills
    ):
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
                "next_due_date_was": "2026-02-01",
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
                "next_due_date_was": "2026-02-01",
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
    def test_default_bill_type_is_fixed(
        self, authed_client, db_session, sample_category
    ):
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
        bill = (
            db_session.query(RecurringBill).filter(RecurringBill.name == "Rent").first()
        )
        assert bill.bill_type == "fixed"

    def test_bill_type_appears_in_api_response(self, authed_client, sample_bills):
        bill = sample_bills[0]
        response = authed_client.get(f"/api/bills/{bill.id}")
        assert response.status_code == 200
        data = response.json()
        assert "bill_type" in data
        assert data["bill_type"] == "fixed"

    def test_create_variable_bill_via_api(
        self, authed_client, db_session, sample_category
    ):
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
    def test_variable_bill_skipped_by_scheduler(
        self, db_session, bills_fund, variable_bill
    ):
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


class TestBillsConfirmRow:
    def test_variable_row_has_confirm_and_skip_not_pay(
        self, authed_client, variable_bill
    ):
        page = authed_client.get("/bills")
        assert "Confirm" in page.text
        assert "Skip" in page.text
        assert ">Pay<" not in page.text
        assert f"/bills/{variable_bill.id}/pay" not in page.text

    def test_confirm_form_shows_due_date_and_source(
        self, authed_client, variable_bill, bills_fund
    ):
        response = authed_client.get(f"/bills/{variable_bill.id}/confirm")
        assert response.status_code == 200
        assert "Due 1 Feb 2026" in response.text
        assert 'name="amount"' in response.text
        assert 'name="due_date"' in response.text
        assert "From Bills fund" in response.text
        assert "Record Payment" not in response.text

    def test_confirm_404_for_nonexistent_bill(self, authed_client):
        response = authed_client.get("/bills/99999/confirm")
        assert response.status_code == 404

    def test_confirm_logs_on_the_due_date(
        self, authed_client, db_session, variable_bill, bills_fund
    ):
        original_due = variable_bill.next_due_date
        response = authed_client.post(
            f"/bills/{variable_bill.id}/confirm",
            data={"amount": "180.50", "due_date": original_due},
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
        assert txn.date == original_due
        assert txn.sinking_fund_id == bills_fund.id
        db_session.refresh(variable_bill)
        assert variable_bill.next_due_date != original_due
        db_session.refresh(bills_fund)
        assert float(bills_fund.current_balance) == 500 - 180.50

    def test_confirm_invalid_amount(self, authed_client, variable_bill, bills_fund):
        response = authed_client.post(
            f"/bills/{variable_bill.id}/confirm",
            data={"amount": "abc", "due_date": variable_bill.next_due_date},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Invalid amount" in response.text

    def test_403_without_csrf(self, authed_client, variable_bill, bills_fund):
        response = authed_client.post(
            f"/bills/{variable_bill.id}/confirm",
            data={"amount": "100.00", "due_date": variable_bill.next_due_date},
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
        body = response.json()
        assert body["detail"] == "The Bills fund is missing."
        assert body["code"] == "source_missing"


class TestPaidWith:
    def _method(self, db, **kwargs):
        from app.models import PaymentMethod

        values = {
            "nickname": "Everyday Visa",
            "kind": "card",
            "last4": "4321",
            "expiry_month": 10,
            "expiry_year": 2026,
            "owner": "Jordan",
        }
        values.update(kwargs)
        method = PaymentMethod(**values)
        db.add(method)
        db.commit()
        db.refresh(method)
        return method

    def test_column_badge_and_sort_map(self, authed_client, db_session, sample_bills):
        method = self._method(db_session)
        sample_bills[0].payment_method_id = method.id
        db_session.commit()
        page = authed_client.get("/bills")
        assert "Paid with" in page.text
        assert "Everyday Visa ending 4321" in page.text
        assert "Expired" in page.text
        assert "Not set" in page.text
        assert "paid_with: 2" in page.text
        assert "paid_from: 3" in page.text
        assert "next_due_date: 7" in page.text
        assert "No payment method: 1" in page.text

    def test_edit_keeps_link_when_key_missing_and_blank_clears(
        self, authed_client, db_session, sample_bills
    ):
        method = self._method(
            db_session, kind="manual", last4=None, expiry_month=None, expiry_year=None
        )
        bill = sample_bills[0]
        bill.payment_method_id = method.id
        db_session.commit()
        kept = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "name": "Rent",
                "next_due_date": "2026-02-01",
                "next_due_date_was": "2026-02-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert kept.status_code == 200
        db_session.refresh(bill)
        assert bill.payment_method_id == method.id
        cleared = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "name": "Rent",
                "payment_method_id": "",
                "next_due_date": "2026-02-01",
                "next_due_date_was": "2026-02-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert cleared.status_code == 200
        db_session.refresh(bill)
        assert bill.payment_method_id is None

    def test_picker_filter_and_api_clear(self, authed_client, db_session, sample_bills):
        from app.services.payment_methods import start

        current = self._method(
            db_session,
            nickname="Current",
            kind="manual",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        outgoing = self._method(
            db_session,
            nickname="Outgoing",
            kind="manual",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        incoming = self._method(
            db_session,
            nickname="Incoming",
            kind="manual",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        visible = self._method(
            db_session,
            nickname="Visible",
            kind="manual",
            last4=None,
            expiry_month=None,
            expiry_year=None,
        )
        current.is_archived = True
        sample_bills[0].payment_method_id = current.id
        sample_bills[1].payment_method_id = outgoing.id
        db_session.commit()
        start(db_session, outgoing.id, new_id=incoming.id)
        db_session.commit()
        edit = authed_client.get(f"/bills/{sample_bills[0].id}/edit")
        assert "Current" in edit.text
        assert "Visible" in edit.text
        assert "Outgoing" not in edit.text

        filtered = authed_client.get("/bills?payment_method=none")
        assert "No payment method: 0" in filtered.text
        assert ">Rent<" not in filtered.text
        all_page = authed_client.get("/bills")
        assert "No payment method: 0" in all_page.text

        set_method = authed_client.put(
            f"/api/bills/{sample_bills[0].id}",
            json={"payment_method_id": visible.id},
        )
        assert set_method.status_code == 200
        assert set_method.json()["payment_method_id"] == visible.id
        cleared = authed_client.put(
            f"/api/bills/{sample_bills[0].id}",
            json={"payment_method_id": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["payment_method_id"] is None


def test_amount_help_summary_and_success_event(
    authed_client, db_session, sample_category
):
    page = authed_client.get("/bills")
    assert "Amount each time it's due." in page.text
    assert "Bills fund monthly cost" in page.text
    assert "Other recurring:" in page.text
    assert "Paid from" in page.text
    created = authed_client.post(
        "/bills",
        data={
            "name": "Power",
            "debtor_provider": "Grid",
            "amount": "40",
            "frequency": "monthly",
            "start_date": "2026-01-01",
            "next_due_date": "2026-06-01",
            "paid_from": "bills_fund",
            "bill_type": "fixed",
        },
        headers={"x-csrftoken": authed_client.csrf_token},
    )
    assert created.status_code == 200
    trigger = created.headers["hx-trigger-after-swap"]
    assert "Bill added." in trigger
    assert "monthly" in trigger
    assert "annual" in trigger
    assert "other" in trigger
    rejected = authed_client.post(
        "/bills",
        data={
            "name": "Nope",
            "debtor_provider": "Nope",
            "amount": "10",
            "frequency": "whenever",
            "start_date": "2026-01-01",
            "next_due_date": "2026-06-01",
            "paid_from": "bills_fund",
        },
        headers={"x-csrftoken": authed_client.csrf_token},
    )
    assert "Choose a frequency." in rejected.text


def _csrf(client):
    return {"x-csrftoken": client.csrf_token}


def _edit_payload(bill, **overrides):
    data = {
        "name": bill.name,
        "debtor_provider": bill.debtor_provider,
        "amount": f"{float(bill.amount):.2f}",
        "frequency": bill.frequency,
        "next_due_date": bill.next_due_date,
        "next_due_date_was": bill.next_due_date,
    }
    data.update(overrides)
    return data


def _bare_bill(**overrides):
    values = {
        "name": "Power",
        "amount": 10,
        "debtor_provider": "Grid",
        "start_date": "2026-01-01",
        "frequency": "monthly",
        "category_id": 1,
        "next_due_date": "2026-06-01",
        "paid_from": "bills_fund",
    }
    values.update(overrides)
    return RecurringBill(**values)


def test_new_and_existing_bills_start_unmarked(
    authed_client, db_session, sample_bills, sample_category
):
    for bill in sample_bills:
        assert bill.importance is None
        assert bill.cancel_note is None
        assert bill.if_stopped_note is None
    created = authed_client.post(
        "/api/bills",
        json={
            "name": "Gas",
            "amount": "20",
            "debtor_provider": "Gas Corp",
            "start_date": "2026-01-01",
            "frequency": "monthly",
            "category_id": sample_category.id,
            "next_due_date": "2026-02-01",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["importance"] is None
    assert body["cancel_note"] is None
    assert body["if_stopped_note"] is None
    marked = authed_client.post(
        "/api/bills",
        json={
            "name": "Mortgage",
            "amount": "20",
            "debtor_provider": "Bank",
            "start_date": "2026-01-01",
            "frequency": "monthly",
            "category_id": sample_category.id,
            "next_due_date": "2026-02-01",
            "importance": "essential",
        },
    )
    assert marked.status_code == 201
    listed = authed_client.get("/api/bills?importance=unset")
    assert listed.status_code == 200
    names = {row["name"] for row in listed.json()}
    assert names >= {"Rent", "Internet", "Gas", "Mortgage"}


def test_html_and_json_set_and_clear_the_mark(authed_client, db_session, sample_bills):
    bill = sample_bills[0]
    saved = authed_client.post(
        f"/bills/{bill.id}",
        data=_edit_payload(
            bill,
            importance="essential",
            cancel_note="  Call 13 13 19  ",
            if_stopped_note="The lights go out",
        ),
        headers=_csrf(authed_client),
    )
    assert saved.status_code == 200
    db_session.refresh(bill)
    assert bill.importance == "essential"
    assert bill.cancel_note == "Call 13 13 19"
    assert bill.if_stopped_note == "The lights go out"
    assert "Keep paying" in saved.text
    assert f'id="bill-notes-{bill.id}" hx-swap-oob="delete"' in saved.text

    omitted = authed_client.put(f"/api/bills/{bill.id}", json={"name": "Rent plus"})
    assert omitted.status_code == 200
    db_session.refresh(bill)
    assert bill.name == "Rent plus"
    assert bill.importance == "essential"
    assert bill.cancel_note == "Call 13 13 19"

    cleared = authed_client.put(
        f"/api/bills/{bill.id}",
        json={"importance": None, "cancel_note": None, "if_stopped_note": ""},
    )
    assert cleared.status_code == 200
    db_session.refresh(bill)
    assert bill.importance is None
    assert bill.cancel_note is None
    assert bill.if_stopped_note is None

    blank = authed_client.post(
        f"/bills/{bill.id}",
        data=_edit_payload(
            bill,
            importance="",
            cancel_note="   ",
            if_stopped_note="",
        ),
        headers=_csrf(authed_client),
    )
    assert blank.status_code == 200
    db_session.refresh(bill)
    assert bill.importance is None
    assert bill.cancel_note is None


def test_notes_reject_a_card_number_and_keep_a_phone(
    authed_client, db_session, sample_bills, sample_category
):
    bill = sample_bills[0]
    phone = authed_client.post(
        f"/bills/{bill.id}",
        data=_edit_payload(bill, cancel_note="Call 13 13 19"),
        headers=_csrf(authed_client),
    )
    assert phone.status_code == 200
    db_session.refresh(bill)
    assert bill.cancel_note == "Call 13 13 19"

    account = authed_client.put(
        f"/api/bills/{bill.id}",
        json={"if_stopped_note": "BSB 062-000 account 12345678"},
    )
    assert account.status_code == 200
    db_session.refresh(bill)
    assert bill.if_stopped_note == "BSB 062-000 account 12345678"

    short = authed_client.put(
        f"/api/bills/{bill.id}",
        json={"cancel_note": "12345678901"},
    )
    assert short.status_code == 200

    for note in (
        "1111 2222 3333",
        "1111-2222-3333",
        "1111.2222.3333",
        "1111/2222/3333",
        "111122223333",
    ):
        rejected = authed_client.post(
            f"/bills/{bill.id}",
            data=_edit_payload(bill, cancel_note=note),
            headers=_csrf(authed_client),
        )
        assert rejected.status_code == 422
        assert "Don't put a card number here." in rejected.text
        assert "1111" not in rejected.text
        assert "2222" not in rejected.text
        db_session.refresh(bill)
        assert bill.cancel_note == "12345678901"

        api = authed_client.put(
            f"/api/bills/{bill.id}",
            json={"if_stopped_note": note},
        )
        assert api.status_code == 422
        assert "Don't put a card number here." in api.text
        assert "1111" not in api.text
        assert "input" not in api.text
        db_session.refresh(bill)
        assert bill.if_stopped_note == "BSB 062-000 account 12345678"

    created = authed_client.post(
        "/api/bills",
        json={
            "name": "Card",
            "amount": "10",
            "debtor_provider": "Bank",
            "start_date": "2026-01-01",
            "frequency": "monthly",
            "category_id": sample_category.id,
            "next_due_date": "2026-06-01",
            "cancel_note": "1111/2222/3333",
        },
    )
    assert created.status_code == 422
    assert "1111" not in created.text
    assert (
        db_session.query(RecurringBill).filter(RecurringBill.name == "Card").first()
        is None
    )


def test_monthly_piles_sum_then_quantize_once():
    from app.routes.bills import _summary
    from app.schemas import UnknownFrequencyError

    summary = _summary(
        [
            _bare_bill(
                name="Week A", amount=1, frequency="weekly", importance="essential"
            ),
            _bare_bill(
                name="Week B",
                amount=1,
                frequency="weekly",
                importance="essential",
                paid_from="budget",
            ),
            _bare_bill(
                name="Year",
                amount=120,
                frequency="yearly",
                importance="optional",
                paid_from="sinking_fund",
                paid_from_fund_id=9,
            ),
            _bare_bill(
                name="Plain",
                amount=10,
                importance=None,
                foreign_amount=999,
                foreign_currency="USD",
            ),
        ]
    )
    assert summary["essential_monthly"] == Decimal("8.69")
    assert summary["optional_monthly"] == Decimal("10.00")
    assert summary["unmarked_monthly"] == Decimal("10.00")
    assert summary["unmarked_count"] == 1
    with pytest.raises(UnknownFrequencyError):
        _summary([_bare_bill(frequency="nope")])


def test_page_splits_averages_and_filters(authed_client, db_session, sample_category):
    from app.models import PaymentMethod

    method = PaymentMethod(nickname="Visa", kind="card", last4="4242", owner="Jordan")
    db_session.add(method)
    db_session.flush()
    loose = _bare_bill(name="Loose", amount=12, category_id=sample_category.id)
    carded = _bare_bill(
        name="Carded",
        amount=12,
        category_id=sample_category.id,
        payment_method_id=method.id,
    )
    kept = _bare_bill(
        name="Kept",
        amount=12,
        category_id=sample_category.id,
        importance="essential",
        if_stopped_note="Mortgage stops",
    )
    optional = _bare_bill(
        name="Stream",
        amount=12,
        category_id=sample_category.id,
        importance="optional",
        cancel_note="Cancel in the app",
        if_stopped_note="Hidden on the list",
    )
    drafted = _bare_bill(
        name="Draft",
        amount=12,
        category_id=sample_category.id,
        cancel_note="Draft cancel",
        if_stopped_note="Draft stop",
    )
    inactive = _bare_bill(
        name="Gone",
        amount=10000,
        category_id=sample_category.id,
        importance="essential",
        is_active=False,
    )
    db_session.add_all([loose, carded, kept, optional, drafted, inactive])
    db_session.commit()

    page = authed_client.get("/bills")
    assert page.status_code == 200
    assert "Split of the monthly averages" in page.text
    assert (
        "Safe to cancel is still paid until someone cancels it with the provider."
        in page.text
    )
    assert "No keep-or-cancel mark:" in page.text
    assert 'id="bills-essential-cost">12.00' in page.text
    assert 'id="bills-optional-cost">12.00' in page.text
    assert "10,012.00" not in page.text
    assert "gw-badge--paid" in page.text
    assert "gw-badge--quiet" in page.text
    assert "If this stops: Mortgage stops" in page.text
    assert "How to cancel: Cancel in the app" in page.text
    assert "Hidden on the list" not in page.text
    assert "How to cancel: Draft cancel" in page.text
    assert "If this stops: Draft stop" in page.text
    assert 'id="bills-unmarked-note"' in page.text
    assert "Mark each payment as keep paying or safe to cancel." in page.text

    unset = authed_client.get("/bills?importance=unset")
    assert "Loose" in unset.text
    assert "Carded" in unset.text
    assert "Draft" in unset.text
    assert "Kept" not in unset.text
    assert "Stream" not in unset.text
    assert 'id="bills-essential-cost">12.00' in unset.text
    assert "Show all" in unset.text

    both = authed_client.get("/bills?payment_method=none&importance=unset")
    assert "Loose" in both.text
    assert "Draft" in both.text
    assert "Carded" not in both.text
    assert "Show all" in both.text
    assert 'id="bills-unmarked-cost">' in both.text

    unknown = authed_client.get("/bills?importance=nope")
    assert "Kept" in unknown.text
    assert "Loose" in unknown.text
    assert "Show all" in unknown.text

    edit = authed_client.get(f"/bills/{loose.id}/edit?importance=unset")
    assert 'colspan="9"' in edit.text
    assert "If something happens to me" in edit.text
    assert f'form="edit-form-{loose.id}"' in edit.text
    assert 'name="cancel_note"' in edit.text
    assert 'name="if_stopped_note"' in edit.text
    assert "Both notes are optional on every payment" in edit.text
    assert f"/bills/{loose.id}?importance=unset" in edit.text
    assert "close_edit=1" in edit.text

    marked = authed_client.post(
        f"/bills/{loose.id}?importance=unset",
        data=_edit_payload(
            loose, importance="essential", cancel_note="", if_stopped_note=""
        ),
        headers=_csrf(authed_client),
    )
    assert marked.headers["hx-reswap"] == "delete"
    assert "Loose" not in marked.text
    trigger = json.loads(marked.headers["hx-trigger-after-swap"])["gwBillSuccess"]
    assert trigger["essential"] == "24.00"
    assert "unmarked_count" in trigger
    assert "optional" in trigger
    assert "unmarked" in trigger

    stayed = authed_client.post(
        f"/bills/{kept.id}?importance=essential",
        data=_edit_payload(
            kept,
            importance="essential",
            if_stopped_note="Mortgage stops",
            cancel_note="Only a note",
        ),
        headers=_csrf(authed_client),
    )
    assert "hx-reswap" not in stayed.headers
    assert "Kept" in stayed.text
    assert "Keep paying" in stayed.text
    assert "hx-trigger-after-swap" not in stayed.headers


def test_safe_to_cancel_stays_in_the_deposit(db_session, sample_category):
    from decimal import Decimal

    from sqlalchemy.exc import IntegrityError

    from app.services.recurring_payments import bills_fund_bills
    from app.tasks import _compute_bills_recommended

    keep = _bare_bill(
        name="Keep", amount=30, category_id=sample_category.id, importance="essential"
    )
    cancel = _bare_bill(
        name="Cancel", amount=90, category_id=sample_category.id, importance="optional"
    )
    budget = _bare_bill(
        name="Budget",
        amount=999,
        category_id=sample_category.id,
        importance="optional",
        paid_from="budget",
    )
    db_session.add_all([keep, cancel, budget])
    db_session.commit()
    ids = {row.id for row in bills_fund_bills(db_session)}
    assert keep.id in ids
    assert cancel.id in ids
    assert budget.id not in ids
    assert _compute_bills_recommended(db_session) == Decimal("120.00")

    cancel.importance = "later"
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_create_and_delete_send_the_mark_totals(
    authed_client, db_session, sample_category
):
    created = authed_client.post(
        "/bills",
        data={
            "name": "Power",
            "debtor_provider": "Grid",
            "amount": "12",
            "frequency": "monthly",
            "start_date": "2026-01-01",
            "next_due_date": "2026-06-01",
            "paid_from": "bills_fund",
            "importance": "optional",
            "cancel_note": "Call 13 13 19",
            "if_stopped_note": "",
        },
        headers=_csrf(authed_client),
    )
    assert created.status_code == 200
    added = json.loads(created.headers["hx-trigger-after-swap"])["gwBillSuccess"]
    assert added["optional"] == "12.00"
    assert added["unmarked_count"] == 0
    bill = db_session.query(RecurringBill).filter(RecurringBill.name == "Power").one()
    assert bill.importance == "optional"
    assert bill.cancel_note == "Call 13 13 19"
    assert bill.if_stopped_note is None
    deleted = authed_client.delete(f"/bills/{bill.id}", headers=_csrf(authed_client))
    removed = json.loads(deleted.headers["hx-trigger-after-swap"])["gwBillSuccess"]
    assert removed["optional"] == "0.00"
    assert removed["unmarked_count"] == 0
    page = authed_client.get("/bills")
    assert (
        'id="bills-unmarked-note" class="gw-text-muted" style="margin: 0 0 12px;" hidden'
        in page.text
    )
