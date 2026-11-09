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
