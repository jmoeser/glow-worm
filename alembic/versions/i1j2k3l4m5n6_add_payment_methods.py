"""add payment methods

Revision ID: i1j2k3l4m5n6
Revises: h1i2j3k4l5m6
Create Date: 2026-11-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "i1j2k3l4m5n6"
down_revision: str | None = "h1i2j3k4l5m6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nickname", sa.String(length=60), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("last4", sa.String(length=4), nullable=True),
        sa.Column("expiry_month", sa.Integer(), nullable=True),
        sa.Column("expiry_year", sa.Integer(), nullable=True),
        sa.Column("owner", sa.String(length=60), nullable=False),
        sa.Column(
            "is_archived", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "payment_method_replacements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("old_id", sa.Integer(), nullable=False),
        sa.Column("new_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'finished', 'stopped')",
            name="ck_pm_replacements_status",
        ),
        sa.CheckConstraint("old_id != new_id", name="ck_pm_replacements_distinct"),
        sa.ForeignKeyConstraint(["old_id"], ["payment_methods.id"]),
        sa.ForeignKeyConstraint(["new_id"], ["payment_methods.id"]),
    )
    op.create_index(
        "ix_payment_method_replacements_old_id",
        "payment_method_replacements",
        ["old_id"],
    )
    op.create_index(
        "uq_pm_replacements_open_old",
        "payment_method_replacements",
        ["old_id"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "payment_method_replacement_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("replacement_id", sa.Integer(), nullable=False),
        sa.Column("recurring_bill_id", sa.Integer(), nullable=False),
        sa.Column("updated_on", sa.String(length=10), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(
            ["replacement_id"],
            ["payment_method_replacements.id"],
        ),
        sa.ForeignKeyConstraint(["recurring_bill_id"], ["recurring_bills.id"]),
        sa.UniqueConstraint(
            "replacement_id",
            "recurring_bill_id",
            name="uq_pm_replacement_items_rep_bill",
        ),
    )
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.add_column(sa.Column("payment_method_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_recurring_bills_payment_method_id",
            "payment_methods",
            ["payment_method_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_recurring_bills_payment_method_id",
            ["payment_method_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.drop_index("ix_recurring_bills_payment_method_id")
        batch_op.drop_constraint(
            "fk_recurring_bills_payment_method_id", type_="foreignkey"
        )
        batch_op.drop_column("payment_method_id")
    op.drop_table("payment_method_replacement_items")
    op.drop_index(
        "uq_pm_replacements_open_old",
        table_name="payment_method_replacements",
    )
    op.drop_index(
        "ix_payment_method_replacements_old_id",
        table_name="payment_method_replacements",
    )
    op.drop_table("payment_method_replacements")
    op.drop_table("payment_methods")
