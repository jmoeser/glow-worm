"""add_income_allocation_recurring_transfers

Revision ID: c5517c16c6c1
Revises: f6a7b8c9d0e1
Create Date: 2026-03-03 18:07:49.214592

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5517c16c6c1"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "income_allocation_recurring_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("income_allocation_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.ForeignKeyConstraint(["income_allocation_id"], ["income_allocations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("income_allocation_recurring_transfers")
