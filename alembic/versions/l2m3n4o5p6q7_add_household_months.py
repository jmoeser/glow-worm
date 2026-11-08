"""add household month processing records

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-11-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "l2m3n4o5p6q7"
down_revision: str | None = "k1l2m3n4o5p6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "household_months",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("income_transaction_id", sa.Integer(), nullable=True),
        sa.Column("settlement_outcome", sa.String(length=20), nullable=True),
        sa.Column("settlement_fund_id", sa.Integer(), nullable=True),
        sa.Column("settlement_transaction_id", sa.Integer(), nullable=True),
        sa.Column("settlement_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("settled_on", sa.String(length=10), nullable=True),
        sa.ForeignKeyConstraint(
            ["income_transaction_id"],
            ["transactions.id"],
            name="fk_household_months_income_txn",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_transaction_id"],
            ["transactions.id"],
            name="fk_household_months_settlement_txn",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("year", "month", name="uq_household_months_year_month"),
        sa.CheckConstraint(
            "month >= 1 AND month <= 12", name="ck_household_months_month"
        ),
        sa.CheckConstraint(
            "(settlement_outcome IS NULL AND settlement_fund_id IS NULL) OR "
            "(settlement_outcome IN ('no_overflow', 'unresolved') "
            "AND settlement_fund_id IS NULL) OR "
            "(settlement_outcome IN ('surplus', 'shortfall', 'zero') "
            "AND settlement_fund_id IS NOT NULL)",
            name="ck_household_months_settlement",
        ),
    )
    op.create_table(
        "household_processing",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("backfilled_at", sa.DateTime(), nullable=True),
        sa.Column("held_year", sa.Integer(), nullable=True),
        sa.Column("held_month", sa.Integer(), nullable=True),
        sa.Column("attempt_year", sa.Integer(), nullable=True),
        sa.Column("attempt_month", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "(held_year IS NULL AND held_month IS NULL) OR "
            "(held_year IS NOT NULL AND held_month >= 1 AND held_month <= 12)",
            name="ck_household_processing_hold",
        ),
        sa.CheckConstraint(
            "(attempt_year IS NULL AND attempt_month IS NULL) OR "
            "(attempt_year IS NOT NULL AND attempt_month >= 1 "
            "AND attempt_month <= 12)",
            name="ck_household_processing_attempt",
        ),
    )
    op.execute(sa.text("INSERT INTO household_processing (id) VALUES (1)"))


def downgrade() -> None:
    op.drop_table("household_processing")
    op.drop_table("household_months")
