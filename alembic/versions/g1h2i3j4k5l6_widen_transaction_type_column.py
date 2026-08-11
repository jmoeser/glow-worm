"""widen transaction_type column for secondary_income_allocation

Revision ID: g1h2i3j4k5l6
Revises: f0e1d2c3b4a5
Create Date: 2026-06-26

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g1h2i3j4k5l6"
down_revision: str | None = "f0e1d2c3b4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "transaction_type",
            existing_type=sa.String(length=20),
            type_=sa.String(length=30),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "transaction_type",
            existing_type=sa.String(length=30),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
