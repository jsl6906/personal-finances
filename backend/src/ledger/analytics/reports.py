"""Reporting queries: cash flow, category breakdowns and trends, merchants.

Reportable = not a transfer, not the uncategorized leg of a matched transfer, and not hidden. Other uncategorized
rows count as income/expense by sign.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

REPORTABLE = """
    t.deleted_at IS NULL
    AND coalesce(c.type, '') <> 'transfer'
    AND (t.transfer_match_id IS NULL OR c.id IS NOT NULL)
    AND NOT coalesce(c.hide_from_reports, false)
    AND NOT coalesce(g.hide_from_reports, false)
"""
FROM = """
    FROM "transaction" t
    LEFT JOIN category c ON c.id = t.category_id
    LEFT JOIN category_group g ON g.id = c.group_id
"""
INCOME = "CASE WHEN c.type = 'income' OR (c.id IS NULL AND t.amount > 0) THEN t.amount ELSE 0 END"
EXPENSE = "CASE WHEN c.type = 'expense' OR (c.id IS NULL AND t.amount < 0) THEN -t.amount ELSE 0 END"


def _f(v) -> float:
    return float(v or 0)


async def span(session: AsyncSession) -> dict:
    sql = f"""--sql
        SELECT min(t.txn_date) AS start, max(t.txn_date) AS end
        {FROM}
        WHERE {REPORTABLE}
    """
    r = (await session.execute(text(sql))).one()
    return {"start": r.start, "end": r.end}


async def cashflow(session: AsyncSession, start: date, end: date) -> list[dict]:
    sql = f"""--sql
        SELECT CAST(date_trunc('month', t.txn_date) AS date) AS month,
               sum({INCOME}) AS income, sum({EXPENSE}) AS expenses, count(*) AS n
        {FROM}
        WHERE {REPORTABLE} AND t.txn_date BETWEEN :start AND :end
        GROUP BY 1 ORDER BY 1
    """
    rows = {r.month: r for r in await session.execute(text(sql), {"start": start, "end": end})}
    out, m = [], date(start.year, start.month, 1)
    while m <= end:
        r = rows.get(m)
        inc, exp = _f(r.income if r else 0), _f(r.expenses if r else 0)
        out.append({"month": m, "income": inc, "expenses": exp, "net": inc - exp, "count": r.n if r else 0})
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


async def category_breakdown(session: AsyncSession, start: date, end: date) -> list[dict]:
    sql = f"""--sql
        SELECT c.id AS category_id, coalesce(c.name, 'Uncategorized') AS category,
               g.id AS group_id, coalesce(g.name, 'Uncategorized') AS grp,
               sum({EXPENSE}) AS spent, sum({INCOME}) AS income, count(*) AS n
        {FROM}
        WHERE {REPORTABLE} AND t.txn_date BETWEEN :start AND :end
        GROUP BY c.id, c.name, g.id, g.name
        HAVING sum({EXPENSE}) <> 0 OR sum({INCOME}) <> 0
        ORDER BY spent DESC
    """
    return [
        {
            "category_id": r.category_id,
            "category": r.category,
            "group_id": r.group_id,
            "group": r.grp,
            "spent": _f(r.spent),
            "income": _f(r.income),
            "count": r.n,
        }
        for r in await session.execute(text(sql), {"start": start, "end": end})
    ]


async def category_trend(
    session: AsyncSession, start: date, end: date, level: str = "group", top: int = 6, ids: list[int] | None = None
) -> dict:
    key, label = (
        ("g.id", "coalesce(g.name, 'Uncategorized')") if level == "group" else ("c.id", "coalesce(c.name, 'Uncategorized')")
    )
    sql = f"""--sql
        SELECT {key} AS id, {label} AS name, CAST(date_trunc('month', t.txn_date) AS date) AS month,
               sum({EXPENSE}) AS spent
        {FROM}
        WHERE {REPORTABLE} AND t.txn_date BETWEEN :start AND :end AND (c.type = 'expense' OR (c.id IS NULL AND t.amount < 0))
        GROUP BY 1, 2, 3
    """
    totals: dict = {}
    cells: dict = {}
    for r in await session.execute(text(sql), {"start": start, "end": end}):
        totals.setdefault((r.id, r.name), 0.0)
        totals[(r.id, r.name)] += _f(r.spent)
        cells[(r.id, r.month)] = _f(r.spent)
    chosen = [k for k in totals if not ids or k[0] in ids]
    chosen.sort(key=lambda k: -totals[k])
    chosen = chosen[:top] if not ids else chosen
    months, m = [], date(start.year, start.month, 1)
    while m <= end:
        months.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return {
        "months": months,
        "series": [
            {"id": k[0], "name": k[1], "total": totals[k], "values": [cells.get((k[0], mo), 0.0) for mo in months]}
            for k in chosen
        ],
    }


async def top_merchants(session: AsyncSession, start: date, end: date, limit: int = 15) -> list[dict]:
    sql = f"""--sql
        SELECT coalesce(t.merchant, lower(t.description)) AS merchant,
               coalesce(max(mp.display_name), min(t.description)) AS example,
               sum({EXPENSE}) AS spent, count(*) AS n, max(t.txn_date) AS last_date
        {FROM}
        LEFT JOIN merchant_profile mp ON mp.key = t.merchant
        WHERE {REPORTABLE} AND t.txn_date BETWEEN :start AND :end AND t.amount < 0
        GROUP BY 1 ORDER BY spent DESC LIMIT :limit
    """
    return [
        {"merchant": r.merchant, "example": r.example, "spent": _f(r.spent), "count": r.n, "last_date": r.last_date}
        for r in await session.execute(text(sql), {"start": start, "end": end, "limit": limit})
    ]


async def monthly_category_history(session: AsyncSession, start: date, end: date) -> dict[int, dict[date, Decimal]]:
    """Expense category -> month -> net spend, for anomaly baselines."""
    sql = f"""--sql
        SELECT c.id AS category_id, CAST(date_trunc('month', t.txn_date) AS date) AS month, sum(-t.amount) AS spent
        {FROM}
        WHERE {REPORTABLE} AND c.type = 'expense' AND t.txn_date BETWEEN :start AND :end
        GROUP BY 1, 2
    """
    out: dict[int, dict[date, Decimal]] = {}
    for r in await session.execute(text(sql), {"start": start, "end": end}):
        out.setdefault(r.category_id, {})[r.month] = Decimal(r.spent)
    return out
