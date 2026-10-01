from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from ledger.ai.client import generate
from ledger.db.engine import get_sessionmaker
from ledger.db.filters import id_in
from ledger.jobs.worker import JobContext, job_handler
from ledger.models import Category, CategoryGroup, Transaction
from ledger.services.categorize import apply_rules

BATCH = 60

SYSTEM = """You categorize household bank and credit card transactions.
Choose exactly one category_id from the provided taxonomy for each transaction.
Amount sign: negative = money out (expense), positive = money in (income, refunds, transfers in).
Prefer the category the household has used before for the same or similar merchant (see history).
Card payments and moves between the household's own accounts are Transfer / Credit Card Payment.
confidence is 0..1; use < 0.5 when guessing. reason: <= 12 words."""


class Suggestion(BaseModel):
    id: int
    category_id: int
    confidence: float = Field(ge=0, le=1)
    reason: str


class Suggestions(BaseModel):
    items: list[Suggestion]


async def _taxonomy(session) -> tuple[str, set[int]]:
    rows = (
        await session.execute(
            select(Category.id, Category.name, Category.type, CategoryGroup.name)
            .join(CategoryGroup)
            .where(Category.is_active)
            .order_by(CategoryGroup.sort_order, Category.name)
        )
    ).all()
    return "\n".join(f"{i}: {g} > {n} ({t})" for i, n, t, g in rows), {r[0] for r in rows}


async def _history(session, limit: int = 200) -> str:
    rows = (
        await session.execute(
            select(Transaction.merchant, Category.name, func.count())
            .join(Category, Transaction.category_id == Category.id)
            .where(Transaction.merchant.is_not(None), Transaction.deleted_at.is_(None))
            .group_by(Transaction.merchant, Category.name)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    return "\n".join(f"{m} -> {c} ({n}x)" for m, c, n in rows) or "(none yet)"


@job_handler("categorize")
async def categorize_job(ctx: JobContext) -> dict:
    ids: list[int] | None = ctx.payload.get("ids")
    sm = get_sessionmaker()
    async with sm() as session:
        by_rule = await apply_rules(session, ids)
        await session.commit()
        taxonomy, valid_ids = await _taxonomy(session)
        history = await _history(session)
        q = select(Transaction.id).where(Transaction.category_id.is_(None), Transaction.deleted_at.is_(None))
        if ids:
            q = q.where(id_in(Transaction.id, ids))
        pending = list((await session.scalars(q.order_by(Transaction.id))).all())

    suggested = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start : start + BATCH]
        async with sm() as session:
            txns = (await session.scalars(select(Transaction).where(Transaction.id.in_(chunk)))).unique().all()
            lines = "\n".join(
                f"{t.id} | {t.txn_date} | {t.amount} | {t.description} | {t.account.name if t.account else '-'}"
                for t in txns
            )
            prompt = (
                f"Taxonomy (id: group > category (type)):\n{taxonomy}\n\n"
                f"History (merchant -> category):\n{history}\n\n"
                f"Transactions (id | date | amount | description | account):\n{lines}"
            )
            result: Suggestions = await generate(
                prompt, purpose="categorize", tier="lite", system=SYSTEM, schema=Suggestions, temperature=0
            )
            wanted = set(chunk)
            for s in result.items:
                if s.id not in wanted or s.category_id not in valid_ids:
                    continue
                await session.execute(
                    update(Transaction)
                    .where(Transaction.id == s.id, Transaction.category_id.is_(None))
                    .values(
                        suggested_category_id=s.category_id,
                        suggestion_confidence=Decimal(str(round(s.confidence, 3))),
                        suggestion_reason=s.reason[:500],
                    )
                )
                suggested += 1
            await session.commit()
        await ctx.progress((start + len(chunk)) / len(pending), f"Suggested {suggested} of {len(pending)}")

    return {"categorized_by_rule": by_rule, "suggested": suggested, "considered": len(pending)}
