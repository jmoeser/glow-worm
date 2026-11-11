from decimal import Decimal

from app.models import Budget, Category, Transaction
from app.routes.transactions import PAGE_SIZE


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
        response = authed_client.get(
            "/transactions?month=1&year=2026&type_filter=income"
        )
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
        assert "75.50" in response.text  # expense
        assert "4,924.50" in response.text  # net

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/transactions", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_pagination_shows_only_page_size_rows(
        self, authed_client, db_session, sample_category
    ):
        """When there are more transactions than PAGE_SIZE, only one page is shown."""
        txns = [
            Transaction(
                date="2026-03-01",
                description=f"Txn {i}",
                amount=1.00,
                category_id=sample_category.id,
                type="expense",
                transaction_type="regular",
            )
            for i in range(PAGE_SIZE + 5)
        ]
        db_session.add_all(txns)
        db_session.commit()

        response = authed_client.get("/transactions?month=3&year=2026&page=1")
        assert response.status_code == 200
        assert "Page 1 of 2" in response.text
        assert f"{PAGE_SIZE + 5} transactions" in response.text

    def test_pagination_page2_shows_remaining_rows(
        self, authed_client, db_session, sample_category
    ):
        txns = [
            Transaction(
                date="2026-03-01",
                description=f"Txn {i:03d}",
                amount=1.00,
                category_id=sample_category.id,
                type="expense",
                transaction_type="regular",
            )
            for i in range(PAGE_SIZE + 3)
        ]
        db_session.add_all(txns)
        db_session.commit()

        response = authed_client.get("/transactions?month=3&year=2026&page=2")
        assert response.status_code == 200
        assert "Page 2 of 2" in response.text

    def test_summary_totals_span_all_pages(
        self, authed_client, db_session, sample_category
    ):
        """Summary totals should reflect the full month, not just the current page."""
        txns = [
            Transaction(
                date="2026-03-01",
                description=f"Txn {i}",
                amount=10.00,
                category_id=sample_category.id,
                type="expense",
                transaction_type="regular",
            )
            for i in range(PAGE_SIZE + 1)
        ]
        db_session.add_all(txns)
        db_session.commit()

        total = (PAGE_SIZE + 1) * 10
        response = authed_client.get("/transactions?month=3&year=2026&page=1")
        assert response.status_code == 200
        assert f"{total:,.2f}" in response.text or f"{total:.2f}" in response.text


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
        txn = (
            db_session.query(Transaction)
            .filter(Transaction.description == "Test purchase")
            .first()
        )
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
        self,
        authed_client,
        db_session,
        sample_category,
        sample_sinking_funds,
        sample_bills,
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
        txn = (
            db_session.query(Transaction)
            .filter(Transaction.description == "Bill payment")
            .first()
        )
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

    def test_edit_form_includes_secondary_income_allocation(
        self, authed_client, db_session, sample_income_category
    ):
        txn = Transaction(
            date="2026-01-10",
            description="Goal fund contribution",
            amount=200.00,
            category_id=sample_income_category.id,
            type="income",
            transaction_type="secondary_income_allocation",
        )
        db_session.add(txn)
        db_session.commit()
        db_session.refresh(txn)

        response = authed_client.get(f"/transactions/{txn.id}/edit")
        assert response.status_code == 200
        assert 'value="secondary_income_allocation"' in response.text
        assert "Secondary Income Allocation" in response.text
        assert 'value="secondary_income_allocation" selected' in response.text.replace(
            "\n", " "
        )

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

    def test_updates_secondary_income_allocation_type(
        self, authed_client, db_session, sample_transactions
    ):
        txn = sample_transactions[0]
        response = authed_client.post(
            f"/transactions/{txn.id}",
            data={
                "transaction_type": "secondary_income_allocation",
                "type": "income",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.refresh(txn)
        assert txn.transaction_type == "secondary_income_allocation"

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
    def test_hard_deletes_transaction(
        self, authed_client, db_session, sample_transactions
    ):
        txn = sample_transactions[0]
        txn_id = txn.id
        response = authed_client.delete(
            f"/transactions/{txn_id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.expire_all()
        assert (
            db_session.query(Transaction).filter(Transaction.id == txn_id).first()
            is None
        )

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
        response = authed_client.get(
            "/api/transactions?month=1&year=2026&type_filter=expense"
        )
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
    def test_updates_and_returns_200(
        self, authed_client, db_session, sample_transactions
    ):
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
    def test_hard_deletes_and_returns_200(
        self, authed_client, db_session, sample_transactions
    ):
        txn = sample_transactions[0]
        txn_id = txn.id
        response = authed_client.delete(
            f"/api/transactions/{txn_id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        db_session.expire_all()
        assert (
            db_session.query(Transaction).filter(Transaction.id == txn_id).first()
            is None
        )

    def test_404_for_nonexistent(self, authed_client):
        response = authed_client.delete(
            "/api/transactions/99999",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 404


class TestBudgetTransferFundBalance:
    """budget_transfer transactions must update Budget.fund_balance."""

    def _make_budget(self, db_session, category, amount=500):
        budget = Budget(
            category_id=category.id,
            month=3,
            year=2026,
            allocated_amount=amount,
            spent_amount=amount + 50,  # overspent
            fund_balance=0,
        )
        db_session.add(budget)
        db_session.commit()
        db_session.refresh(budget)
        return budget

    def test_api_create_budget_transfer_increases_fund_balance(
        self, authed_client, db_session, sample_category, sample_sinking_funds
    ):
        budget = self._make_budget(db_session, sample_category)
        fund = sample_sinking_funds[1]  # Savings

        response = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-03-15",
                "description": "Cover overspend",
                "amount": "100.00",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "budget_transfer",
                "sinking_fund_id": fund.id,
                "budget_id": budget.id,
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201

        db_session.expire_all()
        db_session.refresh(budget)
        assert Decimal(str(budget.fund_balance)) == Decimal("100.00")

    def test_api_delete_budget_transfer_decreases_fund_balance(
        self, authed_client, db_session, sample_category, sample_sinking_funds
    ):
        budget = self._make_budget(db_session, sample_category)
        fund = sample_sinking_funds[1]

        # Pre-set fund_balance as if a transfer was already applied
        budget.fund_balance = 100
        db_session.commit()

        txn = Transaction(
            date="2026-03-15",
            description="Cover overspend",
            amount=100,
            category_id=sample_category.id,
            type="expense",
            transaction_type="budget_transfer",
            sinking_fund_id=fund.id,
            budget_id=budget.id,
        )
        db_session.add(txn)
        db_session.commit()

        response = authed_client.delete(
            f"/api/transactions/{txn.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200

        db_session.expire_all()
        db_session.refresh(budget)
        assert Decimal(str(budget.fund_balance)) == Decimal("0.00")

    def test_non_budget_transfer_does_not_touch_fund_balance(
        self, authed_client, db_session, sample_category, sample_sinking_funds
    ):
        budget = self._make_budget(db_session, sample_category)
        fund = sample_sinking_funds[1]

        response = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-03-15",
                "amount": "50.00",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "budget_expense",
                "sinking_fund_id": fund.id,
                "budget_id": budget.id,
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 201

        db_session.expire_all()
        db_session.refresh(budget)
        assert Decimal(str(budget.fund_balance)) == Decimal("0.00")


class TestFundTransfer:
    def test_creates_two_transactions(
        self, authed_client, db_session, sample_sinking_funds, transfer_category
    ):
        from_fund, to_fund = sample_sinking_funds
        response = authed_client.post(
            "/transactions/fund-transfer",
            data={
                "date": "2026-01-15",
                "description": "Move to savings",
                "amount": "200.00",
                "from_fund_id": str(from_fund.id),
                "to_fund_id": str(to_fund.id),
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        txns = (
            db_session.query(Transaction)
            .filter(Transaction.description == "Move to savings")
            .all()
        )
        assert len(txns) == 2

    def test_adjusts_both_fund_balances(
        self, authed_client, db_session, sample_sinking_funds, transfer_category
    ):
        from_fund, to_fund = sample_sinking_funds
        from_fund.current_balance = 500.0
        to_fund.current_balance = 100.0
        db_session.commit()

        authed_client.post(
            "/transactions/fund-transfer",
            data={
                "date": "2026-01-15",
                "amount": "200.00",
                "from_fund_id": str(from_fund.id),
                "to_fund_id": str(to_fund.id),
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )

        db_session.expire_all()
        db_session.refresh(from_fund)
        db_session.refresh(to_fund)
        assert Decimal(str(from_fund.current_balance)) == Decimal("300.00")
        assert Decimal(str(to_fund.current_balance)) == Decimal("300.00")

    def test_withdrawal_and_transfer_types(
        self, authed_client, db_session, sample_sinking_funds, transfer_category
    ):
        from_fund, to_fund = sample_sinking_funds
        authed_client.post(
            "/transactions/fund-transfer",
            data={
                "date": "2026-01-15",
                "amount": "50.00",
                "from_fund_id": str(from_fund.id),
                "to_fund_id": str(to_fund.id),
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        txns = db_session.query(Transaction).all()
        types = {t.type for t in txns}
        assert "expense" in types
        assert "transfer" in types

    def test_error_on_same_fund(
        self, authed_client, sample_sinking_funds, transfer_category
    ):
        fund = sample_sinking_funds[0]
        response = authed_client.post(
            "/transactions/fund-transfer",
            data={
                "date": "2026-01-15",
                "amount": "50.00",
                "from_fund_id": str(fund.id),
                "to_fund_id": str(fund.id),
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "same fund" in response.text.lower()

    def test_error_on_missing_amount(
        self, authed_client, sample_sinking_funds, transfer_category
    ):
        from_fund, to_fund = sample_sinking_funds
        response = authed_client.post(
            "/transactions/fund-transfer",
            data={
                "date": "2026-01-15",
                "amount": "",
                "from_fund_id": str(from_fund.id),
                "to_fund_id": str(to_fund.id),
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "invalid" in response.text.lower() or "required" in response.text.lower()

    def test_403_without_csrf(self, authed_client, sample_sinking_funds):
        from_fund, to_fund = sample_sinking_funds
        response = authed_client.post(
            "/transactions/fund-transfer",
            data={
                "date": "2026-01-15",
                "amount": "50.00",
                "from_fund_id": str(from_fund.id),
                "to_fund_id": str(to_fund.id),
            },
        )
        assert response.status_code == 403

    def test_uses_transfer_category(
        self, authed_client, db_session, sample_sinking_funds, transfer_category
    ):
        from_fund, to_fund = sample_sinking_funds
        authed_client.post(
            "/transactions/fund-transfer",
            data={
                "date": "2026-01-15",
                "amount": "75.00",
                "from_fund_id": str(from_fund.id),
                "to_fund_id": str(to_fund.id),
                "month": "1",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        txns = db_session.query(Transaction).all()
        assert all(t.category_id == transfer_category.id for t in txns)


class TestRecordedPaymentLock:
    def _payment(self, db, sample_category, sample_sinking_funds, monkeypatch):
        from datetime import date

        from app.models import RecurringBill
        from app.services.recurring_payments import pay_bills_fund

        monkeypatch.setattr(
            "app.services.recurring_payments._today", lambda: date(2026, 11, 3)
        )

        fund = sample_sinking_funds[0]
        fund.current_balance = 200
        other = sample_sinking_funds[1]
        bill = RecurringBill(
            name="Power",
            amount=40,
            debtor_provider="Grid",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-11-03",
            paid_from="bills_fund",
            bill_type="fixed",
        )
        other_bill = RecurringBill(
            name="Water",
            amount=10,
            debtor_provider="City",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-12-01",
            paid_from="bills_fund",
        )
        groceries = Category(
            name="Groceries", type="expense", color="#22C55E", is_budget_category=True
        )
        db.add_all([bill, other_bill, groceries])
        db.flush()
        budget = Budget(
            category_id=groceries.id,
            month=11,
            year=2026,
            allocated_amount=100,
            spent_amount=0,
            fund_balance=0,
        )
        db.add(budget)
        db.commit()
        txn = pay_bills_fund(db, bill, Decimal(40), "2026-11-03").transaction
        db.commit()
        db.refresh(txn)
        return {
            "txn": txn,
            "bill": bill,
            "other_bill": other_bill,
            "fund": fund,
            "other_fund": other,
            "groceries": groceries,
            "budget": budget,
        }

    def _snap(self, txn):
        return (
            str(txn.amount),
            txn.type,
            txn.transaction_type,
            txn.category_id,
            txn.sinking_fund_id,
            txn.budget_id,
            txn.recurring_bill_id,
            txn.description,
            txn.date,
            txn.is_paid,
            txn.occurrence_due_date,
        )

    def test_json_html_and_mcp_refuse_money_fields(
        self,
        authed_client,
        db_session,
        sample_category,
        sample_sinking_funds,
        monkeypatch,
    ):
        from app.mcp_server import update_transaction

        built = self._payment(
            db_session, sample_category, sample_sinking_funds, monkeypatch
        )
        txn = built["txn"]
        changes = [
            {"amount": "1.00"},
            {"type": "income"},
            {"transaction_type": "withdrawal"},
            {"category_id": built["groceries"].id},
            {"sinking_fund_id": built["other_fund"].id},
            {"budget_id": built["budget"].id},
            {"recurring_bill_id": built["other_bill"].id},
        ]
        for payload in changes:
            before = self._snap(txn)
            body = {"description": "should not stick", **payload}
            response = authed_client.put(f"/api/transactions/{txn.id}", json=body)
            assert response.status_code == 409, payload
            db_session.refresh(txn)
            assert self._snap(txn) == before
            html = authed_client.post(
                f"/transactions/{txn.id}",
                data={
                    "description": "should not stick",
                    "date": "2026-11-04",
                    **{key: str(value) for key, value in payload.items()},
                },
                headers={"x-csrftoken": authed_client.csrf_token},
            )
            assert html.status_code == 409, payload
            db_session.refresh(txn)
            assert self._snap(txn) == before
            mcp_kwargs = {}
            for key, value in payload.items():
                mcp_kwargs[key] = float(value) if key == "amount" else value
            mcp = update_transaction(txn.id, **mcp_kwargs)
            assert isinstance(mcp, str)
            assert "can't change" in mcp
            db_session.refresh(txn)
            assert self._snap(txn) == before

    def test_description_and_date_still_save(
        self,
        authed_client,
        db_session,
        sample_category,
        sample_sinking_funds,
        monkeypatch,
    ):
        from app.mcp_server import update_transaction

        built = self._payment(
            db_session, sample_category, sample_sinking_funds, monkeypatch
        )
        txn = built["txn"]
        occurrence = txn.occurrence_due_date
        saved = authed_client.put(
            f"/api/transactions/{txn.id}",
            json={"description": "Noted", "date": "2026-11-04", "is_paid": False},
        )
        assert saved.status_code == 200
        db_session.refresh(txn)
        assert txn.description == "Noted"
        assert txn.date == "2026-11-04"
        assert txn.is_paid is False
        assert txn.occurrence_due_date == occurrence
        html = authed_client.post(
            f"/transactions/{txn.id}",
            data={"description": "From the form", "date": "2026-11-05"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert html.status_code == 200
        db_session.refresh(txn)
        assert txn.description == "From the form"
        assert txn.date == "2026-11-05"
        assert txn.occurrence_due_date == occurrence
        mcp = update_transaction(txn.id, description="From MCP", date="2026-11-06")
        assert isinstance(mcp, dict)
        db_session.refresh(txn)
        assert txn.description == "From MCP"
        assert txn.occurrence_due_date == occurrence
        edit = authed_client.get(f"/transactions/{txn.id}/edit")
        assert 'name="amount"' not in edit.text
        assert "Choosing a bill here" not in edit.text

    def test_clawback_money_fields_are_locked(
        self, authed_client, db_session, sample_category, monkeypatch
    ):
        from datetime import date

        from app.models import (
            HouseholdMonth,
            IncomeAllocation,
            RecurringBill,
            SinkingFund,
        )
        from app.services.recurring_payments import confirm_payment

        groceries = Category(
            name="Groceries", type="expense", color="#22C55E", is_budget_category=True
        )
        transfer = Category(
            name="Transfer", type="transfer", color="#888888", is_system=True
        )
        income = Category(name="Income", type="income", color="#00aa00", is_system=True)
        db_session.add_all([groceries, transfer, income])
        db_session.flush()
        overflow = SinkingFund(name="Overflow", color="#0000ff", current_balance=50)
        db_session.add(overflow)
        db_session.flush()
        budget = Budget(
            category_id=groceries.id,
            month=10,
            year=2026,
            allocated_amount=100,
            spent_amount=0,
            fund_balance=0,
        )
        db_session.add(budget)
        db_session.add(
            IncomeAllocation(
                monthly_income_amount=1000,
                monthly_budget_allocation=100,
                bills_fund_allocation_type="fixed",
                bills_fund_fixed_amount=0,
                overflow_sinking_fund_id=overflow.id,
            )
        )
        db_session.add(
            HouseholdMonth(
                year=2026,
                month=10,
                settlement_outcome="surplus",
                settlement_fund_id=overflow.id,
                settlement_amount=10,
                settled_on="2026-11-03",
            )
        )
        bill = RecurringBill(
            name="HelloFresh",
            amount=20,
            debtor_provider="HelloFresh",
            start_date="2026-01-01",
            frequency="weekly",
            category_id=groceries.id,
            next_due_date="2026-10-26",
            paid_from="budget",
        )
        db_session.add(bill)
        db_session.commit()
        monkeypatch.setattr(
            "app.services.recurring_payments._today", lambda: date(2026, 11, 3)
        )
        result = confirm_payment(db_session, bill, Decimal(20), "2026-10-26")
        db_session.commit()
        claw = db_session.get(Transaction, result.taken_back["transaction_id"])
        before = (
            str(claw.amount),
            claw.type,
            claw.transaction_type,
            claw.category_id,
            claw.sinking_fund_id,
            claw.budget_id,
            claw.recurring_bill_id,
            claw.description,
        )
        refused = authed_client.put(
            f"/api/transactions/{claw.id}",
            json={"amount": "1.00", "description": "nope"},
        )
        assert refused.status_code == 409
        db_session.refresh(claw)
        assert (
            str(claw.amount),
            claw.type,
            claw.transaction_type,
            claw.category_id,
            claw.sinking_fund_id,
            claw.budget_id,
            claw.recurring_bill_id,
            claw.description,
        ) == before


class TestOneOffBillsFundExpense:
    def test_create_edit_and_delete_leave_the_schedule_alone(
        self,
        authed_client,
        db_session,
        sample_category,
        sample_sinking_funds,
        monkeypatch,
    ):
        from datetime import date

        from app.models import RecurringBill
        from app.tasks import process_due_bills

        bills, savings = sample_sinking_funds
        bills.current_balance = 500
        other = Category(
            name="Groceries", type="expense", color="#22C55E", is_budget_category=True
        )
        db_session.add(other)
        db_session.flush()
        budget = Budget(
            category_id=other.id,
            month=2,
            year=2026,
            allocated_amount=100,
            spent_amount=0,
            fund_balance=0,
        )
        due = RecurringBill(
            name="Power",
            amount=40,
            debtor_provider="Grid",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
            paid_from="bills_fund",
            bill_type="fixed",
        )
        parked = RecurringBill(
            name="Later",
            amount=10,
            debtor_provider="Later",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-06-01",
            paid_from="bills_fund",
        )
        db_session.add_all([budget, due, parked])
        db_session.commit()
        page = authed_client.get("/transactions")
        assert "one-off" in page.text
        assert "does not change any bill" in page.text

        api = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-02-01",
                "amount": "10.00",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "withdrawal",
                "sinking_fund_id": bills.id,
                "description": "One-off API",
            },
        )
        assert api.status_code == 201
        api_id = api.json()["id"]
        html = authed_client.post(
            "/transactions",
            data={
                "date": "2026-02-01",
                "description": "One-off form",
                "amount": "15.00",
                "category_id": str(sample_category.id),
                "type": "expense",
                "transaction_type": "withdrawal",
                "sinking_fund_id": str(bills.id),
                "month": "2",
                "year": "2026",
                "is_paid": "on",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert html.status_code == 200
        db_session.refresh(bills)
        assert Decimal(str(bills.current_balance)) == Decimal("475.00")
        rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id.is_(None))
            .all()
        )
        assert len(rows) == 2
        for row in rows:
            assert row.occurrence_due_date is None
            assert row.adjustment_for_transaction_id is None
        db_session.refresh(due)
        db_session.refresh(parked)
        assert due.next_due_date == "2026-02-01"
        assert parked.next_due_date == "2026-06-01"

        form_row = (
            db_session.query(Transaction)
            .filter(Transaction.description == "One-off form")
            .one()
        )
        edit = authed_client.get(f"/transactions/{form_row.id}/edit")
        assert 'name="amount"' in edit.text
        assert "Choosing a bill here does not record a scheduled payment" in edit.text

        steps = [
            {"amount": "12.00"},
            {"type": "income"},
            {"transaction_type": "contribution"},
            {"category_id": other.id},
            {"sinking_fund_id": savings.id},
            {"budget_id": budget.id},
            {"description": "Edited", "date": "2026-02-02"},
            {"is_paid": False},
            {"recurring_bill_id": due.id},
            {"recurring_bill_id": None},
        ]
        for payload in steps:
            response = authed_client.put(f"/api/transactions/{api_id}", json=payload)
            assert response.status_code == 200, payload
            db_session.refresh(db_session.get(Transaction, api_id))
            row = db_session.get(Transaction, api_id)
            assert row.occurrence_due_date is None
            assert row.adjustment_for_transaction_id is None
            db_session.refresh(due)
            db_session.refresh(parked)
            assert due.next_due_date == "2026-02-01"
            assert parked.next_due_date == "2026-06-01"

        day = date(2026, 2, 1)
        monkeypatch.setattr("app.tasks._today", lambda: day)
        monkeypatch.setattr("app.services.recurring_payments._today", lambda: day)
        process_due_bills(db=db_session)
        db_session.rollback()
        auto = (
            db_session.query(Transaction)
            .filter(Transaction.occurrence_due_date == "2026-02-01")
            .one()
        )
        assert auto.recurring_bill_id == due.id
        one_off = db_session.get(Transaction, api_id)
        assert one_off is not None
        assert one_off.occurrence_due_date is None
        before_ids = {row.id for row in db_session.query(Transaction).all()}
        deleted = authed_client.delete(f"/api/transactions/{api_id}")
        assert deleted.status_code == 200
        after_ids = {row.id for row in db_session.query(Transaction).all()}
        assert before_ids - after_ids == {api_id}
        assert auto.id in after_ids
        db_session.refresh(due)
        stepped = due.next_due_date
        assert stepped == "2026-03-01"
        db_session.refresh(due)
        assert due.next_due_date == stepped
