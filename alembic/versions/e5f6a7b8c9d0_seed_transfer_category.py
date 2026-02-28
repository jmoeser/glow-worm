"""seed Transfer system category

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-01 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO categories (name, type, color, is_budget_category, is_deleted) "
            "VALUES ('Transfer', 'transfer', '#6B7280', 0, 0)"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM categories WHERE name = 'Transfer' AND type = 'transfer'")
    )
