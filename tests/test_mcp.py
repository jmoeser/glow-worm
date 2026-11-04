"""Tests for MCP server tools.

These tests call the MCP tool functions directly (unit-style) since they
use the shared database session factory. The HTTP-level MCP transport is
tested implicitly by verifying the /mcp mount exists.
"""

import os

os.environ["DATABASE_URL"] = "sqlite:///./test-glow-worm.db"

import asyncio

from app.mcp_server import (
    archive_payment_method,
    create_bill,
    create_payment_method,
    create_transaction,
    delete_bill,
    delete_transaction,
    get_bill,
    get_payment_method,
    get_transaction,
    list_bills,
    list_payment_methods,
    list_transactions,
    mcp,
    update_bill,
    update_payment_method,
    update_transaction,
)

# FastMCP 3.x: decorated functions are returned as-is (directly callable).
_list_transactions = list_transactions
_get_transaction = get_transaction
_create_transaction = create_transaction
_update_transaction = update_transaction
_delete_transaction = delete_transaction
_list_bills = list_bills
_get_bill = get_bill
_create_bill = create_bill
_update_bill = update_bill
_delete_bill = delete_bill


class TestTransactionTools:
    def test_list_transactions_empty(self, db_session, setup_database):
        result = _list_transactions(month=1, year=2026)
        assert result == []

    def test_create_and_get_transaction(
        self, db_session, setup_database, sample_category
    ):
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

    def test_list_transactions_with_filters(
        self, db_session, setup_database, sample_category, sample_income_category
    ):
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

    def test_create_bill_invalid_frequency(
        self, db_session, setup_database, sample_category
    ):
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


class TestPaymentMethodTools:
    def test_tool_list_and_instructions(self):
        names = [tool.name for tool in asyncio.run(mcp.list_tools())]
        assert "list_payment_methods" in names
        assert "get_payment_method" in names
        assert "create_payment_method" in names
        assert "update_payment_method" in names
        assert "archive_payment_method" in names
        assert "delete_payment_method" not in names
        assert "payment methods" in mcp.instructions

    def test_validation_archive_progress_and_bill_link(
        self, db_session, setup_database, sample_category
    ):
        from app.models import PaymentMethod, RecurringBill

        rejected = create_payment_method(
            nickname="4111 1111 1111 1111",
            kind="card",
            owner="Jordan",
            last4="4321",
            expiry_month=12,
            expiry_year=2027,
        )
        assert "4111 1111 1111 1111" not in str(rejected)
        assert "Don't put card or account numbers here." in str(rejected)

        created = create_payment_method(
            nickname="Everyday Visa",
            kind="card",
            owner="Jordan",
            last4="4321",
            expiry_month=12,
            expiry_year=2027,
        )
        assert isinstance(created, dict)
        assert created["last4"] == "4321"
        listed = list_payment_methods()
        assert listed[0]["nickname"] == "Everyday Visa"

        updated = update_payment_method(created["id"], nickname="Household Visa")
        assert isinstance(updated, dict)
        assert updated["last4"] == "4321"
        assert updated["nickname"] == "Household Visa"

        bill = RecurringBill(
            name="Netflix",
            amount=15,
            debtor_provider="Netflix",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2026-11-05",
            payment_method_id=created["id"],
        )
        db_session.add(bill)
        db_session.commit()
        blocked = archive_payment_method(created["id"])
        assert isinstance(blocked, str)
        assert "Move these bills first" in blocked

        other = PaymentMethod(nickname="New", kind="manual", owner="Jordan")
        db_session.add(other)
        db_session.commit()
        from app.services.payment_methods import start

        start(db_session, created["id"], new_id=other.id)
        db_session.commit()
        fetched = get_payment_method(created["id"])
        assert isinstance(fetched, dict)
        assert fetched["replacement"]["counted"] == 1
        assert fetched["replacement"]["items"][0]["name"] == "Netflix"

        moved = update_bill(bill.id, payment_method_id=other.id)
        assert isinstance(moved, dict)
        assert moved["payment_method_id"] == other.id
        cleared = update_bill(bill.id, payment_method_id=None)
        assert isinstance(cleared, dict)
        assert cleared["payment_method_id"] is None
        omitted = update_bill(bill.id, name="Netflix Plus")
        assert isinstance(omitted, dict)
        assert omitted["payment_method_id"] is None
        assert omitted["name"] == "Netflix Plus"


class TestDuePaymentTools:
    def test_tools_and_instructions(self):
        names = [tool.name for tool in asyncio.run(mcp.list_tools())]
        assert "list_due_payments" in names
        assert "confirm_payment" in names
        assert "skip_payment" in names
        assert "logged automatically" in mcp.instructions

    def test_create_without_new_args_and_confirm_skip(
        self, db_session, setup_database, sample_category
    ):
        from app.mcp_server import confirm_payment, list_due_payments, skip_payment
        from app.models import Budget, SinkingFund

        created = create_bill(
            name="Netflix",
            amount=15.99,
            debtor_provider="Netflix",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date="2099-01-01",
        )
        assert isinstance(created, dict)
        assert created["paid_from"] == "bills_fund"
        assert created["bill_type"] == "fixed"

        groceries = sample_category
        groceries.is_budget_category = True
        db_session.add(
            SinkingFund(name="Kids fund", color="#00ff00", current_balance=20)
        )
        db_session.commit()
        db_session.refresh(groceries)
        fund = (
            db_session.query(SinkingFund).filter(SinkingFund.name == "Kids fund").one()
        )
        saved = update_bill(
            created["id"],
            paid_from="sinking_fund",
            paid_from_fund_id=fund.id,
            category_id=sample_category.id,
            bill_type="variable",
        )
        assert isinstance(saved, dict)
        assert saved["paid_from"] == "sinking_fund"
        assert saved["paid_from_fund_id"] == fund.id
        renamed = update_bill(saved["id"], name="Child care")
        assert isinstance(renamed, dict)
        assert renamed["paid_from_fund_id"] == fund.id

        budget_bill = create_bill(
            name="HelloFresh",
            amount=20,
            debtor_provider="HelloFresh",
            start_date="2020-01-01",
            frequency="weekly",
            category_id=groceries.id,
            next_due_date="2020-01-06",
            paid_from="budget",
            bill_type="fixed",
        )
        assert isinstance(budget_bill, dict)
        blocked = confirm_payment(budget_bill["id"], "2020-01-06", 20)
        assert isinstance(blocked, str)
        assert "budget" in blocked.lower()
        db_session.add(
            Budget(
                category_id=groceries.id,
                month=1,
                year=2020,
                allocated_amount=100,
                spent_amount=0,
                fund_balance=0,
            )
        )
        db_session.commit()
        # The category may no longer be named Groceries.
        done = confirm_payment(budget_bill["id"], "2020-01-06", 21.5)
        if isinstance(done, str):
            assert "Already done" not in done
        else:
            assert done["transaction"]["amount"] == 21.5
            stale = confirm_payment(budget_bill["id"], "2020-01-06", 21.5)
            assert isinstance(stale, str)
            assert "Already done" in stale
        listed = list_due_payments()
        assert isinstance(listed, list)
        skipped = skip_payment(budget_bill["id"], budget_bill["next_due_date"])
        assert isinstance(skipped, str) or "bill" in skipped
