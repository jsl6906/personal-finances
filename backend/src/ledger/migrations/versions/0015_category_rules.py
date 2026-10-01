"""category rules (replace merchant_rule)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01 12:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"


def upgrade() -> None:
    op.create_table('category_rule',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('match_type', sa.String(length=20), server_default='merchant', nullable=False),
    sa.Column('pattern', sa.String(length=300), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('amount_min', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('amount_max', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('priority', sa.Integer(), server_default='100', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('source', sa.String(length=20), server_default='user', nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], [f'{S}.account.id'], name=op.f('fk_category_rule_account_id_account'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['category_id'], [f'{S}.category.id'], name=op.f('fk_category_rule_category_id_category'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_category_rule')),
    schema=S
    )
    op.create_index('ix_category_rule_match', 'category_rule', ['match_type', 'pattern'], unique=False, schema=S)
    op.create_index(op.f('ix_category_rule_category_id'), 'category_rule', ['category_id'], unique=False, schema=S)
    op.add_column('transaction', sa.Column('category_rule_id', sa.Integer(), nullable=True), schema=S)
    op.create_index(op.f('ix_transaction_category_rule_id'), 'transaction', ['category_rule_id'], unique=False, schema=S)
    op.create_foreign_key(
        op.f('fk_transaction_category_rule_id_category_rule'), 'transaction', 'category_rule',
        ['category_rule_id'], ['id'], source_schema=S, referent_schema=S, ondelete='SET NULL',
    )
    op.execute(f"""--sql
        INSERT INTO {S}.category_rule (match_type, pattern, category_id, source, created_at, updated_at)
        SELECT 'merchant', merchant, category_id, CASE WHEN source = 'user' THEN 'user' ELSE 'learned' END,
               created_at, updated_at
        FROM {S}.merchant_rule ORDER BY id""")
    op.execute(f"""--sql
        UPDATE {S}."transaction" t SET category_rule_id = r.id
        FROM {S}.category_rule r
        WHERE t.category_source = 'rule' AND r.match_type = 'merchant' AND r.pattern = t.merchant
          AND r.category_id = t.category_id""")
    op.drop_table('merchant_rule', schema=S)


def downgrade() -> None:
    op.create_table('merchant_rule',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('merchant', sa.String(length=200), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('hits', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], [f'{S}.category.id'], name=op.f('fk_merchant_rule_category_id_category'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_merchant_rule')),
    sa.UniqueConstraint('merchant', name=op.f('uq_merchant_rule_merchant')),
    schema=S
    )
    op.execute(f"""--sql
        INSERT INTO {S}.merchant_rule (merchant, category_id, source, created_at, updated_at)
        SELECT DISTINCT ON (pattern) left(pattern, 200), category_id, source, created_at, updated_at
        FROM {S}.category_rule
        WHERE match_type = 'merchant' AND account_id IS NULL AND amount_min IS NULL AND amount_max IS NULL
        ORDER BY pattern, priority, id DESC""")
    op.drop_constraint(op.f('fk_transaction_category_rule_id_category_rule'), 'transaction', schema=S, type_='foreignkey')
    op.drop_index(op.f('ix_transaction_category_rule_id'), table_name='transaction', schema=S)
    op.drop_column('transaction', 'category_rule_id', schema=S)
    op.drop_index(op.f('ix_category_rule_category_id'), table_name='category_rule', schema=S)
    op.drop_index('ix_category_rule_match', table_name='category_rule', schema=S)
    op.drop_table('category_rule', schema=S)
