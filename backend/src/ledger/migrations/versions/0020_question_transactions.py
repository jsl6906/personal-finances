"""transactions an emailed question is about, so replies can be filed on them

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-10 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"


def upgrade() -> None:
    op.add_column(
        "alert_event",
        sa.Column("transaction_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        schema=S,
    )


def downgrade() -> None:
    op.drop_column("alert_event", "transaction_ids", schema=S)
