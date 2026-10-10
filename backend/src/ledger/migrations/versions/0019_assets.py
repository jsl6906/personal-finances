"""asset valuation settings for property/vehicle accounts

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-10 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"


def upgrade() -> None:
    op.create_table(
        "asset",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=20), server_default="manual", nullable=False),
        sa.Column("loan_account_id", sa.Integer(), nullable=True),
        sa.Column("purchase_date", sa.Date(), nullable=True),
        sa.Column("purchase_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("depreciation_rate", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("last_valued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_result", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], [f"{S}.account.id"], name=op.f("fk_asset_account_id_account"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["loan_account_id"],
            [f"{S}.account.id"],
            name=op.f("fk_asset_loan_account_id_account"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_asset")),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("asset", schema=S)
