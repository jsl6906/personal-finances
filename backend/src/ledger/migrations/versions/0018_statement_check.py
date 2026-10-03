"""statement coverage checks: statement rows vs ledger over the statement period

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-03 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"


def upgrade() -> None:
    op.create_table(
        "statement_check",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("import_batch_id", sa.BigInteger(), nullable=False),
        sa.Column("account_ref", sa.String(length=100), server_default="", nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("statement_total", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("ledger_total", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("difference", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("statement_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ledger_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("trusted", sa.Boolean(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            [f"{S}.import_batch.id"],
            name=op.f("fk_statement_check_import_batch_id_import_batch"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], [f"{S}.account.id"], name=op.f("fk_statement_check_account_id_account"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_check")),
        schema=S,
    )
    op.create_index(op.f("ix_statement_check_account_id"), "statement_check", ["account_id"], unique=False, schema=S)
    op.create_index(op.f("ix_statement_check_status"), "statement_check", ["status"], unique=False, schema=S)
    op.create_index(
        "uq_statement_check_batch_ref", "statement_check", ["import_batch_id", "account_ref"], unique=True, schema=S
    )


def downgrade() -> None:
    op.drop_index("uq_statement_check_batch_ref", table_name="statement_check", schema=S)
    op.drop_index(op.f("ix_statement_check_status"), table_name="statement_check", schema=S)
    op.drop_index(op.f("ix_statement_check_account_id"), table_name="statement_check", schema=S)
    op.drop_table("statement_check", schema=S)
