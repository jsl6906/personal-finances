"""Detail-page endpoints: one transaction in context, and merchant / account / category / group drill-downs."""

from datetime import date
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics import entity
from ledger.db.engine import get_session
from ledger.models import (
    Account,
    Budget,
    Category,
    CategoryGroup,
    CategoryRule,
    MerchantProfile,
    SpreadRule,
    StatementSeries,
    Transaction,
)
from ledger.models.budgets import PERIOD_MONTHS
from ledger.services import merchants

router = APIRouter(tags=["details"])


async def _merchant_name(session: AsyncSession, key: str) -> str:
    return (await merchants.display_names(session, [key])).get(key) or merchants.default_name(key)


def _budget(b: Budget | None) -> dict | None:
    if b is None:
        return None
    return {
        "id": b.id,
        "amount": float(b.amount),
        "period_type": b.period_type,
        "monthly": float(b.amount) / PERIOD_MONTHS[b.period_type],
    }


async def _common(session: AsyncSession, scope: str, key, start: date | None, end: date | None) -> dict:
    if start and end and start > end:
        raise HTTPException(422, "start must be before end")
    st = await entity.stats(session, scope, key)
    s, e = entity.resolve_range(st["first_date"], start, end)
    return {
        "start": s,
        "end": e,
        "stats": st,
        "monthly": await entity.monthly(session, scope, key, s, e),
        "yearly": await entity.yearly(session, scope, key),
    }


def _brief(t: Transaction) -> dict:
    return {
        "id": t.id,
        "txn_date": t.txn_date,
        "description": t.description,
        "amount": float(t.amount),
        "account_id": t.account_id,
        "account_name": t.account.name if t.account else None,
    }


@router.get("/transactions/{txn_id}/context")
async def transaction_context(txn_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Transaction, txn_id)
    if t is None or t.deleted_at is not None:
        raise HTTPException(404, "Transaction not found")
    key = t.merchant or t.description.lower()
    rows = await entity.charges(session, "merchant", key)
    amount = float(t.amount)
    same = sorted(abs(r["amount"]) for r in rows if (r["amount"] < 0) == (amount < 0) and r["amount"] != 0)
    comparison = None
    if len(same) > 1:
        med = median(same)
        comparison = {
            "count": len(same),
            "median": med,
            "min": same[0],
            "max": same[-1],
            "diff_pct": (abs(amount) - med) / med if med else None,
            "rank_pct": sum(1 for v in same if v <= abs(amount)) / len(same),
        }
    match = await session.get(Transaction, t.transfer_match_id) if t.transfer_match_id else None
    same_day = (
        await session.scalars(
            select(Transaction)
            .where(
                Transaction.deleted_at.is_(None),
                Transaction.txn_date == t.txn_date,
                Transaction.account_id == t.account_id,
                Transaction.id != t.id,
            )
            .order_by(Transaction.amount)
            .limit(10)
        )
    ).all() if t.account_id else []
    return {
        "merchant": key,
        "merchant_name": await _merchant_name(session, key),
        "stats": await entity.stats(session, "merchant", key),
        "charges": rows,
        "cadence": entity.cadence(rows),
        "comparison": comparison,
        "transfer_match": _brief(match) if match and match.deleted_at is None else None,
        "same_day": [_brief(x) for x in same_day],
    }


@router.get("/merchants/detail")
async def merchant_detail(
    key: str = Query(min_length=1, max_length=200),
    start: date | None = None,
    end: date | None = None,
    session: AsyncSession = Depends(get_session),
):
    key = await merchants.resolve(session, key)
    out = await _common(session, "merchant", key, start, end)
    if not out["stats"]["count"]:
        raise HTTPException(404, "Merchant not found")
    rows = await entity.charges(session, "merchant", key)
    rule = (
        await session.execute(
            select(CategoryRule, Category.name)
            .join(Category, Category.id == CategoryRule.category_id)
            .where(CategoryRule.match_type == "merchant", CategoryRule.pattern == key, CategoryRule.is_active)
            .order_by(CategoryRule.priority, CategoryRule.id.desc())
            .limit(1)
        )
    ).first()
    rule_out = None
    if rule:
        r, cat = rule
        rule_out = {"id": r.id, "category_id": r.category_id, "category_name": cat, "source": r.source}
    profile = await session.scalar(select(MerchantProfile).where(MerchantProfile.key == key))
    aliases = (
        await session.scalars(
            select(MerchantProfile.key).where(MerchantProfile.alias_of == key).order_by(MerchantProfile.key)
        )
    ).all()
    key_expr = func.coalesce(Transaction.merchant, func.lower(Transaction.description))
    descriptions = [
        {"description": d, "count": n, "last_date": last}
        for d, n, last in await session.execute(
            select(Transaction.description, func.count(), func.max(Transaction.txn_date))
            .where(Transaction.deleted_at.is_(None), key_expr == key)
            .group_by(Transaction.description)
            .order_by(func.count().desc(), func.max(Transaction.txn_date).desc())
            .limit(15)
        )
    ]
    overridden = await session.scalar(
        select(func.count()).where(
            Transaction.merchant == key, Transaction.merchant_source == "user", Transaction.deleted_at.is_(None)
        )
    )
    return out | {
        "key": key,
        "name": (profile.display_name if profile else None) or merchants.default_name(key),
        "display_name": profile.display_name if profile else None,
        "aliases": aliases,
        "descriptions": descriptions,
        "overridden": overridden or 0,
        "latest_description": rows[-1]["description"],
        "charges": rows,
        "cadence": entity.cadence(rows),
        "categories": await entity.breakdown(session, "merchant", key, "category", out["start"], out["end"]),
        "accounts": await entity.breakdown(session, "merchant", key, "account", out["start"], out["end"]),
        "rule": rule_out,
    }


@router.get("/merchants/search")
async def merchant_search(
    q: str = Query("", max_length=100), limit: int = Query(20, ge=1, le=100), session: AsyncSession = Depends(get_session)
):
    sql = text(
        """--sql
        SELECT coalesce(t.merchant, lower(t.description)) AS key, max(mp.display_name) AS display_name,
               count(*) AS n, max(t.txn_date) AS last_date
        FROM "transaction" t
        LEFT JOIN merchant_profile mp ON mp.key = t.merchant
        WHERE t.deleted_at IS NULL
          AND (coalesce(t.merchant, lower(t.description)) ILIKE :q OR mp.display_name ILIKE :q)
        GROUP BY 1 ORDER BY n DESC LIMIT :limit
        """
    )
    like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return [
        {"key": r.key, "name": r.display_name or merchants.default_name(r.key), "count": r.n, "last_date": r.last_date}
        for r in await session.execute(sql, {"q": like, "limit": limit})
    ]


class MerchantNameIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(None, max_length=200)


class MergeIn(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)


class KeyIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


async def _merchant_op(session: AsyncSession, op):
    try:
        result = await op
    except merchants.MerchantError as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from None
    await session.commit()
    return result


@router.put("/merchants/name")
async def rename_merchant(body: MerchantNameIn, session: AsyncSession = Depends(get_session)):
    await _merchant_op(session, merchants.rename(session, body.key, body.display_name))
    return {"key": body.key, "name": await _merchant_name(session, body.key)}


@router.post("/merchants/merge")
async def merge_merchants(body: MergeIn, session: AsyncSession = Depends(get_session)):
    moved = await _merchant_op(session, merchants.merge(session, body.source, body.target))
    return {"key": await merchants.resolve(session, body.target), "moved": moved}


@router.post("/merchants/unmerge")
async def unmerge_merchant(body: KeyIn, session: AsyncSession = Depends(get_session)):
    moved = await _merchant_op(session, merchants.unmerge(session, body.key))
    return {"key": body.key, "moved": moved}


@router.get("/accounts/{account_id}/detail")
async def account_detail(
    account_id: int, start: date | None = None, end: date | None = None, session: AsyncSession = Depends(get_session)
):
    a = await session.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Account not found")
    out = await _common(session, "account", account_id, start, end)
    bal_sql = text(
        """--sql
        SELECT DISTINCT ON (as_of) as_of, balance, available FROM account_balance
        WHERE account_id = :a AND as_of BETWEEN :start AND :end ORDER BY as_of, created_at DESC
        """
    )
    history = [
        {"as_of": r.as_of, "balance": float(r.balance)}
        for r in await session.execute(bal_sql, {"a": account_id, "start": out["start"], "end": out["end"]})
    ]
    latest = (
        await session.execute(
            text(
                """--sql
                SELECT as_of, balance, available, source FROM account_balance
                WHERE account_id = :a ORDER BY as_of DESC, created_at DESC LIMIT 1
                """
            ),
            {"a": account_id},
        )
    ).first()
    holdings = [
        dict(r)
        for r in (
            await session.execute(
                text(
                    """--sql
                    SELECT symbol, description, shares, market_value, cost_basis, as_of FROM holding
                    WHERE account_id = :a AND as_of = (SELECT max(as_of) FROM holding WHERE account_id = :a)
                    ORDER BY market_value DESC NULLS LAST
                    """
                ),
                {"a": account_id},
            )
        ).mappings()
    ]
    for h in holdings:
        for k in ("shares", "market_value", "cost_basis"):
            h[k] = float(h[k]) if h[k] is not None else None
    return out | {
        "account": {
            "id": a.id,
            "name": a.name,
            "institution_name": a.institution.name if a.institution else None,
            "account_type": a.account_type,
            "mask": a.mask,
            "is_hidden": a.is_hidden,
            "is_closed": a.is_closed,
            "notes": a.notes,
            "sources": sorted(a.external_refs or {}),
        },
        "balance": {
            "as_of": latest.as_of,
            "balance": float(latest.balance),
            "available": float(latest.available) if latest.available is not None else None,
            "source": latest.source,
        }
        if latest
        else None,
        "balance_history": history,
        "holdings": holdings,
        "categories": await entity.breakdown(session, "account", account_id, "category", out["start"], out["end"]),
        "merchants": await entity.breakdown(session, "account", account_id, "merchant", out["start"], out["end"]),
    }


async def _series_and_rules(session: AsyncSession, category_ids: list[int]) -> dict:
    series = (
        await session.execute(
            select(StatementSeries.id, StatementSeries.name).where(StatementSeries.category_id.in_(category_ids or [0]))
        )
    ).all()
    spread = (
        await session.execute(
            select(SpreadRule.id, SpreadRule.name, SpreadRule.months).where(
                SpreadRule.category_id.in_(category_ids or [0]), SpreadRule.is_active
            )
        )
    ).all()
    rules = await session.scalar(
        select(func.count()).select_from(CategoryRule).where(CategoryRule.category_id.in_(category_ids or [0]))
    )
    return {
        "bill_series": [{"id": i, "name": n} for i, n in series],
        "spread_rules": [{"id": i, "name": n, "months": m} for i, n, m in spread],
        "category_rules": rules or 0,
    }


@router.get("/categories/{category_id}/detail")
async def category_detail(
    category_id: int, start: date | None = None, end: date | None = None, session: AsyncSession = Depends(get_session)
):
    c = await session.get(Category, category_id)
    if c is None:
        raise HTTPException(404, "Category not found")
    out = await _common(session, "category", category_id, start, end)
    budget = await session.scalar(select(Budget).where(Budget.category_id == category_id))
    return (
        out
        | {
            "category": {
                "id": c.id,
                "name": c.name,
                "type": c.type,
                "group_id": c.group_id,
                "group_name": c.group.name,
                "description": c.description,
                "hide_from_reports": c.hide_from_reports or c.group.hide_from_reports,
                "is_active": c.is_active,
            },
            "budget": _budget(budget),
            "merchants": await entity.breakdown(session, "category", category_id, "merchant", out["start"], out["end"]),
            "accounts": await entity.breakdown(session, "category", category_id, "account", out["start"], out["end"]),
        }
        | await _series_and_rules(session, [category_id])
    )


@router.get("/category-groups/{group_id}/detail")
async def group_detail(
    group_id: int, start: date | None = None, end: date | None = None, session: AsyncSession = Depends(get_session)
):
    g = await session.get(CategoryGroup, group_id)
    if g is None:
        raise HTTPException(404, "Category group not found")
    out = await _common(session, "group", group_id, start, end)
    cats = (await session.scalars(select(Category).where(Category.group_id == group_id).order_by(Category.name))).all()
    ids = [c.id for c in cats]
    budgets = (await session.scalars(select(Budget).where(Budget.category_id.in_(ids or [0])))).all()
    by_cat = {b.category_id: _budget(b) for b in budgets}
    totals = {
        r["id"]: r
        for r in await entity.breakdown(session, "group", group_id, "category", out["start"], out["end"], limit=500)
    }
    return (
        out
        | {
            "group": {
                "id": g.id,
                "name": g.name,
                "type": g.type,
                "hide_from_reports": g.hide_from_reports,
            },
            "budget": _budget(await session.scalar(select(Budget).where(Budget.group_id == group_id))),
            "categories": [
                {
                    "id": c.id,
                    "name": c.name,
                    "is_active": c.is_active,
                    "budget": by_cat.get(c.id),
                    **{k: totals.get(c.id, {}).get(k, 0) for k in ("spent", "received", "net", "count")},
                    "last_date": totals.get(c.id, {}).get("last_date"),
                }
                for c in cats
            ],
            "trend": await entity.monthly_by(session, "group", group_id, "category", out["start"], out["end"]),
            "merchants": await entity.breakdown(session, "group", group_id, "merchant", out["start"], out["end"]),
            "accounts": await entity.breakdown(session, "group", group_id, "account", out["start"], out["end"]),
        }
        | await _series_and_rules(session, ids)
    )
