"""Alembic upgrade and downgrade for payment methods on SQLite."""

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.models import (
    Category,
    PaymentMethod,
    PaymentMethodReplacement,
    PaymentMethodReplacementItem,
    RecurringBill,
)


def test_sqlite_payment_method_migration_round_trip(tmp_path):
    url = f"sqlite:///{tmp_path / 'pay.sqlite'}"
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["SECRET_KEY"] = "migration-test-secret-key-not-for-production-use"
    root = Path(__file__).resolve().parents[1]

    def alembic(*args: str) -> None:
        result = subprocess.run(
            ["uv", "run", "alembic", *args],
            check=False,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "head")
    engine = create_engine(url)
    session = sessionmaker(bind=engine)()
    category = Category(name="Bills", type="expense", color="#112233")
    session.add(category)
    session.flush()
    old = PaymentMethod(
        nickname="Everyday Visa",
        kind="card",
        last4="4321",
        expiry_month=10,
        expiry_year=2026,
        owner="Jordan",
    )
    new = PaymentMethod(
        nickname="New Visa",
        kind="card",
        last4="9876",
        expiry_month=3,
        expiry_year=2028,
        owner="Jordan",
    )
    session.add_all([old, new])
    session.flush()
    bill = RecurringBill(
        name="Netflix",
        amount=15,
        debtor_provider="Netflix",
        start_date="2026-01-01",
        frequency="monthly",
        category_id=category.id,
        next_due_date="2026-11-05",
        payment_method_id=old.id,
    )
    session.add(bill)
    session.flush()
    replacement = PaymentMethodReplacement(old_id=old.id, new_id=new.id, status="open")
    session.add(replacement)
    session.flush()
    session.add(
        PaymentMethodReplacementItem(
            replacement_id=replacement.id,
            recurring_bill_id=bill.id,
            note="called",
        )
    )
    session.commit()
    inspector = inspect(engine)
    pay_columns = {
        column["name"] for column in inspector.get_columns("payment_methods")
    }
    assert "wallet_provider" in pay_columns
    assert "funding_method_id" in pay_columns
    item_columns = inspector.get_columns("payment_method_replacement_items")
    item_names = {column["name"] for column in item_columns}
    assert "wallet_method_id" in item_names
    bill_column = next(
        column for column in item_columns if column["name"] == "recurring_bill_id"
    )
    assert bill_column["nullable"] is True
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO payment_methods (nickname, kind, owner, is_archived, "
                "created_at, updated_at, wallet_provider, funding_method_id) "
                "VALUES ('Phone', 'wallet', 'Jordan', 0, '2026-01-01', "
                "'2026-01-01', NULL, NULL)"
            )
        )
        session.commit()
    session.rollback()
    session.close()
    engine.dispose()

    alembic("downgrade", "h1i2j3k4l5m6")
    engine = create_engine(url)
    inspector = inspect(engine)
    names = set(inspector.get_table_names())
    assert "payment_methods" not in names
    assert "payment_method_replacements" not in names
    assert "payment_method_replacement_items" not in names
    columns = {column["name"] for column in inspector.get_columns("recurring_bills")}
    assert "payment_method_id" not in columns
    engine.dispose()

    alembic("upgrade", "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "payment_methods" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("recurring_bills")}
    assert "payment_method_id" in columns
    pay_columns = {
        column["name"] for column in inspector.get_columns("payment_methods")
    }
    assert "funding_method_id" in pay_columns
    engine.dispose()


def test_paid_from_revision_backfills_and_rejects_a_half_set_row(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    url = f"sqlite:///{tmp_path / 'paid-from.sqlite'}"
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["SECRET_KEY"] = "migration-test-secret-key-not-for-production-use"
    root = Path(__file__).resolve().parents[1]

    def alembic(*args: str) -> None:
        result = subprocess.run(
            ["uv", "run", "alembic", *args],
            check=False,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "i1j2k3l4m5n6")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO categories (name, type, color, is_budget_category, "
                "is_deleted, is_system, exclude_from_monthly_cost) "
                "VALUES ('Bills', 'expense', '#112233', 0, 0, 1, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO recurring_bills (name, amount, debtor_provider, "
                "start_date, frequency, category_id, is_active, next_due_date, "
                "created_at, updated_at) VALUES ("
                "'Power', 40, 'Grid', '2026-01-01', 'monthly', 1, 1, "
                "'2026-06-01', '2026-01-01', '2026-01-01')"
            )
        )
    engine.dispose()

    alembic("upgrade", "j1k2l3m4n5o6")
    engine = create_engine(url)
    with engine.connect() as conn:
        paid_from = conn.execute(
            text("SELECT paid_from, paid_from_fund_id FROM recurring_bills")
        ).one()
        assert paid_from[0] == "bills_fund"
        assert paid_from[1] is None
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO recurring_bills (name, amount, debtor_provider, "
                    "start_date, frequency, category_id, is_active, next_due_date, "
                    "created_at, updated_at, paid_from, paid_from_fund_id) VALUES ("
                    "'Box', 10, 'Shop', '2026-01-01', 'weekly', 1, 1, '2026-06-01', "
                    "'2026-01-01', '2026-01-01', 'sinking_fund', NULL)"
                )
            )
            conn.commit()
        conn.rollback()
    engine.dispose()

    alembic("downgrade", "i1j2k3l4m5n6")
    engine = create_engine(url)
    columns = {
        column["name"] for column in inspect(engine).get_columns("recurring_bills")
    }
    assert "paid_from" not in columns
    engine.dispose()

    alembic("upgrade", "j1k2l3m4n5o6")
    engine = create_engine(url)
    columns = {
        column["name"] for column in inspect(engine).get_columns("recurring_bills")
    }
    assert "paid_from" in columns
    assert "paid_from_fund_id" in columns
    engine.dispose()


def test_contracts_revision_round_trip(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.models import Category, Contract, RecurringBill

    url = f"sqlite:///{tmp_path / 'contracts.sqlite'}"
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["SECRET_KEY"] = "migration-test-secret-key-not-for-production-use"
    root = Path(__file__).resolve().parents[1]

    def alembic(*args: str) -> None:
        result = subprocess.run(
            ["uv", "run", "alembic", *args],
            check=False,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "head")
    engine = create_engine(url)
    session = sessionmaker(bind=engine)()
    category = Category(name="Bills", type="expense", color="#112233")
    session.add(category)
    session.flush()
    contract = Contract(
        name="Home electricity",
        provider="Origin",
        service="electricity",
        bill_delivery="email",
    )
    session.add(contract)
    session.flush()
    session.add(
        RecurringBill(
            name="Power",
            amount=40,
            debtor_provider="Origin",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=category.id,
            next_due_date="2026-06-01",
            contract_id=contract.id,
        )
    )
    session.commit()
    session.close()

    with engine.connect() as conn:
        indexes = {idx["name"] for idx in inspect(engine).get_indexes("transactions")}
        assert "ix_transactions_contract_id" in indexes
        assert "ix_transactions_recurring_bill_id" in indexes
        bill_indexes = {
            idx["name"] for idx in inspect(engine).get_indexes("recurring_bills")
        }
        assert "ix_recurring_bills_contract_id" in bill_indexes
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO contracts (name, provider, service, bill_delivery, "
                    "created_at, updated_at) VALUES ("
                    "'Gas', 'Supplier', 'gas', 'email', '2026-01-01', '2026-01-01')"
                )
            )
            conn.commit()
        conn.rollback()
    engine.dispose()

    alembic("downgrade", "j1k2l3m4n5o6")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "contracts" not in inspector.get_table_names()
    bill_columns = {
        column["name"] for column in inspector.get_columns("recurring_bills")
    }
    txn_columns = {column["name"] for column in inspector.get_columns("transactions")}
    assert "contract_id" not in bill_columns
    assert "contract_id" not in txn_columns
    txn_indexes = {idx["name"] for idx in inspector.get_indexes("transactions")}
    assert "ix_transactions_recurring_bill_id" not in txn_indexes
    engine.dispose()

    alembic("upgrade", "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "contracts" in inspector.get_table_names()
    bill_columns = {
        column["name"] for column in inspector.get_columns("recurring_bills")
    }
    txn_columns = {column["name"] for column in inspector.get_columns("transactions")}
    assert "contract_id" in bill_columns
    assert "contract_id" in txn_columns
    engine.dispose()


def test_partial_index_objects_carry_both_where_clauses():
    import importlib.util

    revision = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "n4o5p6q7r8s9_add_occurrence_due_date.py"
    )
    spec = importlib.util.spec_from_file_location("occurrence_revision", revision)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    for index in (module.OCCURRENCE_INDEX, module.ADJUSTMENT_INDEX):
        sqlite_where = index.dialect_options["sqlite"]["where"]
        postgres_where = index.dialect_options["postgresql"]["where"]
        assert sqlite_where is not None
        assert postgres_where is not None
        assert "IS NOT NULL" in str(sqlite_where)
        assert "IS NOT NULL" in str(postgres_where)


def test_occurrence_upgrade_does_not_move_money_and_downgrade_drops_columns(tmp_path):
    from datetime import date
    from decimal import Decimal

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'occurrence.sqlite'}"
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["SECRET_KEY"] = "migration-test-secret-key-not-for-production-use"
    root = Path(__file__).resolve().parents[1]

    def alembic(*args: str) -> None:
        result = subprocess.run(
            ["uv", "run", "alembic", *args],
            check=False,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "m3n4o5p6q7r8")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO categories (name, type, color, is_budget_category, "
                "is_deleted, is_system, exclude_from_monthly_cost) VALUES "
                "('Ledger', 'expense', '#112233', 0, 0, 0, 0), "
                "('Groceries', 'expense', '#22C55E', 1, 0, 0, 0), "
                "('Move', 'transfer', '#888888', 0, 0, 0, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sinking_funds (name, current_balance, color, is_deleted, "
                "is_system, created_at, updated_at) VALUES "
                "('Overflow', 80, '#0000ff', 0, 0, '2026-01-01', '2026-01-01'), "
                "('Savings', 15, '#00ff00', 0, 0, '2026-01-01', '2026-01-01')"
            )
        )
        bills_cat = conn.execute(
            text("SELECT id FROM categories WHERE name = 'Ledger'")
        ).scalar_one()
        groceries = conn.execute(
            text("SELECT id FROM categories WHERE name = 'Groceries'")
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE sinking_funds SET current_balance = 500 "
                "WHERE name = 'Bills' AND is_deleted = 0"
            )
        )
        bills_fund = conn.execute(
            text("SELECT id FROM sinking_funds WHERE name = 'Bills' AND is_deleted = 0")
        ).scalar_one()
        overflow = conn.execute(
            text("SELECT id FROM sinking_funds WHERE name = 'Overflow'")
        ).scalar_one()
        savings = conn.execute(
            text("SELECT id FROM sinking_funds WHERE name = 'Savings'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO recurring_bills (name, amount, debtor_provider, start_date, "
                "frequency, category_id, is_active, next_due_date, created_at, updated_at, "
                "bill_type, paid_from) VALUES "
                "('Power', 40, 'Grid', '2026-01-01', 'monthly', :cat, 1, '2026-11-03', "
                "'2026-01-01', '2026-01-01', 'fixed', 'bills_fund'), "
                "('Box', 20, 'Shop', '2026-01-01', 'monthly', :groceries, 1, '2026-10-26', "
                "'2026-01-01', '2026-01-01', 'fixed', 'budget')"
            ),
            {"cat": bills_cat, "groceries": groceries},
        )
        power = conn.execute(
            text("SELECT id FROM recurring_bills WHERE name = 'Power'")
        ).scalar_one()
        box = conn.execute(
            text("SELECT id FROM recurring_bills WHERE name = 'Box'")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO budgets (category_id, month, year, allocated_amount, "
                "spent_amount, fund_balance, created_at, updated_at) VALUES "
                "(:cat, 10, 2026, 100, 25, 0, '2026-01-01', '2026-01-01')"
            ),
            {"cat": groceries},
        )
        budget_id = conn.execute(text("SELECT id FROM budgets")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO transactions (date, description, amount, category_id, type, "
                "transaction_type, sinking_fund_id, recurring_bill_id, is_paid, created_at) "
                "VALUES ('2026-11-01', 'Old power', 40, :cat, 'expense', 'regular', "
                ":fund, :bill, 1, '2026-11-01')"
            ),
            {"cat": bills_cat, "fund": bills_fund, "bill": power},
        )
        conn.execute(
            text(
                "INSERT INTO transactions (date, description, amount, category_id, type, "
                "transaction_type, budget_id, recurring_bill_id, is_paid, created_at) "
                "VALUES ('2026-10-26', 'Old box', 25, :cat, 'expense', 'budget_expense', "
                ":budget, :bill, 1, '2026-10-26')"
            ),
            {"cat": groceries, "budget": budget_id, "bill": box},
        )
        conn.execute(
            text(
                "INSERT INTO transactions (date, description, amount, category_id, type, "
                "transaction_type, sinking_fund_id, recurring_bill_id, is_paid, created_at) "
                "VALUES ('2026-11-03', 'Taken back for Box (October 2026 Groceries)', 25, "
                ":cat, 'expense', 'withdrawal', :fund, :bill, 1, '2026-11-03')"
            ),
            {
                "cat": conn.execute(
                    text("SELECT id FROM categories WHERE name = 'Move'")
                ).scalar_one(),
                "fund": overflow,
                "bill": box,
            },
        )
        conn.execute(
            text(
                "INSERT INTO household_months (year, month, settlement_outcome, "
                "settlement_fund_id, settlement_amount, settled_on) VALUES "
                "(2026, 10, 'surplus', :fund, 20, '2026-11-03')"
            ),
            {"fund": overflow},
        )
        conn.execute(
            text(
                "UPDATE household_processing SET backfilled_at = '2026-01-01' WHERE id = 1"
            )
        )
        conn.execute(
            text(
                "INSERT INTO income_allocations (monthly_income_amount, "
                "monthly_budget_allocation, bills_fund_allocation_type, "
                "bills_fund_fixed_amount, created_at, updated_at, "
                "overflow_sinking_fund_id) VALUES "
                "(1000, 100, 'fixed', 0, '2026-01-01', '2026-01-01', :savings)"
            ),
            {"savings": savings},
        )

    def snapshot(conn):
        balances = {
            name: amount
            for name, amount in conn.execute(
                text("SELECT name, current_balance FROM sinking_funds ORDER BY name")
            )
        }
        spent = conn.execute(text("SELECT spent_amount FROM budgets")).scalar_one()
        dues = {
            name: due
            for name, due in conn.execute(
                text("SELECT name, next_due_date FROM recurring_bills ORDER BY name")
            )
        }
        settlement = conn.execute(
            text("SELECT settlement_outcome, settlement_fund_id FROM household_months")
        ).one()
        amounts = [
            amount
            for (amount,) in conn.execute(
                text("SELECT amount FROM transactions ORDER BY id")
            )
        ]
        return balances, spent, dues, settlement, amounts

    with engine.connect() as conn:
        before = snapshot(conn)
    engine.dispose()

    alembic("upgrade", "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        after = snapshot(conn)
        assert after == before
        new_txn = conn.execute(
            text(
                "SELECT occurrence_due_date, adjustment_for_transaction_id "
                "FROM transactions"
            )
        ).all()
        assert all(row[0] is None and row[1] is None for row in new_txn)
        settled = conn.execute(
            text("SELECT last_auto_settled_on FROM recurring_bills")
        ).all()
        assert all(row[0] is None for row in settled)
        index_sql = {
            name: sql
            for name, sql in conn.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type = 'index'")
            )
        }
        assert "WHERE" in index_sql["uq_transactions_bill_occurrence"].upper()
        assert "WHERE" in index_sql["uq_transactions_adjustment_for"].upper()
    engine.dispose()

    from app.models import RecurringBill
    from app.services.recurring_payments import confirm_payment
    from app.tasks import process_due_bills

    engine = create_engine(url)
    Session = sessionmaker(bind=engine)
    session = Session()
    import app.services.recurring_payments as payments
    from app import tasks

    day = date(2026, 11, 3)
    previous_task_today = tasks._today
    previous_payment_today = payments._today
    tasks._today = lambda: day
    payments._today = lambda: day
    power = session.query(RecurringBill).filter(RecurringBill.name == "Power").one()
    old_power_amount = session.execute(
        text("SELECT amount FROM transactions WHERE description = 'Old power'")
    ).scalar_one()
    process_due_bills(db=session)
    session.rollback()
    auto = session.execute(
        text(
            "SELECT occurrence_due_date, amount FROM transactions "
            "WHERE description LIKE 'Auto-payment:%'"
        )
    ).one()
    assert auto[0] == "2026-11-03"
    assert Decimal(str(auto[1])) == Decimal("40.00")
    assert (
        session.execute(
            text("SELECT amount FROM transactions WHERE description = 'Old power'")
        ).scalar_one()
        == old_power_amount
    )
    box = session.query(RecurringBill).filter(RecurringBill.name == "Box").one()
    confirm_payment(session, box, Decimal("12.00"), "2026-10-26")
    session.commit()
    fresh = session.execute(
        text(
            "SELECT description, amount, occurrence_due_date, adjustment_for_transaction_id "
            "FROM transactions ORDER BY id"
        )
    ).all()
    old_amounts = [row[1] for row in fresh if row[2] is None and row[3] is None]
    assert [Decimal(str(amount)) for amount in old_amounts] == [
        Decimal(40),
        Decimal(25),
        Decimal(25),
    ]
    new_payment = [
        row for row in fresh if row[0] and row[0].startswith("Bill payment:")
    ]
    assert len(new_payment) == 1
    assert new_payment[0][2] == "2026-10-26"
    new_claw = [
        row
        for row in fresh
        if row[0] and row[0].startswith("Taken back for Box") and row[3] is not None
    ]
    assert len(new_claw) == 1
    settlement = session.execute(
        text("SELECT settlement_outcome, settlement_fund_id FROM household_months")
    ).one()
    assert settlement[0] == "surplus"
    assert settlement[1] == overflow
    balance_after = session.execute(
        text("SELECT current_balance FROM sinking_funds WHERE name = 'Bills'")
    ).scalar_one()
    session.close()
    engine.dispose()
    tasks._today = previous_task_today
    payments._today = previous_payment_today

    alembic("downgrade", "m3n4o5p6q7r8")
    engine = create_engine(url)
    inspector = inspect(engine)
    txn_columns = {column["name"] for column in inspector.get_columns("transactions")}
    bill_columns = {
        column["name"] for column in inspector.get_columns("recurring_bills")
    }
    assert "occurrence_due_date" not in txn_columns
    assert "adjustment_for_transaction_id" not in txn_columns
    assert "last_auto_settled_on" not in bill_columns
    with engine.connect() as conn:
        balance = conn.execute(
            text("SELECT current_balance FROM sinking_funds WHERE name = 'Bills'")
        ).scalar_one()
        assert Decimal(str(balance)) == Decimal(str(balance_after))
        settlement = conn.execute(
            text("SELECT settlement_outcome, settlement_fund_id FROM household_months")
        ).one()
        assert settlement[0] == "surplus"
    engine.dispose()
