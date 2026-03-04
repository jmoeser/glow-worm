"""remove_monthly_allocation_from_sinking_funds

Revision ID: 30d9a4ca0b49
Revises: f7a8b9c0d1e2
Create Date: 2026-03-04 13:42:47.805918

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "30d9a4ca0b49"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("sinking_funds", schema=None) as batch_op:
        batch_op.drop_column("monthly_allocation")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sinking_funds", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "monthly_allocation", sa.NUMERIC(precision=12, scale=2), nullable=False
            )
        )
