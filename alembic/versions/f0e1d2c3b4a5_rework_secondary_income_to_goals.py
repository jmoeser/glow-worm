"""rework secondary income allocation to goal-based approach

Revision ID: f0e1d2c3b4a5
Revises: d5f6a7b8c9e0
Create Date: 2026-05-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f0e1d2c3b4a5"
down_revision: str | None = "d5f6a7b8c9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("secondary_income_allocation_rules") as batch_op:
        batch_op.drop_column("percentage")
        batch_op.add_column(
            sa.Column(
                "goal_amount",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "sort_order",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    with op.batch_alter_table("secondary_income_allocations") as batch_op:
        batch_op.add_column(
            sa.Column("overflow_sinking_fund_id", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("secondary_income_allocations") as batch_op:
        batch_op.drop_column("overflow_sinking_fund_id")

    with op.batch_alter_table("secondary_income_allocation_rules") as batch_op:
        batch_op.drop_column("sort_order")
        batch_op.drop_column("goal_amount")
        batch_op.add_column(
            sa.Column(
                "percentage",
                sa.Numeric(precision=5, scale=4),
                nullable=False,
                server_default="0",
            )
        )
