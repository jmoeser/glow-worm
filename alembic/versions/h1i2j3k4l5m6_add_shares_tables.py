"""add shares tables

Revision ID: h1i2j3k4l5m6
Revises: g1h2i3j4k5l6
Create Date: 2026-10-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "h1i2j3k4l5m6"
down_revision: str | None = "g1h2i3j4k5l6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "share_trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("trade_type", sa.String(length=4), nullable=False),
        sa.Column("trade_date", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fee", sa.Numeric(12, 2), nullable=False),
        sa.Column("fee_currency", sa.String(length=3), nullable=False),
        sa.Column("fx_rate_aud_usd", sa.Numeric(18, 10), nullable=True),
        sa.Column("fx_rate_input", sa.Numeric(18, 10), nullable=True),
        sa.Column("fx_input_convention", sa.String(length=7), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_share_trades_ticker", "share_trades", ["ticker"])
    op.create_index(
        "ix_share_trades_ticker_date",
        "share_trades",
        ["ticker", "trade_date"],
    )
    op.create_table(
        "share_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("price_date", sa.String(length=10), nullable=False),
        sa.Column("close", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("ticker", "price_date", name="uq_share_prices_ticker_date"),
    )
    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("base", sa.String(length=3), nullable=False),
        sa.Column("quote", sa.String(length=3), nullable=False),
        sa.Column("rate_date", sa.String(length=10), nullable=False),
        sa.Column("rate", sa.Numeric(18, 10), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("base", "quote", "rate_date", name="uq_fx_rates_pair_date"),
    )
    op.create_table(
        "market_data_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("av_requests", sa.Integer(), nullable=False),
        sa.Column("tickers_updated", sa.Integer(), nullable=False),
        sa.Column("fx_source", sa.String(length=30), nullable=True),
        sa.Column("message", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("market_data_runs")
    op.drop_table("fx_rates")
    op.drop_table("share_prices")
    op.drop_index("ix_share_trades_ticker_date", table_name="share_trades")
    op.drop_index("ix_share_trades_ticker", table_name="share_trades")
    op.drop_table("share_trades")
