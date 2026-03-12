"""add_foreign_currency_to_recurring_bills

Revision ID: a2b3c4d5e6f7
Revises: 30d9a4ca0b49
Create Date: 2026-03-12 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "30d9a4ca0b49"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "foreign_amount", sa.Numeric(precision=12, scale=2), nullable=True
            )
        )
        batch_op.add_column(sa.Column("foreign_currency", sa.String(3), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("recurring_bills", schema=None) as batch_op:
        batch_op.drop_column("foreign_currency")
        batch_op.drop_column("foreign_amount")
