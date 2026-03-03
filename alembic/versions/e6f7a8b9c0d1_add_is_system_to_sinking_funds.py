"""add is_system to sinking funds

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-03-04 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sinking_funds") as batch_op:
        batch_op.add_column(
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default="0")
        )

    # Insert Bills sinking fund if it doesn't exist (fresh installs)
    op.execute(
        sa.text(
            "INSERT INTO sinking_funds "
            "(name, monthly_allocation, current_balance, color, is_deleted, is_system, "
            "created_at, updated_at) "
            "SELECT 'Bills', 800, 0, '#EF4444', false, true, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM sinking_funds WHERE name = 'Bills' AND is_deleted = false"
            ")"
        )
    )

    # Mark existing Bills fund as system on existing installs
    op.execute(
        sa.text(
            "UPDATE sinking_funds SET is_system = true "
            "WHERE name = 'Bills' AND is_deleted = false"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE sinking_funds SET is_system = false WHERE name = 'Bills'")
    )
    with op.batch_alter_table("sinking_funds") as batch_op:
        batch_op.drop_column("is_system")
