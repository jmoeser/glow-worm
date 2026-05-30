"""add secondary income allocation

Revision ID: d5f6a7b8c9e0
Revises: c4d5e6f7a8b9
Create Date: 2026-05-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f6a7b8c9e0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secondary_income_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "secondary_income_allocation_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("secondary_income_allocation_id", sa.Integer(), nullable=False),
        sa.Column("sinking_fund_id", sa.Integer(), nullable=False),
        sa.Column("percentage", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.ForeignKeyConstraint(
            ["secondary_income_allocation_id"],
            ["secondary_income_allocations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["sinking_fund_id"],
            ["sinking_funds.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("secondary_income_allocation_rules")
    op.drop_table("secondary_income_allocations")
