"""add is_system to categories

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-03 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("categories") as batch_op:
        batch_op.add_column(
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default="0")
        )

    # Mark the Transfer category as a system category
    op.execute(
        sa.text("UPDATE categories SET is_system = true WHERE type = 'transfer'")
    )

    # Mark the first income category as a system category
    op.execute(
        sa.text(
            "UPDATE categories SET is_system = true "
            "WHERE type = 'income' AND id = (SELECT MIN(id) FROM categories WHERE type = 'income')"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_column("is_system")
