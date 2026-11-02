"""Editing or deleting a budget expense keeps Budget.spent_amount in step.

Quick-expense, the transactions pages, the JSON API, and the MCP tools all
count the same rows: an expense classified as budget_expense with a budget
link. Income and contribution refunds do not. The CLI deletes through the
JSON API and has no edit command, so the API cases cover it.
"""

import math
from decimal import Decimal

from app.mcp_server import (
    create_transaction,
    delete_transaction,
    update_transaction,
)
from app.models import Budget, Category, Transaction
from app.services.transaction_balances import adjust_budget_spent_amount

_create_transaction = create_transaction
_update_transaction = update_transaction
_delete_transaction = delete_transaction


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _spent(db_session, budget_id: int) -> Decimal:
    db_session.expire_all()
    budget = db_session.get(Budget, budget_id)
    assert budget is not None
    return _money(budget.spent_amount)


def _fund_balance(db_session, budget_id: int) -> Decimal:
    db_session.expire_all()
    budget = db_session.get(Budget, budget_id)
    assert budget is not None
    return _money(budget.fund_balance)


def _make_budget(
    db_session,
    category: Category,
    *,
    spent: str = "100.00",
    fund: str = "0.00",
    month: int = 3,
) -> Budget:
    budget = Budget(
        category_id=category.id,
        month=month,
        year=2026,
        allocated_amount=500,
        spent_amount=Decimal(spent),
        fund_balance=Decimal(fund),
    )
    db_session.add(budget)
    db_session.commit()
    db_session.refresh(budget)
    return budget


def _insert_expense(
    db_session,
    category: Category,
    budget: Budget | None,
    amount: str = "25.00",
    *,
    txn_type: str = "expense",
    transaction_type: str = "budget_expense",
) -> Transaction:
    txn = Transaction(
        date="2026-03-15",
        description="Budget spend",
        amount=Decimal(amount),
        category_id=category.id,
        type=txn_type,
        transaction_type=transaction_type,
        budget_id=budget.id if budget is not None else None,
    )
    db_session.add(txn)
    db_session.commit()
    db_session.refresh(txn)
    return txn


def _api(authed_client, method: str, path: str, **kwargs):
    headers = {"x-csrftoken": authed_client.csrf_token}
    return getattr(authed_client, method)(path, headers=headers, **kwargs)


class TestApiBudgetSpent:
    def test_delete_subtracts_the_amount(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="100.00")
        txn = _insert_expense(db_session, sample_category, budget, "25.50")

        response = _api(authed_client, "delete", f"/api/transactions/{txn.id}")
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("74.50")
        assert _fund_balance(db_session, budget.id) == Decimal("0.00")

    def test_edit_amount_applies_the_difference(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="100.00")
        txn = _insert_expense(db_session, sample_category, budget, "40.00")

        response = _api(
            authed_client,
            "put",
            f"/api/transactions/{txn.id}",
            json={"amount": "55.25"},
        )
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("115.25")
        assert _fund_balance(db_session, budget.id) == Decimal("0.00")

    def test_edit_moves_the_amount_between_budgets(
        self, authed_client, db_session, sample_budget_categories
    ):
        groceries, transport = sample_budget_categories[0], sample_budget_categories[1]
        source = _make_budget(db_session, groceries, spent="100.00", month=3)
        dest = _make_budget(db_session, transport, spent="20.00", month=3)
        txn = _insert_expense(db_session, groceries, source, "30.00")

        response = _api(
            authed_client,
            "put",
            f"/api/transactions/{txn.id}",
            json={"budget_id": dest.id},
        )
        assert response.status_code == 200
        assert response.json()["budget_id"] == dest.id
        assert _spent(db_session, source.id) == Decimal("70.00")
        assert _spent(db_session, dest.id) == Decimal("50.00")

    def test_clearing_budget_id_removes_the_spend(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="100.00")
        txn = _insert_expense(db_session, sample_category, budget, "30.00")

        response = _api(
            authed_client,
            "put",
            f"/api/transactions/{txn.id}",
            json={"budget_id": None},
        )
        assert response.status_code == 200
        assert response.json()["budget_id"] is None
        assert _spent(db_session, budget.id) == Decimal("70.00")

    def test_setting_budget_id_adds_the_spend(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="100.00")
        txn = _insert_expense(db_session, sample_category, None, "18.00")
        assert _spent(db_session, budget.id) == Decimal("100.00")

        response = _api(
            authed_client,
            "put",
            f"/api/transactions/{txn.id}",
            json={"budget_id": budget.id},
        )
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("118.00")

    def test_type_change_stops_and_starts_counting(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="100.00")
        txn = _insert_expense(db_session, sample_category, budget, "22.00")

        to_income = _api(
            authed_client,
            "put",
            f"/api/transactions/{txn.id}",
            json={"type": "income"},
        )
        assert to_income.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("78.00")

        to_expense = _api(
            authed_client,
            "put",
            f"/api/transactions/{txn.id}",
            json={"type": "expense"},
        )
        assert to_expense.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("100.00")

    def test_regular_expense_with_a_budget_does_not_count(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="40.00")
        created = _api(
            authed_client,
            "post",
            "/api/transactions",
            json={
                "date": "2026-03-15",
                "amount": "9.00",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "regular",
                "budget_id": budget.id,
            },
        )
        assert created.status_code == 201
        assert _spent(db_session, budget.id) == Decimal("40.00")
        assert _fund_balance(db_session, budget.id) == Decimal("0.00")

        counted = _api(
            authed_client,
            "put",
            f"/api/transactions/{created.json()['id']}",
            json={"transaction_type": "budget_expense"},
        )
        assert counted.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("49.00")

    def test_income_against_a_budget_does_not_change_spent(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="80.00")
        created = _api(
            authed_client,
            "post",
            "/api/transactions",
            json={
                "date": "2026-03-15",
                "amount": "15.00",
                "category_id": sample_category.id,
                "type": "income",
                "transaction_type": "budget_expense",
                "budget_id": budget.id,
            },
        )
        assert created.status_code == 201
        assert _spent(db_session, budget.id) == Decimal("80.00")

        deleted = _api(
            authed_client, "delete", f"/api/transactions/{created.json()['id']}"
        )
        assert deleted.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("80.00")

    def test_contribution_refund_does_not_change_spent(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="80.00")
        for txn_type in ("expense", "income"):
            created = _api(
                authed_client,
                "post",
                "/api/transactions",
                json={
                    "date": "2026-03-15",
                    "amount": "12.00",
                    "category_id": sample_category.id,
                    "type": txn_type,
                    "transaction_type": "contribution",
                    "budget_id": budget.id,
                },
            )
            assert created.status_code == 201
            assert _spent(db_session, budget.id) == Decimal("80.00")
            deleted = _api(
                authed_client, "delete", f"/api/transactions/{created.json()['id']}"
            )
            assert deleted.status_code == 200
            assert _spent(db_session, budget.id) == Decimal("80.00")

        expense = _insert_expense(db_session, sample_category, budget, "12.00")
        reclassed = _api(
            authed_client,
            "put",
            f"/api/transactions/{expense.id}",
            json={"transaction_type": "contribution"},
        )
        assert reclassed.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("68.00")

    def test_budget_transfer_does_not_change_spent(
        self, authed_client, db_session, sample_category, sample_sinking_funds
    ):
        budget = _make_budget(db_session, sample_category, spent="40.00", fund="0.00")
        fund = sample_sinking_funds[0]
        fund.current_balance = Decimal("200.00")
        db_session.commit()

        created = _api(
            authed_client,
            "post",
            "/api/transactions",
            json={
                "date": "2026-03-15",
                "amount": "30.00",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "budget_transfer",
                "sinking_fund_id": fund.id,
                "budget_id": budget.id,
            },
        )
        assert created.status_code == 201
        assert _spent(db_session, budget.id) == Decimal("40.00")
        assert _fund_balance(db_session, budget.id) == Decimal("30.00")

        updated = _api(
            authed_client,
            "put",
            f"/api/transactions/{created.json()['id']}",
            json={"amount": "10.00"},
        )
        assert updated.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("40.00")
        assert _fund_balance(db_session, budget.id) == Decimal("10.00")

    def test_create_then_delete_round_trips(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="40.00")
        created = _api(
            authed_client,
            "post",
            "/api/transactions",
            json={
                "date": "2026-03-15",
                "amount": "19.95",
                "category_id": sample_category.id,
                "type": "expense",
                "transaction_type": "budget_expense",
                "budget_id": budget.id,
            },
        )
        assert created.status_code == 201
        assert _spent(db_session, budget.id) == Decimal("59.95")

        deleted = _api(
            authed_client, "delete", f"/api/transactions/{created.json()['id']}"
        )
        assert deleted.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("40.00")

    def test_delete_clamps_at_zero(self, authed_client, db_session, sample_category):
        budget = _make_budget(db_session, sample_category, spent="5.00")
        txn = _insert_expense(db_session, sample_category, budget, "12.50")

        response = _api(authed_client, "delete", f"/api/transactions/{txn.id}")
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("0.00")

    def test_amount_edit_clamps_after_the_net_change(
        self, authed_client, db_session, sample_category
    ):
        # Spent is already below this row's amount. The edit subtracts the
        # old amount and adds the new one once, then clamps, so the result
        # stays at zero instead of coming back as the new amount.
        budget = _make_budget(db_session, sample_category, spent="5.00")
        txn = _insert_expense(db_session, sample_category, budget, "12.50")

        response = _api(
            authed_client,
            "put",
            f"/api/transactions/{txn.id}",
            json={"amount": "3.00"},
        )
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("0.00")

    def test_paid_flag_does_not_change_spent(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="40.00")
        txn = _insert_expense(db_session, sample_category, budget, "10.00")
        budget.spent_amount = Decimal("40.00")
        db_session.commit()

        response = _api(
            authed_client,
            "put",
            f"/api/transactions/{txn.id}",
            json={"is_paid": False},
        )
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("40.00")


class TestSpentRounding:
    def test_quantize_to_cents(self, db_session, sample_category):
        budget = _make_budget(db_session, sample_category, spent="10.00")
        adjust_budget_spent_amount(
            db_session,
            budget_id=budget.id,
            txn_type="expense",
            transaction_type="budget_expense",
            amount=Decimal("1.235"),
        )
        db_session.commit()
        assert _spent(db_session, budget.id) == Decimal("11.24")

    def test_negative_zero_is_stored_as_zero(self, db_session, sample_category):
        budget = _make_budget(db_session, sample_category, spent="0.00")
        adjust_budget_spent_amount(
            db_session,
            budget_id=budget.id,
            txn_type="expense",
            transaction_type="budget_expense",
            amount=Decimal("0.004"),
            reverse=True,
        )
        db_session.commit()
        assert _spent(db_session, budget.id) == Decimal("0.00")
        stored = float(budget.spent_amount)
        assert stored == 0.0
        assert math.copysign(1.0, stored) == 1.0

    def test_missing_budget_is_a_no_op(self, db_session):
        adjust_budget_spent_amount(
            db_session,
            budget_id=999999,
            txn_type="expense",
            transaction_type="budget_expense",
            amount=Decimal("10.00"),
        )


class TestWebBudgetSpent:
    def test_html_create_increments_spent(
        self, authed_client, db_session, sample_category
    ):
        budget = _make_budget(db_session, sample_category, spent="10.00")
        response = authed_client.post(
            "/transactions",
            data={
                "date": "2026-03-15",
                "description": "Groceries",
                "amount": "8.40",
                "category_id": str(sample_category.id),
                "type": "expense",
                "transaction_type": "budget_expense",
                "budget_id": str(budget.id),
                "month": "3",
                "year": "2026",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("18.40")

    def test_html_edit_amount(self, authed_client, db_session, sample_category):
        budget = _make_budget(db_session, sample_category, spent="50.00")
        txn = _insert_expense(db_session, sample_category, budget, "20.00")

        response = authed_client.post(
            f"/transactions/{txn.id}",
            data={"amount": "12.00"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("42.00")

    def test_html_delete(self, authed_client, db_session, sample_category):
        budget = _make_budget(db_session, sample_category, spent="50.00")
        txn = _insert_expense(db_session, sample_category, budget, "20.00")

        response = authed_client.delete(
            f"/transactions/{txn.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert _spent(db_session, budget.id) == Decimal("30.00")

    def test_quick_expense_delete_restores_spent(
        self, authed_client, db_session, sample_budgets
    ):
        budget = sample_budgets[0]
        month, year = budget.month, budget.year
        opening = _spent(db_session, budget.id)

        created = authed_client.post(
            "/dashboard/quick-expense",
            data={
                "budget_id": str(budget.id),
                "amount": "42.50",
                "date": f"{year}-{month:02d}-15",
                "month": str(month),
                "year": str(year),
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert created.status_code == 200
        assert _spent(db_session, budget.id) == opening + Decimal("42.50")

        txn = (
            db_session.query(Transaction)
            .filter(Transaction.budget_id == budget.id)
            .one()
        )
        deleted = authed_client.delete(
            f"/transactions/{txn.id}",
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert deleted.status_code == 200
        assert _spent(db_session, budget.id) == opening


class TestMcpBudgetSpent:
    def test_update_amount_and_delete(
        self, db_session, sample_category, setup_database
    ):
        budget = _make_budget(db_session, sample_category, spent="40.00")
        created = _create_transaction(
            date="2026-03-15",
            amount=25,
            category_id=sample_category.id,
            type="expense",
            transaction_type="budget_expense",
            budget_id=budget.id,
        )
        assert isinstance(created, dict)
        assert _spent(db_session, budget.id) == Decimal("65.00")

        updated = _update_transaction(transaction_id=created["id"], amount=10)
        assert isinstance(updated, dict)
        assert _spent(db_session, budget.id) == Decimal("50.00")

        assert "deleted" in _delete_transaction(created["id"]).lower()
        assert _spent(db_session, budget.id) == Decimal("40.00")

    def test_move_clear_and_set_budget(
        self, db_session, sample_budget_categories, setup_database
    ):
        groceries, transport = sample_budget_categories[0], sample_budget_categories[1]
        source = _make_budget(db_session, groceries, spent="80.00", month=4)
        dest = _make_budget(db_session, transport, spent="10.00", month=4)
        created = _create_transaction(
            date="2026-04-02",
            amount=15,
            category_id=groceries.id,
            type="expense",
            transaction_type="budget_expense",
            budget_id=source.id,
        )
        assert isinstance(created, dict)
        assert _spent(db_session, source.id) == Decimal("95.00")

        moved = _update_transaction(transaction_id=created["id"], budget_id=dest.id)
        assert isinstance(moved, dict)
        assert _spent(db_session, source.id) == Decimal("80.00")
        assert _spent(db_session, dest.id) == Decimal("25.00")

        cleared = _update_transaction(transaction_id=created["id"], budget_id=None)
        assert isinstance(cleared, dict)
        assert cleared["budget_id"] is None
        assert _spent(db_session, dest.id) == Decimal("10.00")

        restored = _update_transaction(
            transaction_id=created["id"], budget_id=source.id
        )
        assert isinstance(restored, dict)
        assert _spent(db_session, source.id) == Decimal("95.00")

    def test_type_change_to_income_drops_spent(
        self, db_session, sample_category, setup_database
    ):
        budget = _make_budget(db_session, sample_category, spent="30.00")
        created = _create_transaction(
            date="2026-03-15",
            amount=8,
            category_id=sample_category.id,
            type="expense",
            transaction_type="budget_expense",
            budget_id=budget.id,
        )
        assert isinstance(created, dict)
        assert _spent(db_session, budget.id) == Decimal("38.00")

        updated = _update_transaction(transaction_id=created["id"], type="income")
        assert isinstance(updated, dict)
        assert _spent(db_session, budget.id) == Decimal("30.00")
