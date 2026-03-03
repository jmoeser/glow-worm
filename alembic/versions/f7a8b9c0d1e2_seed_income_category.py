"""seed income system category

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-03-04 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c0d1e2"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Insert a default income category if none exists (fresh installs).
    # Existing installs already have a user-created income category; this only
    # fires when there are no income-type categories at all.
    op.execute(
        sa.text(
            "INSERT INTO categories (name, type, color, is_budget_category, is_deleted, is_system) "
            "SELECT 'Income', 'income', '#22C55E', false, false, true "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM categories WHERE type = 'income' AND is_deleted = false"
            ")"
        )
    )

    # Ensure the first income category is marked as a system category.
    # migration f6a7b8c9d0e1 ran this UPDATE but it was a no-op on installs
    # where no income category existed yet.
    op.execute(
        sa.text(
            "UPDATE categories SET is_system = true "
            "WHERE type = 'income' AND id = (SELECT MIN(id) FROM categories WHERE type = 'income')"
        )
    )


def downgrade() -> None:
    # Only remove the seeded category if it's the generic "Income" one we created.
    # Don't touch user-renamed or pre-existing income categories.
    op.execute(
        sa.text(
            "DELETE FROM categories "
            "WHERE name = 'Income' AND type = 'income' AND is_system = true"
        )
    )
