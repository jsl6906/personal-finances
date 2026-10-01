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
    session: AsyncSession,
    start: date,
    end: date,
    level: str = "group",
    top: int = 6,
    ids: list[int] | None = None,
    kind: str = "expense",
    other: bool = False,
) -> dict:
    """Monthly totals per group/category for the top series; `other` adds the remainder as an "Other" series."""
    key, label = (
        ("g.id", "coalesce(g.name, 'Uncategorized')") if level == "group" else ("c.id", "coalesce(c.name, 'Uncategorized')")
    )
    measure, sign = (INCOME, ">") if kind == "income" else (EXPENSE, "<")
    sql = f"""--sql
        SELECT {key} AS id, {label} AS name, CAST(date_trunc('month', t.txn_date) AS date) AS month,
               sum({measure}) AS spent
        {FROM}
        WHERE {REPORTABLE} AND t.txn_date BETWEEN :start AND :end
          AND (c.type = :kind OR (c.id IS NULL AND t.amount {sign} 0))
        GROUP BY 1, 2, 3
    """
    totals: dict = {}
    cells: dict = {}
    by_month: dict = {}
    for r in await session.execute(text(sql), {"start": start, "end": end, "kind": kind}):
        totals.setdefault((r.id, r.name), 0.0)
        totals[(r.id, r.name)] += _f(r.spent)
        cells[(r.id, r.month)] = _f(r.spent)
        by_month[r.month] = by_month.get(r.month, 0.0) + _f(r.spent)
    chosen = [k for k in totals if not ids or k[0] in ids]
    chosen.sort(key=lambda k: -totals[k])
    chosen = chosen[:top] if not ids else chosen
    months, m = [], date(start.year, start.month, 1)
    while m <= end:
        months.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    series = [
        {"id": k[0], "name": k[1], "total": totals[k], "values": [cells.get((k[0], mo), 0.0) for mo in months]}
        for k in chosen
    ]
    if other:
        rest = [round(by_month.get(mo, 0.0) - sum(s["values"][i] for s in series), 2) for i, mo in enumerate(months)]
        if any(rest):
            series.append({"id": None, "name": "Other", "total": sum(rest), "values": rest})
    return {"months": months, "series": series}


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


async def contributors(
    session: AsyncSession,
    start: date,
    end: date,
    *,
    basis: str = "reports",
    kind: str | None = None,
    level: str = "category",
    ids: list[int] | None = None,
    exclude: list[int] | None = None,
    account_id: int | None = None,
    merchant: str | None = None,
    limit: int = 8,
) -> dict:
    """Top merchants and largest transactions behind one aggregate (a bar, a segment, a month).

    basis "reports" applies the Reports filters (no transfers/hidden); "all" matches the detail pages. In ids/exclude,
    0 stands for Uncategorized.
    """
    col = "coalesce(g.id, 0)" if level == "group" else "coalesce(c.id, 0)"
    conds = [REPORTABLE if basis == "reports" else "t.deleted_at IS NULL", "t.txn_date BETWEEN :start AND :end"]
    params: dict = {"start": start, "end": end, "limit": limit}
    if kind:
        conds.append(f"(c.type = :kind OR (c.id IS NULL AND t.amount {'>' if kind == 'income' else '<'} 0))")
        params["kind"] = kind
    if ids:
        conds.append(f"{col} = ANY(:ids)")
        params["ids"] = ids
    if exclude:
        conds.append(f"NOT ({col} = ANY(:exclude))")
        params["exclude"] = exclude
    if account_id:
        conds.append("t.account_id = :account_id")
        params["account_id"] = account_id
    if merchant:
        conds.append("coalesce(t.merchant, lower(t.description)) = :merchant")
        params["merchant"] = merchant
    where = " AND ".join(conds)
    from_ = f"{FROM} LEFT JOIN merchant_profile mp ON mp.key = t.merchant"
    out_, in_ = "CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END", "CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END"
    total = (
        await session.execute(text(f"SELECT count(*) AS n, sum({out_}) AS o, sum({in_}) AS i {from_} WHERE {where}"), params)
    ).one()
    merch_sql = f"""--sql
        SELECT coalesce(t.merchant, lower(t.description)) AS key,
               coalesce(max(mp.display_name), min(t.description)) AS name,
               count(*) AS n, sum({out_}) AS o, sum({in_}) AS i
        {from_}
        WHERE {where}
        GROUP BY 1 ORDER BY abs(sum(t.amount)) DESC, count(*) DESC LIMIT :limit
    """
    txn_sql = f"""--sql
        SELECT t.id, t.txn_date, t.description, t.amount, c.name AS category
        {from_}
        WHERE {where}
        ORDER BY abs(t.amount) DESC, t.txn_date DESC LIMIT :limit
    """
    return {
        "count": total.n,
        "out": _f(total.o),
        "in": _f(total.i),
        "merchants": [
            {"key": r.key, "name": r.name, "count": r.n, "out": _f(r.o), "in": _f(r.i)}
            for r in await session.execute(text(merch_sql), params)
        ],
        "transactions": [
            {"id": r.id, "date": r.txn_date, "description": r.description, "amount": _f(r.amount), "category": r.category}
            for r in await session.execute(text(txn_sql), params)
        ],
    }
