from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.filters import id_in
from ledger.models import MerchantRule, Transaction


async def learn_rule(session: AsyncSession, merchant: str | None, category_id: int | None) -> None:
    """Remember the user's latest category choice for a merchant; explicit user rules are never overwritten."""
    if not merchant or category_id is None:
        return
    stmt = insert(MerchantRule).values(merchant=merchant, category_id=category_id, source="learned", hits=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[MerchantRule.merchant],
        set_={"category_id": category_id, "hits": MerchantRule.hits + 1},
        where=MerchantRule.source == "learned",
    )
    await session.execute(stmt)


async def rule_category(session: AsyncSession, merchant: str | None) -> int | None:
    if not merchant:
        return None
    return await session.scalar(select(MerchantRule.category_id).where(MerchantRule.merchant == merchant))


async def apply_rules(session: AsyncSession, ids: list[int] | None = None) -> int:
    """Categorize uncategorized transactions whose merchant has a rule. Returns rows updated."""
    stmt = (
        update(Transaction)
        .where(
            Transaction.category_id.is_(None),
            Transaction.deleted_at.is_(None),
            Transaction.merchant == MerchantRule.merchant,
        )
        .values(
            category_id=MerchantRule.category_id,
            category_source="rule",
            suggested_category_id=None,
            suggestion_confidence=None,
            suggestion_reason=None,
        )
    )
    if ids:
        stmt = stmt.where(id_in(Transaction.id, ids))
    result = await session.execute(stmt.execution_options(synchronize_session=False))
    return result.rowcount or 0
