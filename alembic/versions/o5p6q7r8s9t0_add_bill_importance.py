"""add importance and notes to recurring bills

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-11-12

No data update and no balance update. Existing rows stay null, which
means not set yet. Downgrade drops the check, then the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o5p6q7r8s9t0"
down_revision: str | None = "n4o5p6q7r8s9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_recurring_bills_importance"


def upgrade() -> None:
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("importance", sa.String(length=20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cancel_note", sa.String(length=500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("if_stopped_note", sa.String(length=500), nullable=True)
        )
        batch_op.create_check_constraint(
            _CHECK,
            "importance IS NULL OR importance IN ('essential', 'optional')",
        )


def downgrade() -> None:
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.drop_column("if_stopped_note")
        batch_op.drop_column("cancel_note")
        batch_op.drop_column("importance")
