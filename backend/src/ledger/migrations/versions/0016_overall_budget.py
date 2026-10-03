"""overall spending budget (budget with no category or group)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-02 12:00:00

"""
from collections.abc import Sequence

from alembic import op

revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "personal_finances"

V_BUDGETS_NEW = f"""
    SELECT coalesce(c.name, g.name, CAST('All spending' AS varchar(100))) AS target,
           CASE WHEN b.category_id IS NOT NULL THEN 'category' WHEN b.group_id IS NOT NULL THEN 'group' ELSE 'overall' END
               AS target_type, b.period_type, b.amount, b.notes
    FROM {S}.budget b
    LEFT JOIN {S}.category c ON c.id = b.category_id
    LEFT JOIN {S}.category_group g ON g.id = b.group_id"""

V_BUDGETS_OLD = f"""
    SELECT coalesce(c.name, g.name) AS target, CASE WHEN b.category_id IS NULL THEN 'group' ELSE 'category' END
               AS target_type, b.period_type, b.amount, b.notes
    FROM {S}.budget b
    LEFT JOIN {S}.category c ON c.id = b.category_id
    LEFT JOIN {S}.category_group g ON g.id = b.group_id"""


def upgrade() -> None:
    op.drop_constraint(op.f('ck_budget_one_target'), 'budget', schema=S, type_='check')
    op.create_check_constraint(
        op.f('ck_budget_one_target'), 'budget', 'NOT (category_id IS NOT NULL AND group_id IS NOT NULL)', schema=S
    )
    op.execute(
        f"CREATE UNIQUE INDEX uq_budget_overall ON {S}.budget ((true)) WHERE category_id IS NULL AND group_id IS NULL"
    )
    op.execute(f"CREATE OR REPLACE VIEW {S}.v_budgets AS {V_BUDGETS_NEW}")
    op.execute(f"GRANT SELECT ON {S}.v_budgets TO pf_readonly")


def downgrade() -> None:
    op.execute(f"DELETE FROM {S}.budget WHERE category_id IS NULL AND group_id IS NULL")
    op.execute(f"DROP VIEW IF EXISTS {S}.v_budgets")
    op.execute(f"CREATE VIEW {S}.v_budgets AS {V_BUDGETS_OLD}")
    op.execute(f"GRANT SELECT ON {S}.v_budgets TO pf_readonly")
    op.execute(f"DROP INDEX IF EXISTS {S}.uq_budget_overall")
    op.drop_constraint(op.f('ck_budget_one_target'), 'budget', schema=S, type_='check')
    op.create_check_constraint(
        op.f('ck_budget_one_target'), 'budget', '(category_id IS NULL) <> (group_id IS NULL)', schema=S
    )
