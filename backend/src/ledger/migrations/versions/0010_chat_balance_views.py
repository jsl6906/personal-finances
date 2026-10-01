"""chat views for balances and holdings

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"
VIEWS = {
    "v_balances": f"""
        SELECT a.name AS account, a.account_type, i.name AS institution, b.as_of, b.balance, b.available, b.source
        FROM {S}.account_balance b
        JOIN {S}.account a ON a.id = b.account_id
        LEFT JOIN {S}.institution i ON i.id = a.institution_id""",
    "v_holdings": f"""
        SELECT a.name AS account, h.as_of, h.symbol, h.description, h.shares, h.market_value, h.cost_basis,
               h.currency, h.source
        FROM {S}.holding h
        JOIN {S}.account a ON a.id = h.account_id""",
}


def upgrade() -> None:
    for name, sql in VIEWS.items():
        op.execute(f"CREATE OR REPLACE VIEW {S}.{name} AS {sql}")
        op.execute(f"GRANT SELECT ON {S}.{name} TO pf_readonly")


def downgrade() -> None:
    for name in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {S}.{name}")
