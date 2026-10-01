"""transfer matching

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('transaction', sa.Column('transfer_match_id', sa.BigInteger(), nullable=True), schema='personal_finances')
    op.create_index(op.f('ix_transaction_transfer_match_id'), 'transaction', ['transfer_match_id'], unique=False, schema='personal_finances')
    op.create_foreign_key(op.f('fk_transaction_transfer_match_id_transaction'), 'transaction', 'transaction', ['transfer_match_id'], ['id'], source_schema='personal_finances', referent_schema='personal_finances', ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(op.f('fk_transaction_transfer_match_id_transaction'), 'transaction', schema='personal_finances', type_='foreignkey')
    op.drop_index(op.f('ix_transaction_transfer_match_id'), table_name='transaction', schema='personal_finances')
    op.drop_column('transaction', 'transfer_match_id', schema='personal_finances')
