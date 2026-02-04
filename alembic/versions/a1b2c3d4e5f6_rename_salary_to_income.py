"""rename salary to income

Revision ID: a1b2c3d4e5f6
Revises: ff882a43230d
Create Date: 2026-02-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'ff882a43230d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rename tables
    op.rename_table('salary_allocations', 'income_allocations')
    op.rename_table('salary_allocation_to_sinking_funds', 'income_allocation_to_sinking_funds')

    # Rename columns (SQLite requires batch mode)
    with op.batch_alter_table('income_allocations') as batch_op:
        batch_op.alter_column('monthly_salary_amount', new_column_name='monthly_income_amount')

    with op.batch_alter_table('income_allocation_to_sinking_funds') as batch_op:
        batch_op.alter_column('salary_allocation_id', new_column_name='income_allocation_id')

    # Update transaction_type values
    op.execute("UPDATE transactions SET transaction_type = 'income' WHERE transaction_type = 'salary'")
    op.execute("UPDATE transactions SET transaction_type = 'income_allocation' WHERE transaction_type = 'salary_allocation'")


def downgrade() -> None:
    # Revert transaction_type values
    op.execute("UPDATE transactions SET transaction_type = 'salary' WHERE transaction_type = 'income'")
    op.execute("UPDATE transactions SET transaction_type = 'salary_allocation' WHERE transaction_type = 'income_allocation'")

    # Revert column renames
    with op.batch_alter_table('income_allocation_to_sinking_funds') as batch_op:
        batch_op.alter_column('income_allocation_id', new_column_name='salary_allocation_id')

    with op.batch_alter_table('income_allocations') as batch_op:
        batch_op.alter_column('monthly_income_amount', new_column_name='monthly_salary_amount')

    # Revert table renames
    op.rename_table('income_allocations', 'salary_allocations')
    op.rename_table('income_allocation_to_sinking_funds', 'salary_allocation_to_sinking_funds')
