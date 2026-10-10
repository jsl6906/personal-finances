"""Budget evaluation with spreading: a transaction may count across N months (per-transaction override or rule)."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models import Budget, Category, CategoryGroup, SpreadRule
from ledger.models.budgets import PERIOD_MONTHS


@dataclass
class Period:
    type: str
    start: date
    end: date

    @property
    def months(self) -> int:
        return PERIOD_MONTHS[self.type]

    @property
    def label(self) -> str:
        if self.type == "month":
            return self.start.strftime("%B %Y")
        if self.type == "quarter":
            return f"Q{(self.start.month - 1) // 3 + 1} {self.start.year}"
        return str(self.start.year)

    def elapsed(self, today: date) -> float:
        if today < self.start:
            return 0.0
        if today >= self.end:
            return 1.0
        return ((today - self.start).days + 1) / ((self.end - self.start).days + 1)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def period_for(kind: str, on: date) -> Period:
    if kind == "month":
        start = date(on.year, on.month, 1)
    elif kind == "quarter":
        start = date(on.year, (on.month - 1) // 3 * 3 + 1, 1)
    else:
        start = date(on.year, 1, 1)
    last = add_months(start, PERIOD_MONTHS[kind] - 1)
    return Period(kind, start, date(last.year, last.month, monthrange(last.year, last.month)[1]))


_SPREAD_ACTUALS = text(
    """--sql
    WITH tx AS (
        SELECT t.category_id, t.amount,
               CAST(date_trunc('month', t.txn_date) AS date) AS m0,
               COALESCE(t.budget_spread_months, (
                   SELECT r.months FROM spread_rule r
                   WHERE r.is_active
                     AND (r.category_id IS NULL OR r.category_id = t.category_id)
                     AND (r.merchant_pattern IS NULL OR t.description ILIKE '%' || r.merchant_pattern || '%')
                     AND (r.min_amount IS NULL OR abs(t.amount) >= r.min_amount)
                   ORDER BY r.id LIMIT 1
               ), 1) AS n
        FROM "transaction" t
        WHERE t.deleted_at IS NULL
          AND t.category_id IS NOT NULL
          AND t.txn_date >= CAST(:start AS date) - interval '60 months'
          AND t.txn_date <= CAST(:end AS date)
    )
    SELECT tx.category_id,
           sum(tx.amount / tx.n) AS net,
           sum(CASE WHEN tx.n > 1 THEN tx.amount / tx.n ELSE 0 END) AS spread_part
    FROM tx
    CROSS JOIN LATERAL generate_series(0, tx.n - 1) AS g(i)
    WHERE CAST(tx.m0 + make_interval(months => g.i) AS date) BETWEEN CAST(:start AS date) AND CAST(:end AS date)
    GROUP BY tx.category_id
    """
)


async def actuals(session: AsyncSession, start: date, end: date) -> dict[int, tuple[Decimal, Decimal]]:
    """category_id -> (net signed amount, portion coming from spread transactions) within [start, end]."""
    rows = await session.execute(_SPREAD_ACTUALS, {"start": start, "end": end})
    return {r.category_id: (Decimal(r.net), Decimal(r.spread_part)) for r in rows}


def _q(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"))


OVERALL_NAME = "All spending"


async def budget_status(session: AsyncSession, period: Period, today: date) -> dict:
    cats = {c.id: c for c in (await session.scalars(select(Category))).unique().all()}
    groups = {g.id: g for g in (await session.scalars(select(CategoryGroup))).all()}
    budgets = (await session.scalars(select(Budget))).all()
    act = await actuals(session, period.start, period.end)
    elapsed = period.elapsed(today)

    def in_period(b: Budget) -> Decimal:
        return _q(b.amount * period.months / PERIOD_MONTHS[b.period_type])

    cat_budgets = [b for b in budgets if b.category_id]
    group_budgets = {b.group_id: b for b in budgets if b.group_id}
    rows, covered = [], set()
    for b in budgets:
        # Wider budgets are shown net of the narrower budgets (and their spending) inside them.
        sub: list[Budget] = []
        if b.category_id:
            members = [b.category_id]
            c = cats[b.category_id]
            name, group, kind, scope = c.name, c.group.name, c.type, "category"
        elif b.group_id:
            members = [cid for cid, c in cats.items() if c.group_id == b.group_id]
            g = groups[b.group_id]
            sub = [x for x in cat_budgets if cats[x.category_id].group_id == b.group_id]
            name, group, kind, scope = (f"{g.name} · everything else" if sub else g.name), None, g.type, "group"
        else:
            members = [
                cid
                for cid, c in cats.items()
                if c.type == "expense" and not c.hide_from_reports and not c.group.hide_from_reports
            ]
            sub = [x for x in group_budgets.values() if groups[x.group_id].type == "expense"] + [
                x
                for x in cat_budgets
                if cats[x.category_id].type == "expense" and cats[x.category_id].group_id not in group_budgets
            ]
            name, group, kind, scope = ("Everything else" if sub else OVERALL_NAME), None, "expense", "overall"
        excluded = {x.category_id for x in sub if x.category_id}
        excluded.update(cid for cid, c in cats.items() if c.group_id in {x.group_id for x in sub if x.group_id})
        members = [m for m in members if m not in excluded]
        net = sum((act.get(c, (Decimal(0), Decimal(0)))[0] for c in members), Decimal(0))
        spread = sum((act.get(c, (Decimal(0), Decimal(0)))[1] for c in members), Decimal(0))
        actual = -net if kind != "income" else net
        sign = -1 if kind != "income" else 1
        by_cat = sorted(((sign * act[c][0], cats[c].name) for c in members if c in act), reverse=True)
        total_budget = in_period(b)
        allocated = sum((in_period(x) for x in sub), Decimal(0))
        budget = total_budget - allocated
        if kind == "expense":
            covered.update(members)
        # Spread shares are fixed for the whole period, so only the rest is extrapolated by pace.
        spread_actual = sign * spread
        projected = (
            _q(spread_actual + (actual - spread_actual) / Decimal(str(elapsed))) if 0 < elapsed < 1 else _q(actual)
        )
        rows.append(
            {
                "budget_id": b.id,
                "category_id": b.category_id,
                "group_id": b.group_id,
                "scope": scope,
                "name": name,
                "group": group,
                "category_ids": members,
                "category_names": [n for v, n in by_cat if v > 0] if scope != "category" else [],
                "kind": kind,
                "period_type": b.period_type,
                "base_amount": b.amount,
                "total_budget": total_budget,
                "allocated": allocated,
                "budget": budget,
                "actual": _q(actual),
                "left": _q(budget - actual),
                "pct": float(actual / budget * 100) if budget > 0 else 0.0,
                "projected": projected,
                "spread_amount": _q(abs(spread)),
                "notes": b.notes,
                "status": "over" if actual > budget else ("pace" if projected > budget else "ok"),
            }
        )
    rows.sort(key=lambda r: (r["kind"] != "expense", r["scope"] == "overall", -r["pct"]))

    expense_rows = [r for r in rows if r["kind"] == "expense"]
    spent_total = -sum((act.get(c, (Decimal(0), Decimal(0)))[0] for c in covered), Decimal(0))
    budget_total = sum((r["budget"] for r in expense_rows), Decimal(0))
    unbudgeted = []
    for cid, (net, _) in act.items():
        c = cats.get(cid)
        if c and c.type == "expense" and not c.hide_from_reports and cid not in covered and net < 0:
            unbudgeted.append({"category_id": cid, "name": c.name, "group": c.group.name, "actual": _q(-net)})
    unbudgeted.sort(key=lambda r: -r["actual"])
    return {
        "period": {
            "type": period.type,
            "start": period.start,
            "end": period.end,
            "label": period.label,
            "elapsed": round(elapsed, 4),
        },
        "rows": rows,
        "total": {
            "spent": _q(spent_total),
            "budget": _q(budget_total),
            "left": _q(budget_total - spent_total),
            "over_count": sum(1 for r in expense_rows if r["status"] == "over"),
            "count": len(expense_rows),
        },
        "unbudgeted": unbudgeted,
    }


async def suggest_budgets(session: AsyncSession, today: date, months: int = 12) -> list[dict]:
    """Monthly budget suggestions from the trailing full months' average spend, rounded up to $10."""
    end_month = date(today.year, today.month, 1)
    start = add_months(end_month, -months)
    end = end_month - timedelta(days=1)
    act = await actuals(session, start, end)
    cats = {c.id: c for c in (await session.scalars(select(Category))).unique().all()}
    budgeted = set((await session.scalars(select(Budget.category_id).where(Budget.category_id.is_not(None)))).all())
    out = []
    for cid, (net, _) in act.items():
        c = cats.get(cid)
        if not c or c.type != "expense" or c.hide_from_reports or cid in budgeted or net >= 0:
            continue
        avg = -net / months
        if avg < 10:
            continue
        rounded = (avg / 10).to_integral_value(rounding=ROUND_CEILING) * 10
        out.append(
            {"category_id": cid, "name": c.name, "group": c.group.name, "monthly_average": _q(avg), "suggested": rounded}
        )
    out.sort(key=lambda r: -r["monthly_average"])
    return out


async def rule_matches(session: AsyncSession, rule: SpreadRule, since: date) -> tuple[int, Decimal]:
    row = (
        await session.execute(
            text(
                """--sql
                SELECT count(*) AS n, coalesce(sum(abs(t.amount)), 0) AS total
                FROM "transaction" t
                WHERE t.deleted_at IS NULL AND t.txn_date >= :since AND t.budget_spread_months IS NULL
                  AND (CAST(:cat AS integer) IS NULL OR t.category_id = CAST(:cat AS integer))
                  AND (CAST(:pat AS text) IS NULL OR t.description ILIKE '%' || CAST(:pat AS text) || '%')
                  AND (CAST(:min AS numeric) IS NULL OR abs(t.amount) >= CAST(:min AS numeric))
                """
            ),
            {"since": since, "cat": rule.category_id, "pat": rule.merchant_pattern, "min": rule.min_amount},
        )
    ).one()
    return row.n, Decimal(row.total)
