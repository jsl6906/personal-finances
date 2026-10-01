"""transaction sources and notes

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30 20:37:59.752705

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"

BACKFILL = [
    # Rows that created a transaction.
    f"""--sql
    INSERT INTO {S}.transaction_source
        (transaction_id, role, import_batch_id, import_row_id, attachment_id, txn_date, description, amount, created_at)
    SELECT r.transaction_id, 'created', r.batch_id, r.id, b.attachment_id, r.txn_date, r.description, r.amount,
           coalesce(b.committed_at, b.created_at)
    FROM {S}.import_row r
    JOIN {S}.import_batch b ON b.id = r.batch_id
    WHERE r.transaction_id IS NOT NULL AND b.status = 'committed'
    ON CONFLICT DO NOTHING""",
    # Rows skipped as duplicates of an existing transaction.
    f"""--sql
    INSERT INTO {S}.transaction_source
        (transaction_id, role, import_batch_id, import_row_id, attachment_id, txn_date, description, amount,
         match_score, created_at)
    SELECT p.txn_a_id, 'matched', r.batch_id, r.id, b.attachment_id, r.txn_date, r.description, r.amount, p.score,
           coalesce(b.committed_at, b.created_at)
    FROM {S}.import_row r
    JOIN {S}.import_batch b ON b.id = r.batch_id
    JOIN {S}.duplicate_pair p ON p.import_row_id = r.id AND p.status = 'confirmed_duplicate'
    WHERE r.decision = 'skip_duplicate' AND b.status = 'committed'
    ON CONFLICT DO NOTHING""",
    # Row-specific notes on those duplicates (not the batch-wide default note).
    f"""--sql
    INSERT INTO {S}.transaction_note (transaction_id, body, source, import_batch_id, attachment_id, created_at, updated_at)
    SELECT DISTINCT ON (p.txn_a_id, r.notes) p.txn_a_id, r.notes, 'import', r.batch_id, b.attachment_id,
           coalesce(b.committed_at, b.created_at), coalesce(b.committed_at, b.created_at)
    FROM {S}.import_row r
    JOIN {S}.import_batch b ON b.id = r.batch_id
    JOIN {S}.duplicate_pair p ON p.import_row_id = r.id AND p.status = 'confirmed_duplicate'
    JOIN {S}."transaction" t ON t.id = p.txn_a_id
    WHERE r.decision = 'skip_duplicate' AND b.status = 'committed'
      AND nullif(trim(r.notes), '') IS NOT NULL
      AND r.notes IS DISTINCT FROM b.defaults->>'notes'
      AND position(r.notes IN coalesce(t.notes, '')) = 0""",
]

NOTES_VIEW = f"""
    SELECT n.transaction_id, t.txn_date AS date, t.description, t.amount, a.name AS account,
           n.body AS note, n.source, att.filename AS document, n.created_at
    FROM {S}.transaction_note n
    JOIN {S}."transaction" t ON t.id = n.transaction_id AND t.deleted_at IS NULL
    LEFT JOIN {S}.account a ON a.id = t.account_id
    LEFT JOIN {S}.attachment att ON att.id = n.attachment_id"""


def upgrade() -> None:
    op.create_table('transaction_note',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('transaction_id', sa.BigInteger(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=20), server_default='user', nullable=False),
    sa.Column('import_batch_id', sa.BigInteger(), nullable=True),
    sa.Column('attachment_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['attachment_id'], ['personal_finances.attachment.id'], name=op.f('fk_transaction_note_attachment_id_attachment'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['import_batch_id'], ['personal_finances.import_batch.id'], name=op.f('fk_transaction_note_import_batch_id_import_batch'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['transaction_id'], ['personal_finances.transaction.id'], name=op.f('fk_transaction_note_transaction_id_transaction'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_transaction_note')),
    schema='personal_finances'
    )
    op.create_index(op.f('ix_transaction_note_import_batch_id'), 'transaction_note', ['import_batch_id'], unique=False, schema='personal_finances')
    op.create_index(op.f('ix_transaction_note_transaction_id'), 'transaction_note', ['transaction_id'], unique=False, schema='personal_finances')
    op.create_table('transaction_source',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('transaction_id', sa.BigInteger(), nullable=False),
    sa.Column('role', sa.String(length=10), nullable=False),
    sa.Column('import_batch_id', sa.BigInteger(), nullable=True),
    sa.Column('import_row_id', sa.BigInteger(), nullable=True),
    sa.Column('attachment_id', sa.BigInteger(), nullable=True),
    sa.Column('txn_date', sa.Date(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('match_score', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['attachment_id'], ['personal_finances.attachment.id'], name=op.f('fk_transaction_source_attachment_id_attachment'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['import_batch_id'], ['personal_finances.import_batch.id'], name=op.f('fk_transaction_source_import_batch_id_import_batch'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['import_row_id'], ['personal_finances.import_row.id'], name=op.f('fk_transaction_source_import_row_id_import_row'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['transaction_id'], ['personal_finances.transaction.id'], name=op.f('fk_transaction_source_transaction_id_transaction'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_transaction_source')),
    schema='personal_finances'
    )
    op.create_index(op.f('ix_transaction_source_attachment_id'), 'transaction_source', ['attachment_id'], unique=False, schema='personal_finances')
    op.create_index(op.f('ix_transaction_source_import_batch_id'), 'transaction_source', ['import_batch_id'], unique=False, schema='personal_finances')
    op.create_index(op.f('ix_transaction_source_transaction_id'), 'transaction_source', ['transaction_id'], unique=False, schema='personal_finances')
    op.create_index('uq_transaction_source_row', 'transaction_source', ['transaction_id', 'import_row_id'], unique=True, schema='personal_finances', postgresql_where=sa.text('import_row_id IS NOT NULL'))

    for sql in BACKFILL:
        op.execute(sql)
    op.execute(f"CREATE OR REPLACE VIEW {S}.v_transaction_notes AS {NOTES_VIEW}")
    op.execute(f"GRANT SELECT ON {S}.v_transaction_notes TO pf_readonly")


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {S}.v_transaction_notes")
    op.drop_index('uq_transaction_source_row', table_name='transaction_source', schema='personal_finances', postgresql_where=sa.text('import_row_id IS NOT NULL'))
    op.drop_index(op.f('ix_transaction_source_transaction_id'), table_name='transaction_source', schema='personal_finances')
    op.drop_index(op.f('ix_transaction_source_import_batch_id'), table_name='transaction_source', schema='personal_finances')
    op.drop_index(op.f('ix_transaction_source_attachment_id'), table_name='transaction_source', schema='personal_finances')
    op.drop_table('transaction_source', schema='personal_finances')
    op.drop_index(op.f('ix_transaction_note_transaction_id'), table_name='transaction_note', schema='personal_finances')
    op.drop_index(op.f('ix_transaction_note_import_batch_id'), table_name='transaction_note', schema='personal_finances')
    op.drop_table('transaction_note', schema='personal_finances')
