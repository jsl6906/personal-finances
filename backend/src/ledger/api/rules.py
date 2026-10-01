"""Category rules (auto-categorization) and import category-label aliases."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import get_session
from ledger.models import Account, Category, CategoryAlias, CategoryGroup, CategoryRule, Transaction
from ledger.services import merchants
from ledger.services.categorize import PRECEDENCE, apply_rule, describe, matching_rules, rule_condition

router = APIRouter(tags=["rules"])


class RuleSpecIn(BaseModel):
    match_type: Literal["merchant", "contains", "regex"] = "merchant"
    pattern: str = Field(min_length=1, max_length=300)
    category_id: int
    account_id: int | None = None
    amount_min: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    amount_max: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)

    @model_validator(mode="after")
    def _clean(self):
        self.pattern = self.pattern.strip()
        if self.match_type == "merchant":
            self.pattern = self.pattern.lower()
        if len(self.pattern) < (1 if self.match_type == "merchant" else 2):
            raise ValueError("Pattern is too short")
        if self.amount_min is not None and self.amount_max is not None and self.amount_min > self.amount_max:
            raise ValueError("Minimum amount is above the maximum")
        return self


class RuleIn(RuleSpecIn):
    priority: int = Field(default=100, ge=0, le=1000)
    is_active: bool = True
    note: str | None = Field(default=None, max_length=500)
    # POST only: if an identical rule exists, update it instead of failing.
    replace_existing: bool = False


class RuleOut(BaseModel):
    id: int
    match_type: str
    pattern: str
    category_id: int
    category_name: str
    category_group_name: str
    account_id: int | None
    account_name: str | None
    amount_min: Decimal | None
    amount_max: Decimal | None
    priority: int
    is_active: bool
    source: str
    note: str | None
    description: str
    applied_count: int
    created_at: datetime
    updated_at: datetime


class PreviewTxn(BaseModel):
    id: int
    txn_date: date
    description: str
    amount: Decimal
    account_name: str | None
    category_name: str | None
    category_source: str | None


class PreviewOut(BaseModel):
    matches: int
    uncategorized: int
    same: int
    auto: int
    user: int
    samples: list[PreviewTxn]


class ApplyIn(BaseModel):
    include_user: bool = False


async def _outs(session: AsyncSession, rules: list[CategoryRule]) -> list[RuleOut]:
    if not rules:
        return []
    cat_ids = {r.category_id for r in rules}
    cats = {
        i: (n, g)
        for i, n, g in await session.execute(
            select(Category.id, Category.name, CategoryGroup.name).join(CategoryGroup).where(Category.id.in_(cat_ids))
        )
    }
    acct_ids = {r.account_id for r in rules if r.account_id}
    accts = (
        dict((await session.execute(select(Account.id, Account.name).where(Account.id.in_(acct_ids)))).all())
        if acct_ids
        else {}
    )
    counts = dict(
        (
            await session.execute(
                select(Transaction.category_rule_id, func.count())
                .where(Transaction.category_rule_id.in_([r.id for r in rules]), Transaction.deleted_at.is_(None))
                .group_by(Transaction.category_rule_id)
            )
        ).all()
    )
    return [
        RuleOut(
            id=r.id,
            match_type=r.match_type,
            pattern=r.pattern,
            category_id=r.category_id,
            category_name=cats.get(r.category_id, ("?", "?"))[0],
            category_group_name=cats.get(r.category_id, ("?", "?"))[1],
            account_id=r.account_id,
            account_name=accts.get(r.account_id),
            amount_min=r.amount_min,
            amount_max=r.amount_max,
            priority=r.priority,
            is_active=r.is_active,
            source=r.source,
            note=r.note,
            description=describe(r),
            applied_count=counts.get(r.id, 0),
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rules
    ]


async def _rule(session: AsyncSession, rule_id: int) -> CategoryRule:
    r = await session.get(CategoryRule, rule_id, populate_existing=True)
    if r is None:
        raise HTTPException(404, "Rule not found")
    return r


async def _validate(session: AsyncSession, body: RuleSpecIn) -> None:
    if await session.get(Category, body.category_id) is None:
        raise HTTPException(422, f"Category {body.category_id} does not exist")
    if body.account_id is not None and await session.get(Account, body.account_id) is None:
        raise HTTPException(422, f"Account {body.account_id} does not exist")
    if body.match_type == "merchant":
        body.pattern = (await merchants.resolve(session, body.pattern)) or body.pattern
    elif body.match_type == "regex":
        try:
            async with session.begin_nested():
                await session.execute(text("SELECT '' ~* :p"), {"p": body.pattern})
        except DBAPIError:
            raise HTTPException(422, "Not a valid regular expression") from None


async def _identical(session: AsyncSession, body: RuleSpecIn, exclude_id: int | None = None) -> CategoryRule | None:
    q = select(CategoryRule).where(
        CategoryRule.match_type == body.match_type,
        CategoryRule.pattern == body.pattern,
        CategoryRule.account_id.is_not_distinct_from(body.account_id),
        CategoryRule.amount_min.is_not_distinct_from(body.amount_min),
        CategoryRule.amount_max.is_not_distinct_from(body.amount_max),
    )
    if exclude_id is not None:
        q = q.where(CategoryRule.id != exclude_id)
    return await session.scalar(q.order_by(CategoryRule.id).limit(1))


def _assign(r: CategoryRule, body: RuleIn) -> None:
    for k, v in body.model_dump(exclude={"replace_existing"}).items():
        setattr(r, k, v)
    r.source = "user"


@router.get("/rules", response_model=list[RuleOut])
async def list_rules(session: AsyncSession = Depends(get_session)):
    rules = (await session.scalars(select(CategoryRule).order_by(*PRECEDENCE))).all()
    return await _outs(session, list(rules))


@router.get("/rules/for-transaction/{txn_id}", response_model=list[RuleOut])
async def rules_for_transaction(txn_id: int, session: AsyncSession = Depends(get_session)):
    return await _outs(session, await matching_rules(session, txn_id))


@router.get("/rules/{rule_id}", response_model=RuleOut)
async def get_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    return (await _outs(session, [await _rule(session, rule_id)]))[0]


@router.post("/rules", response_model=RuleOut, status_code=201)
async def create_rule(body: RuleIn, session: AsyncSession = Depends(get_session)):
    await _validate(session, body)
    r = await _identical(session, body)
    if r is not None and not body.replace_existing:
        raise HTTPException(409, f"An identical rule already exists (#{r.id})")
    if r is None:
        r = CategoryRule()
        session.add(r)
    _assign(r, body)
    await session.commit()
    return (await _outs(session, [await _rule(session, r.id)]))[0]


@router.put("/rules/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: int, body: RuleIn, session: AsyncSession = Depends(get_session)):
    r = await _rule(session, rule_id)
    await _validate(session, body)
    if (dup := await _identical(session, body, exclude_id=rule_id)) is not None:
        raise HTTPException(409, f"An identical rule already exists (#{dup.id})")
    _assign(r, body)
    await session.commit()
    return (await _outs(session, [await _rule(session, rule_id)]))[0]


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _rule(session, rule_id))
    await session.commit()
    return Response(status_code=204)


@router.post("/rules/preview", response_model=PreviewOut)
async def preview_rule(body: RuleSpecIn, session: AsyncSession = Depends(get_session)):
    """What a rule would match today, split by how those transactions are categorized now."""
    await _validate(session, body)
    T, cat = Transaction, body.category_id
    live = and_(T.deleted_at.is_(None), rule_condition(body))
    differs = or_(T.category_id.is_(None), T.category_id != cat)
    by_user = func.coalesce(T.category_source, "") == "user"
    n, uncat, same, auto, user = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(T.category_id.is_(None)),
                func.count().filter(T.category_id == cat),
                func.count().filter(T.category_id != cat, ~by_user),
                func.count().filter(T.category_id != cat, by_user),
            ).where(live)
        )
    ).one()
    samples = (
        await session.execute(
            select(T.id, T.txn_date, T.description, T.amount, Account.name, Category.name, T.category_source)
            .outerjoin(Account, Account.id == T.account_id)
            .outerjoin(Category, Category.id == T.category_id)
            .where(live, differs)
            .order_by(T.txn_date.desc(), T.id.desc())
            .limit(8)
        )
    ).all()
    return PreviewOut(
        matches=n,
        uncategorized=uncat,
        same=same,
        auto=auto,
        user=user,
        samples=[
            PreviewTxn(id=i, txn_date=d, description=desc, amount=a, account_name=an, category_name=cn, category_source=cs)
            for i, d, desc, a, an, cn, cs in samples
        ],
    )


@router.post("/rules/{rule_id}/apply")
async def apply_rule_now(rule_id: int, body: ApplyIn, session: AsyncSession = Depends(get_session)):
    r = await _rule(session, rule_id)
    updated = await apply_rule(session, r, include_user=body.include_user)
    await session.commit()
    return {"updated": updated}


# ---- import category labels -> our categories ----
class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=200)
    category_id: int


class AliasOut(AliasIn):
    id: int
    category_name: str


async def _alias_out(session: AsyncSession, a: CategoryAlias) -> AliasOut:
    name = await session.scalar(select(Category.name).where(Category.id == a.category_id))
    return AliasOut(id=a.id, alias=a.alias, category_id=a.category_id, category_name=name or "?")


async def _save_alias(session: AsyncSession, a: CategoryAlias, body: AliasIn) -> AliasOut:
    if await session.get(Category, body.category_id) is None:
        raise HTTPException(422, f"Category {body.category_id} does not exist")
    a.alias, a.category_id = body.alias.strip().lower(), body.category_id
    session.add(a)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "That label is already mapped") from None
    return await _alias_out(session, a)


@router.get("/category-aliases", response_model=list[AliasOut])
async def list_aliases(session: AsyncSession = Depends(get_session)):
    rows = await session.execute(
        select(CategoryAlias, Category.name)
        .join(Category, Category.id == CategoryAlias.category_id)
        .order_by(CategoryAlias.alias)
    )
    return [AliasOut(id=a.id, alias=a.alias, category_id=a.category_id, category_name=n) for a, n in rows]


@router.post("/category-aliases", response_model=AliasOut, status_code=201)
async def create_alias(body: AliasIn, session: AsyncSession = Depends(get_session)):
    return await _save_alias(session, CategoryAlias(), body)


@router.put("/category-aliases/{alias_id}", response_model=AliasOut)
async def update_alias(alias_id: int, body: AliasIn, session: AsyncSession = Depends(get_session)):
    a = await session.get(CategoryAlias, alias_id)
    if a is None:
        raise HTTPException(404, "Alias not found")
    return await _save_alias(session, a, body)


@router.delete("/category-aliases/{alias_id}", status_code=204)
async def delete_alias(alias_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(CategoryAlias, alias_id)
    if a is None:
        raise HTTPException(404, "Alias not found")
    await session.delete(a)
    await session.commit()
    return Response(status_code=204)
