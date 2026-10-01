"""merchant profiles

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30 18:59:14.918744

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('merchant_profile',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('alias_of', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_merchant_profile')),
    sa.UniqueConstraint('key', name=op.f('uq_merchant_profile_key')),
    schema='personal_finances'
    )
    op.create_index(op.f('ix_merchant_profile_alias_of'), 'merchant_profile', ['alias_of'], unique=False, schema='personal_finances')
    op.add_column('transaction', sa.Column('merchant_source', sa.String(length=10), nullable=True), schema='personal_finances')


def downgrade() -> None:
    op.drop_column('transaction', 'merchant_source', schema='personal_finances')
    op.drop_index(op.f('ix_merchant_profile_alias_of'), table_name='merchant_profile', schema='personal_finances')
    op.drop_table('merchant_profile', schema='personal_finances')
