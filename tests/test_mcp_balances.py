"""MCP transaction tools keep sinking-fund and budget balances in sync.

The HTML and JSON transaction routes apply the same effects through
``app.services.transaction_balances``. These tests cover each MCP tool and
one parity case against ``/api/transactions``.
"""

from decimal import Decimal

from app.mcp_server import (
    create_transaction,
    delete_transaction,
    update_transaction,
)
from app.models import Budget, Category, SinkingFund

_create_transaction = create_transaction
_update_transaction = update_transaction
_delete_transaction = delete_transaction


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _fund_balance(db_session, fund_id: int) -> Decimal:
    db_session.expire_all()
    fund = db_session.get(SinkingFund, fund_id)
    assert fund is not None
    return _money(fund.current_balance)


def _budget_figures(db_session, budget_id: int) -> tuple[Decimal, Decimal]:
    db_session.expire_all()
    budget = db_session.get(Budget, budget_id)
    assert budget is not None
    return _money(budget.fund_balance), _money(budget.spent_amount)


def _set_fund_balance(db_session, fund: SinkingFund, amount: str) -> None:
    fund.current_balance = Decimal(amount)
    db_session.commit()


def _make_budget(db_session, category: Category, month: int = 3) -> Budget:
    budget = Budget(
        category_id=category.id,
        month=month,
        year=2026,
        allocated_amount=500,
        spent_amount=40,
        fund_balance=0,
    )
    db_session.add(budget)
    db_session.commit()
    db_session.refresh(budget)
    return budget


class TestMcpSinkingFundBalances:
    def test_create_expense_subtracts(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "1000.00")

        result = _create_transaction(
            date="2026-03-15",
            amount=40.25,
            category_id=sample_category.id,
            type="expense",
            sinking_fund_id=fund.id,
        )
        assert isinstance(result, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("959.75")

    def test_create_income_and_transfer_add(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        income_fund, transfer_fund = sample_sinking_funds
        _set_fund_balance(db_session, income_fund, "1000.00")
        _set_fund_balance(db_session, transfer_fund, "1000.00")

        income = _create_transaction(
            date="2026-03-15",
            amount=40.25,
            category_id=sample_category.id,
            type="income",
            transaction_type="income",
            sinking_fund_id=income_fund.id,
        )
        transfer = _create_transaction(
            date="2026-03-15",
            amount=15,
            category_id=sample_category.id,
            type="transfer",
            transaction_type="contribution",
            sinking_fund_id=transfer_fund.id,
        )
        assert isinstance(income, dict)
        assert isinstance(transfer, dict)
        assert _fund_balance(db_session, income_fund.id) == Decimal("1040.25")
        assert _fund_balance(db_session, transfer_fund.id) == Decimal("1015.00")

    def test_create_ignores_is_paid_and_transaction_type(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        unpaid, typed = sample_sinking_funds
        _set_fund_balance(db_session, unpaid, "500.00")
        _set_fund_balance(db_session, typed, "500.00")

        # An unpaid expense still leaves the fund. transaction_type does not
        # flip the direction; only Transaction.type does.
        unpaid_txn = _create_transaction(
            date="2026-03-15",
            amount=10,
            category_id=sample_category.id,
            type="expense",
            transaction_type="contribution",
            sinking_fund_id=unpaid.id,
            is_paid=False,
        )
        income_withdrawal = _create_transaction(
            date="2026-03-15",
            amount=10,
            category_id=sample_category.id,
            type="income",
            transaction_type="withdrawal",
            sinking_fund_id=typed.id,
            is_paid=False,
        )
        assert isinstance(unpaid_txn, dict)
        assert isinstance(income_withdrawal, dict)
        assert unpaid_txn["is_paid"] is False
        assert _fund_balance(db_session, unpaid.id) == Decimal("490.00")
        assert _fund_balance(db_session, typed.id) == Decimal("510.00")

    def test_create_without_fund_leaves_balance(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "1000.00")

        result = _create_transaction(
            date="2026-03-15",
            amount=80,
            category_id=sample_category.id,
            type="expense",
        )
        assert isinstance(result, dict)
        assert result["sinking_fund_id"] is None
        assert _fund_balance(db_session, fund.id) == Decimal("1000.00")

    def test_delete_reverses_expense_and_income(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        expense_fund, income_fund = sample_sinking_funds
        _set_fund_balance(db_session, expense_fund, "1000.00")
        _set_fund_balance(db_session, income_fund, "1000.00")

        expense = _create_transaction(
            date="2026-03-15",
            amount=40,
            category_id=sample_category.id,
            type="expense",
            sinking_fund_id=expense_fund.id,
        )
        income = _create_transaction(
            date="2026-03-15",
            amount=40,
            category_id=sample_category.id,
            type="income",
            sinking_fund_id=income_fund.id,
        )
        assert isinstance(expense, dict)
        assert isinstance(income, dict)
        assert _fund_balance(db_session, expense_fund.id) == Decimal("960.00")
        assert _fund_balance(db_session, income_fund.id) == Decimal("1040.00")

        assert "deleted" in _delete_transaction(expense["id"]).lower()
        assert "deleted" in _delete_transaction(income["id"]).lower()
        assert _fund_balance(db_session, expense_fund.id) == Decimal("1000.00")
        assert _fund_balance(db_session, income_fund.id) == Decimal("1000.00")

    def test_update_amount(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "1000.00")
        created = _create_transaction(
            date="2026-03-15",
            amount=40,
            category_id=sample_category.id,
            type="expense",
            sinking_fund_id=fund.id,
        )
        assert isinstance(created, dict)

        updated = _update_transaction(transaction_id=created["id"], amount=10)
        assert isinstance(updated, dict)
        assert updated["amount"] == "10.00"
        assert _fund_balance(db_session, fund.id) == Decimal("990.00")

    def test_update_type(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "1000.00")
        created = _create_transaction(
            date="2026-03-15",
            amount=40,
            category_id=sample_category.id,
            type="expense",
            sinking_fund_id=fund.id,
        )
        assert isinstance(created, dict)

        to_income = _update_transaction(transaction_id=created["id"], type="income")
        assert isinstance(to_income, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("1040.00")

        to_transfer = _update_transaction(transaction_id=created["id"], type="transfer")
        assert isinstance(to_transfer, dict)
        # income and transfer both add, so the balance stays put.
        assert _fund_balance(db_session, fund.id) == Decimal("1040.00")

    def test_update_moves_between_funds(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        source, dest = sample_sinking_funds
        _set_fund_balance(db_session, source, "500.00")
        _set_fund_balance(db_session, dest, "200.00")
        created = _create_transaction(
            date="2026-03-15",
            amount=50,
            category_id=sample_category.id,
            type="expense",
            sinking_fund_id=source.id,
        )
        assert isinstance(created, dict)
        assert _fund_balance(db_session, source.id) == Decimal("450.00")

        moved = _update_transaction(
            transaction_id=created["id"], sinking_fund_id=dest.id
        )
        assert isinstance(moved, dict)
        assert moved["sinking_fund_id"] == dest.id
        assert _fund_balance(db_session, source.id) == Decimal("500.00")
        assert _fund_balance(db_session, dest.id) == Decimal("150.00")

    def test_update_from_no_fund_and_to_no_fund(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "500.00")
        created = _create_transaction(
            date="2026-03-15",
            amount=50,
            category_id=sample_category.id,
            type="expense",
        )
        assert isinstance(created, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("500.00")

        linked = _update_transaction(
            transaction_id=created["id"], sinking_fund_id=fund.id
        )
        assert isinstance(linked, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("450.00")

        cleared = _update_transaction(
            transaction_id=created["id"], sinking_fund_id=None
        )
        assert isinstance(cleared, dict)
        assert cleared["sinking_fund_id"] is None
        assert _fund_balance(db_session, fund.id) == Decimal("500.00")

    def test_update_amount_type_and_fund_together(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        source, dest = sample_sinking_funds
        _set_fund_balance(db_session, source, "1000.00")
        _set_fund_balance(db_session, dest, "200.00")
        created = _create_transaction(
            date="2026-03-15",
            amount=100,
            category_id=sample_category.id,
            type="expense",
            sinking_fund_id=source.id,
        )
        assert isinstance(created, dict)

        updated = _update_transaction(
            transaction_id=created["id"],
            amount=40,
            type="transfer",
            sinking_fund_id=dest.id,
        )
        assert isinstance(updated, dict)
        assert _fund_balance(db_session, source.id) == Decimal("1000.00")
        assert _fund_balance(db_session, dest.id) == Decimal("240.00")

        cleared = _update_transaction(
            transaction_id=created["id"],
            amount=30,
            type="expense",
            sinking_fund_id=None,
        )
        assert isinstance(cleared, dict)
        assert _fund_balance(db_session, source.id) == Decimal("1000.00")
        assert _fund_balance(db_session, dest.id) == Decimal("200.00")

    def test_update_is_paid_or_transaction_type_does_not_change_balance(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "500.00")
        created = _create_transaction(
            date="2026-03-15",
            amount=50,
            category_id=sample_category.id,
            type="expense",
            sinking_fund_id=fund.id,
            is_paid=True,
        )
        assert isinstance(created, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("450.00")

        updated = _update_transaction(
            transaction_id=created["id"],
            is_paid=False,
            transaction_type="withdrawal",
            description="Still the same money",
        )
        assert isinstance(updated, dict)
        assert updated["is_paid"] is False
        assert updated["transaction_type"] == "withdrawal"
        assert _fund_balance(db_session, fund.id) == Decimal("450.00")


class TestMcpBudgetFundBalance:
    def test_create_budget_transfer_moves_both_balances(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "500.00")
        budget = _make_budget(db_session, sample_category)

        result = _create_transaction(
            date="2026-03-15",
            amount=80,
            category_id=sample_category.id,
            type="expense",
            transaction_type="budget_transfer",
            sinking_fund_id=fund.id,
            budget_id=budget.id,
        )
        assert isinstance(result, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("420.00")
        fund_balance, spent = _budget_figures(db_session, budget.id)
        assert fund_balance == Decimal("80.00")
        assert spent == Decimal("40.00")

    def test_update_amount_budget_and_clear(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "500.00")
        first = _make_budget(db_session, sample_category, month=3)
        other_category = Category(
            name="Eating out",
            type="expense",
            color="#123456",
            is_budget_category=True,
        )
        db_session.add(other_category)
        db_session.commit()
        db_session.refresh(other_category)
        second = _make_budget(db_session, other_category, month=3)

        created = _create_transaction(
            date="2026-03-15",
            amount=25,
            category_id=sample_category.id,
            type="expense",
            transaction_type="budget_transfer",
            sinking_fund_id=fund.id,
            budget_id=first.id,
        )
        assert isinstance(created, dict)
        assert _budget_figures(db_session, first.id)[0] == Decimal("25.00")

        retargeted = _update_transaction(
            transaction_id=created["id"], amount=10, budget_id=second.id
        )
        assert isinstance(retargeted, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("490.00")
        assert _budget_figures(db_session, first.id)[0] == Decimal("0.00")
        assert _budget_figures(db_session, second.id)[0] == Decimal("10.00")
        assert _budget_figures(db_session, second.id)[1] == Decimal("40.00")

        cleared = _update_transaction(
            transaction_id=created["id"], budget_id=None, transaction_type="regular"
        )
        assert isinstance(cleared, dict)
        assert cleared["budget_id"] is None
        # The fund link is unchanged, so the expense still reduces the fund.
        assert _fund_balance(db_session, fund.id) == Decimal("490.00")
        assert _budget_figures(db_session, second.id)[0] == Decimal("0.00")

    def test_delete_budget_transfer_reverses_both(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "500.00")
        budget = _make_budget(db_session, sample_category)
        created = _create_transaction(
            date="2026-03-15",
            amount=80,
            category_id=sample_category.id,
            type="expense",
            transaction_type="budget_transfer",
            sinking_fund_id=fund.id,
            budget_id=budget.id,
        )
        assert isinstance(created, dict)

        assert "deleted" in _delete_transaction(created["id"]).lower()
        assert _fund_balance(db_session, fund.id) == Decimal("500.00")
        fund_balance, spent = _budget_figures(db_session, budget.id)
        assert fund_balance == Decimal("0.00")
        assert spent == Decimal("40.00")

    def test_other_types_do_not_change_budget_figures(
        self, db_session, setup_database, sample_category, sample_sinking_funds
    ):
        fund = sample_sinking_funds[0]
        _set_fund_balance(db_session, fund, "500.00")
        budget = _make_budget(db_session, sample_category)

        created = _create_transaction(
            date="2026-03-15",
            amount=50,
            category_id=sample_category.id,
            type="expense",
            transaction_type="budget_expense",
            sinking_fund_id=fund.id,
            budget_id=budget.id,
        )
        assert isinstance(created, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("450.00")
        fund_balance, spent = _budget_figures(db_session, budget.id)
        assert fund_balance == Decimal("0.00")
        assert spent == Decimal("40.00")

        updated = _update_transaction(transaction_id=created["id"], amount=20)
        assert isinstance(updated, dict)
        assert _fund_balance(db_session, fund.id) == Decimal("480.00")
        fund_balance, spent = _budget_figures(db_session, budget.id)
        assert fund_balance == Decimal("0.00")
        assert spent == Decimal("40.00")


class TestRouteMcpBalanceParity:
    def test_api_and_mcp_leave_the_same_balances(
        self,
        authed_client,
        db_session,
        sample_category,
        sample_sinking_funds,
    ):
        """The JSON route and MCP tools apply the same balance sequence."""
        api_primary, api_other = sample_sinking_funds
        mcp_primary = SinkingFund(
            name="MCP primary", color="#111111", current_balance=0
        )
        mcp_other = SinkingFund(name="MCP other", color="#222222", current_balance=0)
        db_session.add_all([mcp_primary, mcp_other])
        db_session.commit()
        for fund, opening in (
            (api_primary, "1000.00"),
            (api_other, "250.00"),
            (mcp_primary, "1000.00"),
            (mcp_other, "250.00"),
        ):
            _set_fund_balance(db_session, fund, opening)

        api_budget = _make_budget(db_session, sample_category, month=4)
        mcp_category = Category(
            name="MCP groceries",
            type="expense",
            color="#333333",
            is_budget_category=True,
        )
        db_session.add(mcp_category)
        db_session.commit()
        db_session.refresh(mcp_category)
        mcp_budget = _make_budget(db_session, mcp_category, month=4)

        def assert_pair(
            primary, other, budget, expected_primary, expected_other, expected_fb
        ):
            assert _fund_balance(db_session, primary) == Decimal(expected_primary)
            assert _fund_balance(db_session, other) == Decimal(expected_other)
            assert _budget_figures(db_session, budget) == (
                Decimal(expected_fb),
                Decimal("40.00"),
            )

        api_created = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-04-02",
                "amount": "125.50",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "withdrawal",
                "sinking_fund_id": api_primary.id,
                "is_paid": False,
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert api_created.status_code == 201
        api_id = api_created.json()["id"]
        mcp_created = _create_transaction(
            date="2026-04-02",
            amount=125.50,
            category_id=mcp_category.id,
            type="expense",
            transaction_type="withdrawal",
            sinking_fund_id=mcp_primary.id,
            is_paid=False,
        )
        assert isinstance(mcp_created, dict)
        for primary, other, budget in (
            (api_primary.id, api_other.id, api_budget.id),
            (mcp_primary.id, mcp_other.id, mcp_budget.id),
        ):
            assert_pair(primary, other, budget, "874.50", "250.00", "0.00")

        api_updated = authed_client.put(
            f"/api/transactions/{api_id}",
            json={"amount": "20.00", "type": "income"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert api_updated.status_code == 200
        mcp_updated = _update_transaction(
            transaction_id=mcp_created["id"], amount=20, type="income"
        )
        assert isinstance(mcp_updated, dict)
        for primary, other, budget in (
            (api_primary.id, api_other.id, api_budget.id),
            (mcp_primary.id, mcp_other.id, mcp_budget.id),
        ):
            assert_pair(primary, other, budget, "1020.00", "250.00", "0.00")

        api_moved = authed_client.put(
            f"/api/transactions/{api_id}",
            json={"sinking_fund_id": api_other.id},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert api_moved.status_code == 200
        mcp_moved = _update_transaction(
            transaction_id=mcp_created["id"], sinking_fund_id=mcp_other.id
        )
        assert isinstance(mcp_moved, dict)
        for primary, other, budget in (
            (api_primary.id, api_other.id, api_budget.id),
            (mcp_primary.id, mcp_other.id, mcp_budget.id),
        ):
            assert_pair(primary, other, budget, "1000.00", "270.00", "0.00")

        api_cleared = authed_client.put(
            f"/api/transactions/{api_id}",
            json={"sinking_fund_id": None},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert api_cleared.status_code == 200
        assert api_cleared.json()["sinking_fund_id"] is None
        mcp_cleared = _update_transaction(
            transaction_id=mcp_created["id"], sinking_fund_id=None
        )
        assert isinstance(mcp_cleared, dict)
        assert mcp_cleared["sinking_fund_id"] is None
        for primary, other, budget in (
            (api_primary.id, api_other.id, api_budget.id),
            (mcp_primary.id, mcp_other.id, mcp_budget.id),
        ):
            assert_pair(primary, other, budget, "1000.00", "250.00", "0.00")

        api_transfer = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-04-03",
                "amount": "60.00",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "budget_transfer",
                "sinking_fund_id": api_primary.id,
                "budget_id": api_budget.id,
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert api_transfer.status_code == 201
        api_transfer_id = api_transfer.json()["id"]
        mcp_transfer = _create_transaction(
            date="2026-04-03",
            amount=60,
            category_id=mcp_category.id,
            type="expense",
            transaction_type="budget_transfer",
            sinking_fund_id=mcp_primary.id,
            budget_id=mcp_budget.id,
        )
        assert isinstance(mcp_transfer, dict)
        for primary, other, budget in (
            (api_primary.id, api_other.id, api_budget.id),
            (mcp_primary.id, mcp_other.id, mcp_budget.id),
        ):
            assert_pair(primary, other, budget, "940.00", "250.00", "60.00")

        api_resized = authed_client.put(
            f"/api/transactions/{api_transfer_id}",
            json={"amount": "15.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert api_resized.status_code == 200
        mcp_resized = _update_transaction(transaction_id=mcp_transfer["id"], amount=15)
        assert isinstance(mcp_resized, dict)
        for primary, other, budget in (
            (api_primary.id, api_other.id, api_budget.id),
            (mcp_primary.id, mcp_other.id, mcp_budget.id),
        ):
            assert_pair(primary, other, budget, "985.00", "250.00", "15.00")

        api_deleted = authed_client.delete(
            f"/api/transactions/{api_transfer_id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert api_deleted.status_code == 200
        assert "deleted" in _delete_transaction(mcp_transfer["id"]).lower()
        for primary, other, budget in (
            (api_primary.id, api_other.id, api_budget.id),
            (mcp_primary.id, mcp_other.id, mcp_budget.id),
        ):
            assert_pair(primary, other, budget, "1000.00", "250.00", "0.00")
