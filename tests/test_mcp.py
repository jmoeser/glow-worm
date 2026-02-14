"""Tests for MCP server tools.

These tests call the MCP tool functions directly (unit-style) since they
use the shared database session factory. The HTTP-level MCP transport is
tested implicitly by verifying the /mcp mount exists.
"""

import os

os.environ["DATABASE_URL"] = "sqlite:///./test-glow-worm.db"

from app.mcp_server import (
    create_bill,
    create_transaction,
    delete_bill,
    delete_transaction,
    get_bill,
    get_transaction,
    list_bills,
    list_transactions,
    update_bill,
    update_transaction,
)

# FastMCP wraps decorated functions into FunctionTool objects.
# Access the underlying callable via .fn for direct testing.
_list_transactions = list_transactions.fn
_get_transaction = get_transaction.fn
_create_transaction = create_transaction.fn
_update_transaction = update_transaction.fn
_delete_transaction = delete_transaction.fn
_list_bills = list_bills.fn
_get_bill = get_bill.fn
_create_bill = create_bill.fn
_update_bill = update_bill.fn
_delete_bill = delete_bill.fn


class TestTransactionTools:
    def test_list_transactions_empty(self, db_session, setup_database):
        result = _list_transactions(month=1, year=2026)
        assert result == []

    def test_create_and_get_transaction(self, db_session, setup_database, sample_category):
        result = _create_transaction(
            date="2026-01-15",
            amount=50.00,
            category_id=sample_category.id,
            type="expense",
            description="Test expense",
        )
        assert isinstance(result, dict)
        assert result["amount"] == "50.00"
        assert result["description"] == "Test expense"
        assert result["type"] == "expense"

        # get_transaction
        fetched = _get_transaction(result["id"])
        assert isinstance(fetched, dict)
        assert fetched["id"] == result["id"]

    def test_create_transaction_invalid_category(self, db_session, setup_database):
        result = _create_transaction(
            date="2026-01-15",
            amount=50.00,
            category_id=9999,
            type="expense",
        )
        assert isinstance(result, str)
        assert "not found" in result.lower()

    def test_list_transactions_with_filters(self, db_session, setup_database, sample_category, sample_income_category):
        _create_transaction(
            date="2026-01-10",
            amount=100.00,
            category_id=sample_category.id,
            type="expense",
        )
        _create_transaction(
            date="2026-01-10",
            amount=5000.00,
            category_id=sample_income_category.id,
            type="income",
            transaction_type="income",
        )

        all_txns = _list_transactions(month=1, year=2026)
        assert len(all_txns) == 2

        expenses = _list_transactions(month=1, year=2026, type_filter="expense")
        assert len(expenses) == 1
        assert expenses[0]["type"] == "expense"

        income = _list_transactions(month=1, year=2026, type_filter="income")
        assert len(income) == 1

    def test_update_transaction(self, db_session, setup_database, sample_category):
        created = _create_transaction(
            date="2026-01-15",
            amount=50.00,
            category_id=sample_category.id,
            type="expense",
        )

        updated = _update_transaction(
            transaction_id=created["id"],
            amount=75.00,
            description="Updated",
        )
        assert isinstance(updated, dict)
        assert updated["amount"] == "75.00"
        assert updated["description"] == "Updated"

    def test_update_nonexistent_transaction(self, db_session, setup_database):
        result = _update_transaction(transaction_id=9999, amount=10.00)
        assert isinstance(result, str)
        assert "not found" in result.lower()

    def test_delete_transaction(self, db_session, setup_database, sample_category):
        created = _create_transaction(
            date="2026-01-15",
            amount=50.00,
            category_id=sample_category.id,
            type="expense",
        )

        result = _delete_transaction(created["id"])
        assert "deleted" in result.lower()

        fetched = _get_transaction(created["id"])
        assert isinstance(fetched, str)
        assert "not found" in fetched.lower()

    def test_delete_nonexistent_transaction(self, db_session, setup_database):
        result = _delete_transaction(9999)
        assert "not found" in result.lower()

    def test_get_nonexistent_transaction(self, db_session, setup_database):
        result = _get_transaction(9999)
        assert isinstance(result, str)
        assert "not found" in result.lower()


class TestBillTools:
    def test_list_bills_empty(self, db_session, setup_database):
        result = _list_bills()
        assert result == []

    def test_create_and_get_bill(self, db_session, setup_database, sample_category):
        result = _create_bill(
            name="Netflix",
            amount=15.99,
            debtor_provider="Netflix Inc",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
        )
        assert isinstance(result, dict)
        assert result["name"] == "Netflix"
        assert result["frequency"] == "monthly"

        fetched = _get_bill(result["id"])
        assert isinstance(fetched, dict)
        assert fetched["id"] == result["id"]

    def test_create_bill_invalid_category(self, db_session, setup_database):
        result = _create_bill(
            name="Test",
            amount=10.00,
            debtor_provider="Test",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=9999,
            next_due_date="2026-02-01",
        )
        assert isinstance(result, str)
        assert "not found" in result.lower()

    def test_create_bill_invalid_frequency(self, db_session, setup_database, sample_category):
        result = _create_bill(
            name="Test",
            amount=10.00,
            debtor_provider="Test",
            start_date="2026-01-01",
            frequency="biweekly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
        )
        assert isinstance(result, str)
        assert "validation error" in result.lower()

    def test_list_bills_active_only(self, db_session, setup_database, sample_category):
        _create_bill(
            name="Active Bill",
            amount=50.00,
            debtor_provider="Provider A",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
        )
        inactive = _create_bill(
            name="Inactive Bill",
            amount=30.00,
            debtor_provider="Provider B",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
        )
        _delete_bill(inactive["id"])

        active = _list_bills(include_inactive=False)
        assert len(active) == 1
        assert active[0]["name"] == "Active Bill"

        all_bills = _list_bills(include_inactive=True)
        assert len(all_bills) == 2

    def test_update_bill(self, db_session, setup_database, sample_category):
        created = _create_bill(
            name="Old Name",
            amount=50.00,
            debtor_provider="Provider",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
        )

        updated = _update_bill(
            bill_id=created["id"],
            name="New Name",
            amount=60.00,
        )
        assert isinstance(updated, dict)
        assert updated["name"] == "New Name"
        assert updated["amount"] == "60.00"

    def test_update_nonexistent_bill(self, db_session, setup_database):
        result = _update_bill(bill_id=9999, name="Ghost")
        assert isinstance(result, str)
        assert "not found" in result.lower()

    def test_delete_bill(self, db_session, setup_database, sample_category):
        created = _create_bill(
            name="To Delete",
            amount=25.00,
            debtor_provider="Provider",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-02-01",
        )

        result = _delete_bill(created["id"])
        assert "deactivated" in result.lower()

        # Should still be fetchable but inactive
        fetched = _get_bill(created["id"])
        assert isinstance(fetched, dict)
        assert fetched["is_active"] is False

    def test_delete_nonexistent_bill(self, db_session, setup_database):
        result = _delete_bill(9999)
        assert "not found" in result.lower()

    def test_get_nonexistent_bill(self, db_session, setup_database):
        result = _get_bill(9999)
        assert isinstance(result, str)
        assert "not found" in result.lower()


class TestMcpMount:
    def test_mcp_endpoint_exists(self, client):
        """The /mcp endpoint should be mounted (returns something, not 404 from FastAPI)."""
        # SSE endpoint without auth should get redirected to login (302/303)
        # or return 401 — but NOT 404 which would mean it's not mounted
        resp = client.get("/mcp/sse")
        assert resp.status_code != 404
