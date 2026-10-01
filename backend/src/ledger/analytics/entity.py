"""Per-entity analytics for the detail pages (merchant, account, category, category group).

Detail pages show everything booked against the entity, including transfers and hidden categories; only deleted rows are
excluded. Amounts: `spent` = money out (positive), `received` = money in, `net` = signed sum.
"""

from datetime import date, timedelta
from statistics import median

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.budgets.service import add_months

MERCHANT_KEY = "coalesce(t.merchant, lower(t.description))"
SCOPES = {
    "merchant": f"{MERCHANT_KEY} = :key",
    "account": "t.account_id = :key",
    "category": "t.category_id = :key",
    "group": "c.group_id = :key",
}
FROM = """
    FROM "transaction" t
    LEFT JOIN category c ON c.id = t.category_id
    LEFT JOIN category_group g ON g.id = c.group_id
    LEFT JOIN account a ON a.id = t.account_id
    LEFT JOIN merchant_profile mp ON mp.key = t.merchant
"""
OUT = "CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END"
IN = "CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END"
BY = {
    "category": ("c.id", "coalesce(c.name, 'Uncategorized')"),
    "group": ("g.id", "coalesce(g.name, 'Uncategorized')"),
    "account": ("a.id", "coalesce(a.name, 'No account')"),
    "merchant": (MERCHANT_KEY, "coalesce(max(mp.display_name), (array_agg(t.description ORDER BY t.txn_date DESC))[1])"),
}
CADENCES = [
    ("weekly", 6, 8),
    ("every 2 weeks", 13, 16),
    ("monthly", 27, 33),
    ("every 2 months", 56, 65),
    ("quarterly", 85, 96),
    ("twice a year", 170, 195),
    ("yearly", 350, 380),
]


def _f(v) -> float:
    return float(v or 0)


def _where(scope: str) -> str:
    return f"t.deleted_at IS NULL AND {SCOPES[scope]}"


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def resolve_range(first: date | None, start: date | None, end: date | None) -> tuple[date, date]:
    today = date.today()
    end = end or add_months(month_start(today), 1) - timedelta(days=1)
    start = start or month_start(first or today)
    return min(start, end), end


def _months(start: date, end: date) -> list[date]:
    out, m = [], month_start(start)
    while m <= end:
        out.append(m)
        m = add_months(m, 1)
    return out


async def stats(session: AsyncSession, scope: str, key) -> dict:
    since = add_months(month_start(date.today()), -11)
    sql = f"""--sql
        SELECT count(*) AS n, min(t.txn_date) AS first_date, max(t.txn_date) AS last_date,
               sum({OUT}) AS spent, sum({IN}) AS received,
               count(*) FILTER (WHERE t.amount < 0) AS n_out, count(*) FILTER (WHERE t.amount > 0) AS n_in,
               avg(-t.amount) FILTER (WHERE t.amount < 0) AS avg_out,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY -t.amount) FILTER (WHERE t.amount < 0) AS median_out,
               max(-t.amount) FILTER (WHERE t.amount < 0) AS max_out,
               count(*) FILTER (WHERE t.txn_date >= :since) AS n_12m,
               coalesce(sum({OUT}) FILTER (WHERE t.txn_date >= :since), 0) AS spent_12m,
               coalesce(sum({IN}) FILTER (WHERE t.txn_date >= :since), 0) AS received_12m
        {FROM}
        WHERE {_where(scope)}
    """
    r = (await session.execute(text(sql), {"key": key, "since": since})).one()
    return {
        "count": r.n,
        "first_date": r.first_date,
        "last_date": r.last_date,
        "spent": _f(r.spent),
        "received": _f(r.received),
        "count_out": r.n_out,
        "count_in": r.n_in,
        "avg_out": _f(r.avg_out) if r.avg_out is not None else None,
        "median_out": _f(r.median_out) if r.median_out is not None else None,
        "max_out": _f(r.max_out) if r.max_out is not None else None,
        "count_12m": r.n_12m,
        "spent_12m": _f(r.spent_12m),
        "received_12m": _f(r.received_12m),
    }


async def monthly(session: AsyncSession, scope: str, key, start: date, end: date) -> list[dict]:
    sql = f"""--sql
        SELECT CAST(date_trunc('month', t.txn_date) AS date) AS month,
               sum({OUT}) AS spent, sum({IN}) AS received, count(*) AS n
        {FROM}
        WHERE {_where(scope)} AND t.txn_date BETWEEN :start AND :end
        GROUP BY 1
    """
    rows = {r.month: r for r in await session.execute(text(sql), {"key": key, "start": start, "end": end})}
    out = []
    for m in _months(start, end):
        r = rows.get(m)
        spent, received = _f(r.spent if r else 0), _f(r.received if r else 0)
        out.append({"month": m, "spent": spent, "received": received, "net": received - spent, "count": r.n if r else 0})
    return out


async def monthly_by(session: AsyncSession, scope: str, key, by: str, start: date, end: date) -> dict:
    """Monthly net outflow per sub-entity (e.g. categories within a group)."""
    ident, label = BY[by]
    sql = f"""--sql
        SELECT {ident} AS id, {label} AS name, CAST(date_trunc('month', t.txn_date) AS date) AS month,
               sum(t.amount) AS net
        {FROM}
        WHERE {_where(scope)} AND t.txn_date BETWEEN :start AND :end
        GROUP BY {ident}, 3
    """
    names: dict = {}
    cells: dict = {}
    for r in await session.execute(text(sql), {"key": key, "start": start, "end": end}):
        names.setdefault(r.id, r.name)
        cells[(r.id, r.month)] = _f(r.net)
    months = _months(start, end)
    series = [
        {"id": i, "name": n, "values": [cells.get((i, m), 0.0) for m in months]} for i, n in names.items()
    ]
    for s in series:
        s["total"] = sum(s["values"])
    series.sort(key=lambda s: abs(s["total"]), reverse=True)
    return {"months": months, "series": series}


async def yearly(session: AsyncSession, scope: str, key) -> list[dict]:
    sql = f"""--sql
        SELECT CAST(extract(year FROM t.txn_date) AS integer) AS year,
               sum({OUT}) AS spent, sum({IN}) AS received, count(*) AS n
        {FROM}
        WHERE {_where(scope)}
        GROUP BY 1 ORDER BY 1 DESC
    """
    return [
        {"year": r.year, "spent": _f(r.spent), "received": _f(r.received), "net": _f(r.received) - _f(r.spent), "count": r.n}
        for r in await session.execute(text(sql), {"key": key})
    ]


async def breakdown(
    session: AsyncSession, scope: str, key, by: str, start: date, end: date, limit: int = 12
) -> list[dict]:
    ident, label = BY[by]
    sql = f"""--sql
        SELECT {ident} AS id, {label} AS name, sum({OUT}) AS spent, sum({IN}) AS received, count(*) AS n,
               max(t.txn_date) AS last_date
        {FROM}
        WHERE {_where(scope)} AND t.txn_date BETWEEN :start AND :end
        GROUP BY {ident}
        ORDER BY greatest(sum({OUT}), sum({IN})) DESC
        LIMIT :limit
    """
    params = {"key": key, "start": start, "end": end, "limit": limit}
    return [
        {
            "id": r.id,
            "name": r.name,
            "spent": _f(r.spent),
            "received": _f(r.received),
            "net": _f(r.received) - _f(r.spent),
            "count": r.n,
            "last_date": r.last_date,
        }
        for r in await session.execute(text(sql), params)
    ]


async def charges(session: AsyncSession, scope: str, key, limit: int = 5000) -> list[dict]:
    """Individual transactions (newest `limit`), oldest first, for per-charge charts."""
    sql = f"""--sql
        SELECT * FROM (
            SELECT t.id, t.txn_date, t.amount, t.description, a.name AS account_name
            {FROM}
            WHERE {_where(scope)}
            ORDER BY t.txn_date DESC, t.id DESC
            LIMIT :limit
        ) x ORDER BY txn_date, id
    """
    return [
        {"id": r.id, "date": r.txn_date, "amount": _f(r.amount), "description": r.description, "account": r.account_name}
        for r in await session.execute(text(sql), {"key": key, "limit": limit})
    ]


def cadence(rows: list[dict], today: date | None = None) -> dict | None:
    """Detect a regular payment rhythm from charge dates (dominant direction only)."""
    outs = [r for r in rows if r["amount"] < 0]
    pick = outs if outs else [r for r in rows if r["amount"] > 0]
    dates = sorted({r["date"] for r in pick})[-13:]
    if len(dates) < 3:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    med = median(gaps)
    if med < 5 or median(abs(g - med) for g in gaps) > max(3, med * 0.2):
        return None
    label = next((name for name, lo, hi in CADENCES if lo <= med <= hi), f"every ~{round(med)} days")
    next_date = dates[-1] + timedelta(days=round(med))
    recent = [abs(r["amount"]) for r in pick if r["date"] in set(dates[-3:])]
    return {
        "label": label,
        "days": med,
        "next_date": next_date,
        "typical_amount": median(recent) if recent else None,
        "lapsed": (today or date.today()) > next_date + timedelta(days=max(5, round(med * 0.5))),
    }
