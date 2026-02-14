from app.models import Transaction


class TestTransactionsPageGet:
    def test_renders_page_with_table(self, authed_client):
        response = authed_client.get("/transactions")
        assert response.status_code == 200
        assert "Transactions" in response.text
        assert "Date" in response.text
        assert "Amount" in response.text
        assert "Category" in response.text
        assert "Status" in response.text

    def test_lists_transactions_for_month(self, authed_client, sample_transactions):
        response = authed_client.get("/transactions?month=1&year=2026")
        assert response.status_code == 200
        assert "Groceries shopping" in response.text
        assert "Monthly income" in response.text

    def test_month_navigation_links(self, authed_client):
        response = authed_client.get("/transactions?month=6&year=2026")
        assert response.status_code == 200
        assert "June 2026" in response.text
        assert "month=5" in response.text  # prev
        assert "month=7" in response.text  # next

    def test_month_year_wrapping(self, authed_client):
        response = authed_client.get("/transactions?month=1&year=2026")
        assert "month=12" in response.text
        assert "year=2025" in response.text

        response = authed_client.get("/transactions?month=12&year=2026")
        assert "month=1" in response.text
        assert "year=2027" in response.text

    def test_type_filter(self, authed_client, sample_transactions):
        response = authed_client.get("/transactions?month=1&year=2026&type_filter=income")
        assert response.status_code == 200
        assert "Monthly income" in response.text
        assert "Groceries shopping" not in response.text

    def test_category_filter(self, authed_client, sample_transactions, sample_category):
        response = authed_client.get(
            f"/transactions?month=1&year=2026&category_id={sample_category.id}"
        )
        assert response.status_code == 200
        assert "Groceries shopping" in response.text
        assert "Monthly income" not in response.text

    def test_summary_totals(self, authed_client, sample_transactions):
        response = authed_client.get("/transactions?month=1&year=2026")
        assert response.status_code == 200
        assert "5,000.00" in response.text  # income
        assert "75.50" in response.text    # expense
        assert "4,924.50" in response.text  # net

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/transactions", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestTransactionsPagePost:
    def test_creates_transaction(self, authed_client, db_session, sample_category):
        response = authed_client.post(
            "/transactions",
            data={
                "date": "2026-01-20",
                "description": "Test purchase",
                "amount": "42.99",
                "category_id": str(sample_category.id),
                "type": "expense",
                "transaction_type": "regular",
                "is_paid": "on",
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        txn = db_session.query(Transaction).filter(
            Transaction.description == "Test purchase"
        ).first()
        assert txn is not None
        assert float(txn.amount) == 42.99

    def test_error_on_missing_date(self, authed_client, sample_category):
        response = authed_client.post(
            "/transactions",
            data={
                "date": "",
                "amount": "10.00",
                "category_id": str(sample_category.id),
                "type": "expense",
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_error_on_missing_category(self, authed_client):
        response = authed_client.post(
            "/transactions",
            data={
                "date": "2026-01-20",
                "amount": "10.00",
                "category_id": "",
                "type": "expense",
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    def test_error_on_invalid_amount(self, authed_client, sample_category):
        response = authed_client.post(
            "/transactions",
            data={
                "date": "2026-01-20",
                "amount": "abc",
                "category_id": str(sample_category.id),
                "type": "expense",
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Invalid" in response.text

    def test_error_on_zero_amount(self, authed_client, sample_category):
        response = authed_client.post(
            "/transactions",
            data={
                "date": "2026-01-20",
                "amount": "0",
                "category_id": str(sample_category.id),
                "type": "expense",
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "greater than zero" in response.text.lower()

    def test_error_on_invalid_type(self, authed_client, sample_category):
        response = authed_client.post(
            "/transactions",
            data={
                "date": "2026-01-20",
                "amount": "10.00",
                "category_id": str(sample_category.id),
                "type": "invalid",
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "income or expense" in response.text.lower()

    def test_creates_with_linked_entities(
        self, authed_client, db_session, sample_category, sample_sinking_funds, sample_bills
    ):
        response = authed_client.post(
            "/transactions",
            data={
                "date": "2026-01-25",
                "description": "Bill payment",
                "amount": "100.00",
                "category_id": str(sample_category.id),
                "type": "expense",
                "transaction_type": "regular",
                "sinking_fund_id": str(sample_sinking_funds[0].id),
                "recurring_bill_id": str(sample_bills[0].id),
                "is_paid": "on",
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        txn = db_session.query(Transaction).filter(
            Transaction.description == "Bill payment"
        ).first()
        assert txn is not None
        assert txn.sinking_fund_id == sample_sinking_funds[0].id
        assert txn.recurring_bill_id == sample_bills[0].id

    def test_403_without_csrf(self, authed_client, sample_category):
        response = authed_client.post(
            "/transactions",
            data={
                "date": "2026-01-20",
                "amount": "10.00",
                "category_id": str(sample_category.id),
                "type": "expense",
                "month": "1",
                "year": "2026",
            },
        )
        assert response.status_code == 403


class TestTransactionsEditGet:
    def test_returns_edit_form_row(self, authed_client, sample_transactions):
        txn = sample_transactions[0]
        response = authed_client.get(f"/transactions/{txn.id}/edit")
        assert response.status_code == 200
        assert 'name="amount"' in response.text
        assert 'name="date"' in response.text

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.get("/transactions/99999/edit")
        assert response.status_code == 404


class TestTransactionsEditPost:
    def test_updates_transaction(self, authed_client, db_session, sample_transactions):
        txn = sample_transactions[0]
        response = authed_client.post(
            f"/transactions/{txn.id}",
            data={"amount": "99.99", "description": "Updated desc"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(txn)
        assert float(txn.amount) == 99.99
        assert txn.description == "Updated desc"

    def test_returns_updated_row(self, authed_client, sample_transactions):
        txn = sample_transactions[0]
        response = authed_client.post(
            f"/transactions/{txn.id}",
            data={"amount": "99.99"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert "99.99" in response.text

    def test_clears_linked_entity(
        self, authed_client, db_session, sample_transactions, sample_sinking_funds
    ):
        txn = sample_transactions[0]
        txn.sinking_fund_id = sample_sinking_funds[0].id
        db_session.commit()

        response = authed_client.post(
            f"/transactions/{txn.id}",
            data={"sinking_fund_id": ""},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(txn)
        assert txn.sinking_fund_id is None

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.post(
            "/transactions/99999",
            data={"amount": "10.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestTransactionsDelete:
    def test_hard_deletes_transaction(self, authed_client, db_session, sample_transactions):
        txn = sample_transactions[0]
        txn_id = txn.id
        response = authed_client.delete(
            f"/transactions/{txn_id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.expire_all()
        assert db_session.query(Transaction).filter(Transaction.id == txn_id).first() is None

    def test_returns_empty_response(self, authed_client, sample_transactions):
        txn = sample_transactions[0]
        response = authed_client.delete(
            f"/transactions/{txn.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.text == ""

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.delete(
            "/transactions/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_403_without_csrf(self, authed_client, sample_transactions):
        txn = sample_transactions[0]
        response = authed_client.delete(f"/transactions/{txn.id}")
        assert response.status_code == 403


class TestApiTransactionsList:
    def test_returns_json_list(self, authed_client, sample_transactions):
        response = authed_client.get("/api/transactions?month=1&year=2026")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_defaults_to_current_month(self, authed_client):
        response = authed_client.get("/api/transactions")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_filter_by_type(self, authed_client, sample_transactions):
        response = authed_client.get("/api/transactions?month=1&year=2026&type_filter=expense")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["type"] == "expense"

    def test_unauthenticated_redirects(self, client):
        response = client.get("/api/transactions", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestApiTransactionsCreate:
    def test_creates_and_returns_201(self, authed_client, db_session, sample_category):
        response = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-01-20",
                "description": "API transaction",
                "amount": "55.00",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "regular",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert float(data["amount"]) == 55.0
        assert data["description"] == "API transaction"

    def test_422_on_validation_error(self, authed_client):
        response = authed_client.post(
            "/api/transactions",
            json={"date": "bad-date"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422

    def test_api_csrf_exempt(self, authed_client, sample_category):
        """API routes are CSRF-exempt (they use Bearer token auth instead)."""
        response = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-01-20",
                "amount": "10.00",
                "category_id": sample_category.id,
                "type": "expense",
            },
        )
        assert response.status_code == 201


class TestApiTransactionsGet:
    def test_returns_single_transaction(self, authed_client, sample_transactions):
        txn = sample_transactions[0]
        response = authed_client.get(f"/api/transactions/{txn.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == txn.id
        assert float(data["amount"]) == 75.5

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.get("/api/transactions/99999")
        assert response.status_code == 404


class TestApiTransactionsUpdate:
    def test_updates_and_returns_200(self, authed_client, db_session, sample_transactions):
        txn = sample_transactions[0]
        response = authed_client.put(
            f"/api/transactions/{txn.id}",
            json={"amount": "120.00", "description": "Updated via API"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert float(data["amount"]) == 120.0
        assert data["description"] == "Updated via API"

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.put(
            "/api/transactions/99999",
            json={"amount": "10.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404

    def test_422_on_validation_error(self, authed_client, sample_transactions):
        txn = sample_transactions[0]
        response = authed_client.put(
            f"/api/transactions/{txn.id}",
            json={"amount": "-10"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 422


class TestApiTransactionsDelete:
    def test_hard_deletes_and_returns_200(self, authed_client, db_session, sample_transactions):
        txn = sample_transactions[0]
        txn_id = txn.id
        response = authed_client.delete(
            f"/api/transactions/{txn_id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.expire_all()
        assert db_session.query(Transaction).filter(Transaction.id == txn_id).first() is None

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.delete(
            "/api/transactions/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404
