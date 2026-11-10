"""add occurrence due date and auto-pay day

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-11-10

No data update and no balance update. Existing rows keep both new
transaction columns null, so they do not occupy the partial unique keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "n4o5p6q7r8s9"
down_revision: str | None = "m3n4o5p6q7r8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_transactions_not_occurrence_and_adjustment"
_OCCURRENCE_INDEX = "uq_transactions_bill_occurrence"
_ADJUSTMENT_INDEX = "uq_transactions_adjustment_for"

# Kept on the revision so tests can see both dialect WHERE clauses.
# Upgrade creates the indexes from these objects, outside batch mode.
OCCURRENCE_INDEX = sa.Index(
    _OCCURRENCE_INDEX,
    sa.column("recurring_bill_id"),
    sa.column("occurrence_due_date"),
    unique=True,
    sqlite_where=sa.text("occurrence_due_date IS NOT NULL"),
    postgresql_where=sa.text("occurrence_due_date IS NOT NULL"),
)
ADJUSTMENT_INDEX = sa.Index(
    _ADJUSTMENT_INDEX,
    sa.column("adjustment_for_transaction_id"),
    unique=True,
    sqlite_where=sa.text("adjustment_for_transaction_id IS NOT NULL"),
    postgresql_where=sa.text("adjustment_for_transaction_id IS NOT NULL"),
)


def _create_partial(index: sa.Index) -> None:
    column_names = [column.name for column in index.expressions]
    op.create_index(
        index.name,
        "transactions",
        column_names,
        unique=True,
        sqlite_where=index.dialect_options["sqlite"]["where"],
        postgresql_where=index.dialect_options["postgresql"]["where"],
    )


def upgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("occurrence_due_date", sa.String(length=10), nullable=True)
        )
        batch_op.add_column(
            sa.Column("adjustment_for_transaction_id", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            _CHECK,
            "occurrence_due_date IS NULL OR adjustment_for_transaction_id IS NULL",
        )
    _create_partial(OCCURRENCE_INDEX)
    _create_partial(ADJUSTMENT_INDEX)
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("last_auto_settled_on", sa.String(length=10), nullable=True)
        )


def downgrade() -> None:
    op.drop_index(_ADJUSTMENT_INDEX, table_name="transactions")
    op.drop_index(_OCCURRENCE_INDEX, table_name="transactions")
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.drop_column("adjustment_for_transaction_id")
        batch_op.drop_column("occurrence_due_date")
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.drop_column("last_auto_settled_on")
