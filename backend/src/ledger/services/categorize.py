from decimal import Decimal
from typing import Protocol

from sqlalchemy import and_, case, false, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ledger.db.filters import id_in
from ledger.models import CategoryRule, Transaction


class RuleSpec(Protocol):
    match_type: str
    pattern: str
    account_id: int | None
    amount_min: Decimal | None
    amount_max: Decimal | None


def _contains(pattern, desc, orig):
    p = func.lower(pattern)
    return or_(func.strpos(func.lower(desc), p) > 0, func.strpos(func.lower(func.coalesce(orig, "")), p) > 0)


def _regex(pattern, desc, orig):
    return or_(desc.op("~*", is_comparison=True)(pattern), func.coalesce(orig, "").op("~*", is_comparison=True)(pattern))


def rule_condition(r: RuleSpec, T=Transaction):
    """WHERE clause selecting the transactions a single (known) rule matches."""
    if r.match_type == "merchant":
        conds = [T.merchant == r.pattern]
    elif r.match_type == "contains":
        conds = [_contains(r.pattern, T.description, T.original_description)]
    else:
        conds = [_regex(r.pattern, T.description, T.original_description)]
    if r.account_id is not None:
        conds.append(T.account_id == r.account_id)
    if r.amount_min is not None:
        conds.append(func.abs(T.amount) >= r.amount_min)
    if r.amount_max is not None:
        conds.append(func.abs(T.amount) <= r.amount_max)
    return and_(*conds)


def _sql_match(R, T):
    # CASE keeps Postgres from evaluating a merchant/contains pattern as a regex.
    text_match = case(
        (R.match_type == "merchant", T.merchant == R.pattern),
        (R.match_type == "contains", _contains(R.pattern, T.description, T.original_description)),
        (R.match_type == "regex", _regex(R.pattern, T.description, T.original_description)),
        else_=false(),
    )
    return and_(
        text_match,
        or_(R.account_id.is_(None), R.account_id == T.account_id),
        or_(R.amount_min.is_(None), func.abs(T.amount) >= R.amount_min),
        or_(R.amount_max.is_(None), func.abs(T.amount) <= R.amount_max),
    )


PRECEDENCE = (CategoryRule.priority, CategoryRule.id.desc())


async def apply_rules(session: AsyncSession, ids: list[int] | None = None) -> int:
    """Categorize uncategorized transactions with the first matching active rule. Returns rows updated."""
    T = aliased(Transaction)
    best = (
        select(CategoryRule.id, CategoryRule.category_id)
        .where(CategoryRule.is_active, _sql_match(CategoryRule, T))
        .order_by(*PRECEDENCE)
        .limit(1)
        .lateral("best")
    )
    pick = (
        select(T.id.label("txn_id"), best.c.id.label("rule_id"), best.c.category_id.label("cat"))
        .join(best, true())
        .where(T.category_id.is_(None), T.deleted_at.is_(None))
    )
    if ids:
        pick = pick.where(id_in(T.id, ids))
    pick = pick.subquery()
    stmt = (
        update(Transaction)
        .where(Transaction.id == pick.c.txn_id)
        .values(
            category_id=pick.c.cat,
            category_rule_id=pick.c.rule_id,
            category_source="rule",
            suggested_category_id=None,
            suggestion_confidence=None,
            suggestion_reason=None,
        )
    )
    result = await session.execute(stmt.execution_options(synchronize_session=False))
    return result.rowcount or 0


async def matching_rules(session: AsyncSession, txn_id: int) -> list[CategoryRule]:
    """Every rule (active or not) that matches a transaction, in precedence order."""
    stmt = (
        select(CategoryRule)
        .join(Transaction, _sql_match(CategoryRule, Transaction))
        .where(Transaction.id == txn_id)
        .order_by(CategoryRule.is_active.desc(), *PRECEDENCE)
    )
    return list((await session.scalars(stmt)).all())


async def apply_rule(session: AsyncSession, rule: CategoryRule, include_user: bool = False) -> int:
    """Point every matching transaction at `rule`'s category; hand-set categories only when include_user."""
    conds = [
        Transaction.deleted_at.is_(None),
        rule_condition(rule),
        or_(Transaction.category_id.is_(None), Transaction.category_id != rule.category_id),
    ]
    if not include_user:
        conds.append(func.coalesce(Transaction.category_source, "") != "user")
    res = await session.execute(
        update(Transaction)
        .where(*conds)
        .values(
            category_id=rule.category_id,
            category_rule_id=rule.id,
            category_source="rule",
            suggested_category_id=None,
            suggestion_confidence=None,
            suggestion_reason=None,
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount or 0


async def learn_rule(session: AsyncSession, merchant: str | None, category_id: int | None, source: str = "ai") -> None:
    """Remember an accepted category for a merchant; rules the user created or edited are never overwritten."""
    if not merchant or category_id is None:
        return
    r = await session.scalar(
        select(CategoryRule)
        .where(
            CategoryRule.match_type == "merchant",
            CategoryRule.pattern == merchant,
            CategoryRule.account_id.is_(None),
            CategoryRule.amount_min.is_(None),
            CategoryRule.amount_max.is_(None),
        )
        .order_by(CategoryRule.id)
        .limit(1)
    )
    if r is None:
        session.add(CategoryRule(match_type="merchant", pattern=merchant, category_id=category_id, source=source))
        await session.flush()
    elif r.source != "user":
        r.category_id, r.source = category_id, source


def describe(r: RuleSpec) -> str:
    what = {"merchant": "Merchant is", "contains": "Description contains", "regex": "Description matches"}
    parts = [f"{what.get(r.match_type, r.match_type)} “{r.pattern}”"]
    if r.amount_min is not None and r.amount_max is not None:
        parts.append(f"amount ${r.amount_min:,.2f}–${r.amount_max:,.2f}")
    elif r.amount_min is not None:
        parts.append(f"amount ≥ ${r.amount_min:,.2f}")
    elif r.amount_max is not None:
        parts.append(f"amount ≤ ${r.amount_max:,.2f}")
    return " · ".join(parts)
