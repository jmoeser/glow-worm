"""add overflow_sinking_fund to income_allocation

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-03-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("income_allocations") as batch_op:
        batch_op.add_column(
            sa.Column("overflow_sinking_fund_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_income_alloc_overflow_fund",
            "sinking_funds",
            ["overflow_sinking_fund_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("income_allocations") as batch_op:
        batch_op.drop_constraint("fk_income_alloc_overflow_fund", type_="foreignkey")
        batch_op.drop_column("overflow_sinking_fund_id")
