"""Alembic upgrade and downgrade for payment methods on SQLite."""

import os
import subprocess
from pathlib import Path

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
    session.close()
    engine.dispose()

    alembic("downgrade", "-1")
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
    engine.dispose()
