"""add wallet payment methods

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "m3n4o5p6q7r8"
down_revision: str | None = "l2m3n4o5p6q7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WALLET_CHECK = (
    "(kind = 'wallet' AND wallet_provider IN ('apple_pay', 'google_pay') "
    "AND funding_method_id IS NOT NULL AND funding_method_id != id) "
    "OR (kind != 'wallet' AND wallet_provider IS NULL AND funding_method_id IS NULL)"
)
_ITEM_CHECK = (
    "(recurring_bill_id IS NOT NULL AND wallet_method_id IS NULL) OR "
    "(recurring_bill_id IS NULL AND wallet_method_id IS NOT NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("payment_methods", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("wallet_provider", sa.String(length=20), nullable=True)
        )
        batch_op.add_column(sa.Column("funding_method_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_payment_methods_funding_method_id",
            ["funding_method_id"],
        )
        batch_op.create_foreign_key(
            "fk_payment_methods_funding_method_id",
            "payment_methods",
            ["funding_method_id"],
            ["id"],
        )
        batch_op.create_check_constraint("ck_payment_methods_wallet", _WALLET_CHECK)
    with op.batch_alter_table(
        "payment_method_replacement_items", schema=None
    ) as batch_op:
        batch_op.alter_column(
            "recurring_bill_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.add_column(sa.Column("wallet_method_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_pm_replacement_items_wallet_method_id",
            "payment_methods",
            ["wallet_method_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "uq_pm_replacement_items_rep_wallet",
            ["replacement_id", "wallet_method_id"],
        )
        batch_op.create_check_constraint(
            "ck_pm_replacement_items_one_target", _ITEM_CHECK
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "payment_method_replacement_items", schema=None
    ) as batch_op:
        batch_op.drop_constraint("ck_pm_replacement_items_one_target", type_="check")
        batch_op.drop_constraint("uq_pm_replacement_items_rep_wallet", type_="unique")
        batch_op.drop_constraint(
            "fk_pm_replacement_items_wallet_method_id", type_="foreignkey"
        )
        batch_op.drop_column("wallet_method_id")
        batch_op.alter_column(
            "recurring_bill_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
    with op.batch_alter_table("payment_methods", schema=None) as batch_op:
        batch_op.drop_constraint("ck_payment_methods_wallet", type_="check")
        batch_op.drop_constraint(
            "fk_payment_methods_funding_method_id", type_="foreignkey"
        )
        batch_op.drop_index("ix_payment_methods_funding_method_id")
        batch_op.drop_column("funding_method_id")
        batch_op.drop_column("wallet_provider")
