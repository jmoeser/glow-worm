"""add paid_from to recurring bills

Revision ID: j1k2l3m4n5o6
Revises: i1j2k3l4m5n6
Create Date: 2026-11-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "j1k2l3m4n5o6"
down_revision: str | None = "i1j2k3l4m5n6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "paid_from",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'bills_fund'"),
            )
        )
        batch_op.add_column(sa.Column("paid_from_fund_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_recurring_bills_paid_from_fund_id",
            "sinking_funds",
            ["paid_from_fund_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_recurring_bills_paid_from_fund_id",
            ["paid_from_fund_id"],
        )
        batch_op.create_check_constraint(
            "ck_recurring_bills_paid_from",
            "paid_from IN ('bills_fund', 'budget', 'sinking_fund')",
        )
        batch_op.create_check_constraint(
            "ck_recurring_bills_paid_from_fund",
            "(paid_from = 'sinking_fund' AND paid_from_fund_id IS NOT NULL) "
            "OR (paid_from <> 'sinking_fund' AND paid_from_fund_id IS NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.drop_constraint("ck_recurring_bills_paid_from_fund", type_="check")
        batch_op.drop_constraint("ck_recurring_bills_paid_from", type_="check")
        batch_op.drop_index("ix_recurring_bills_paid_from_fund_id")
        batch_op.drop_constraint(
            "fk_recurring_bills_paid_from_fund_id", type_="foreignkey"
        )
        batch_op.drop_column("paid_from_fund_id")
        batch_op.drop_column("paid_from")
