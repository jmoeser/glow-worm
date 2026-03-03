"""mark_bills_category_as_system

Revision ID: d5e6f7a8b9c0
Revises: c5517c16c6c1
Create Date: 2026-03-04 06:48:20.629924

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "c5517c16c6c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Insert the Bills category if it doesn't already exist (fresh installs)
    op.execute(
        sa.text(
            "INSERT INTO categories (name, type, color, is_budget_category, is_deleted, is_system) "
            "SELECT 'Bills', 'expense', '#EF4444', false, false, true "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM categories WHERE name = 'Bills' AND type = 'expense' AND is_deleted = false"
            ")"
        )
    )

    # Mark the Bills expense category as a system category on existing installs
    op.execute(
        sa.text(
            "UPDATE categories SET is_system = true "
            "WHERE name = 'Bills' AND type = 'expense' AND is_deleted = false"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE categories SET is_system = false "
            "WHERE name = 'Bills' AND type = 'expense'"
        )
    )
