"""add contracts register

Revision ID: k1l2m3n4o5p6
Revises: j1k2l3m4n5o6
Create Date: 2026-11-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "k1l2m3n4o5p6"
down_revision: str | None = "j1k2l3m4n5o6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BILL_INDEX = "ix_transactions_recurring_bill_id"


def _transactions_have_bill_index() -> bool:
    inspector = sa.inspect(op.get_bind())
    for index in inspector.get_indexes("transactions"):
        if index.get("name") == _BILL_INDEX:
            return True
        if index.get("column_names") == ["recurring_bill_id"]:
            return True
    return False


def upgrade() -> None:
    op.create_table(
        "contracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=150), nullable=False),
        sa.Column("plan_name", sa.String(length=150), nullable=True),
        sa.Column("service", sa.String(length=30), nullable=False),
        sa.Column("bill_delivery", sa.String(length=20), nullable=False),
        sa.Column("delivery_detail", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("started_on", sa.String(length=10), nullable=True),
        sa.Column("ended_on", sa.String(length=10), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "service IN ('electricity', 'health_insurance', 'internet', "
            "'phone', 'car_insurance', 'other')",
            name="ck_contracts_service",
        ),
        sa.CheckConstraint(
            "bill_delivery IN ('email', 'post', 'app', 'other')",
            name="ck_contracts_bill_delivery",
        ),
    )
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.add_column(sa.Column("contract_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_recurring_bills_contract_id",
            "contracts",
            ["contract_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_recurring_bills_contract_id", ["contract_id"])
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("contract_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_transactions_contract_id",
            "contracts",
            ["contract_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_transactions_contract_id", ["contract_id"])
    if not _transactions_have_bill_index():
        op.create_index(_BILL_INDEX, "transactions", ["recurring_bill_id"])


def downgrade() -> None:
    if _transactions_have_bill_index():
        op.drop_index(_BILL_INDEX, table_name="transactions")
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_index("ix_transactions_contract_id")
        batch_op.drop_constraint("fk_transactions_contract_id", type_="foreignkey")
        batch_op.drop_column("contract_id")
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.drop_index("ix_recurring_bills_contract_id")
        batch_op.drop_constraint("fk_recurring_bills_contract_id", type_="foreignkey")
        batch_op.drop_column("contract_id")
    op.drop_table("contracts")
