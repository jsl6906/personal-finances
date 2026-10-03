"""AI merchant review: merchant_profile.reviewed_at + merchant_suggestion

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-03 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"


def upgrade() -> None:
    op.add_column("merchant_profile", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True), schema=S)
    op.create_table(
        "merchant_suggestion",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("target_key", sa.String(length=200), nullable=False),
        sa.Column("source_keys", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchant_suggestion")),
        schema=S,
    )
    op.create_index(op.f("ix_merchant_suggestion_status"), "merchant_suggestion", ["status"], unique=False, schema=S)


def downgrade() -> None:
    op.drop_index(op.f("ix_merchant_suggestion_status"), table_name="merchant_suggestion", schema=S)
    op.drop_table("merchant_suggestion", schema=S)
    op.drop_column("merchant_profile", "reviewed_at", schema=S)
