from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.budgets.service import OVERALL_NAME, budget_status, period_for, rule_matches, suggest_budgets
from ledger.db.engine import get_session
from ledger.models import Budget, Category, CategoryGroup, SpreadRule

router = APIRouter(tags=["budgets"])


class BudgetIn(BaseModel):
    category_id: int | None = None
    group_id: int | None = None
    period_type: Literal["month", "quarter", "year"] = "month"
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    notes: str | None = None

    @model_validator(mode="after")
    def _one_target(self):
        if self.category_id is not None and self.group_id is not None:
            raise ValueError("Set category_id or group_id, not both (neither = overall spending)")
        return self


class BudgetOut(BudgetIn):
    id: int
    name: str


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category_id: int | None = None
    merchant_pattern: str | None = Field(default=None, max_length=200)
    min_amount: Decimal | None = Field(default=None, ge=0)
    months: int = Field(default=12, ge=1, le=60)
    is_active: bool = True

    @model_validator(mode="after")
    def _has_criteria(self):
        if self.category_id is None and not self.merchant_pattern:
            raise ValueError("A rule needs a category or a description pattern")
        return self


class RuleOut(RuleIn):
    id: int
    category_name: str | None = None
    matches_12m: int = 0
    total_12m: Decimal = Decimal(0)


async def _budget_out(session: AsyncSession, b: Budget) -> BudgetOut:
    if b.category_id:
        name = await session.scalar(select(Category.name).where(Category.id == b.category_id))
    elif b.group_id:
        name = await session.scalar(select(CategoryGroup.name).where(CategoryGroup.id == b.group_id))
    else:
        name = OVERALL_NAME
    return BudgetOut(
        id=b.id,
        name=name or "?",
        category_id=b.category_id,
        group_id=b.group_id,
        period_type=b.period_type,
        amount=b.amount,
        notes=b.notes,
    )


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "That category, group or overall spending already has a budget") from None


@router.get("/budgets", response_model=list[BudgetOut])
async def list_budgets(session: AsyncSession = Depends(get_session)):
    return [await _budget_out(session, b) for b in (await session.scalars(select(Budget).order_by(Budget.id))).all()]


@router.post("/budgets", response_model=BudgetOut, status_code=201)
async def create_budget(body: BudgetIn, session: AsyncSession = Depends(get_session)):
    b = Budget(**body.model_dump())
    session.add(b)
    await _commit(session)
    return await _budget_out(session, b)


@router.put("/budgets/{budget_id}", response_model=BudgetOut)
async def update_budget(budget_id: int, body: BudgetIn, session: AsyncSession = Depends(get_session)):
    b = await session.get(Budget, budget_id)
    if b is None:
        raise HTTPException(404, "Budget not found")
    for k, v in body.model_dump().items():
        setattr(b, k, v)
    await _commit(session)
    return await _budget_out(session, b)


@router.delete("/budgets/{budget_id}", status_code=204)
async def delete_budget(budget_id: int, session: AsyncSession = Depends(get_session)):
    b = await session.get(Budget, budget_id)
    if b is None:
        raise HTTPException(404, "Budget not found")
    await session.delete(b)
    await session.commit()
    return Response(status_code=204)


@router.get("/budgets/status")
async def status(
    period: Literal["month", "quarter", "year"] = "month",
    on: date | None = None,
    session: AsyncSession = Depends(get_session),
):
    today = date.today()
    return await budget_status(session, period_for(period, on or today), today)


@router.get("/budgets/suggestions")
async def suggestions(months: int = Query(12, ge=3, le=36), session: AsyncSession = Depends(get_session)):
    return await suggest_budgets(session, date.today(), months)


# ---- spread rules ----
async def _rule_out(session: AsyncSession, r: SpreadRule) -> RuleOut:
    n, total = await rule_matches(session, r, date.today() - timedelta(days=365))
    cat = await session.scalar(select(Category.name).where(Category.id == r.category_id)) if r.category_id else None
    return RuleOut(
        id=r.id,
        name=r.name,
        category_id=r.category_id,
        merchant_pattern=r.merchant_pattern,
        min_amount=r.min_amount,
        months=r.months,
        is_active=r.is_active,
        category_name=cat,
        matches_12m=n,
        total_12m=total,
    )


@router.get("/spread-rules", response_model=list[RuleOut])
async def list_rules(session: AsyncSession = Depends(get_session)):
    return [await _rule_out(session, r) for r in (await session.scalars(select(SpreadRule).order_by(SpreadRule.id))).all()]


@router.post("/spread-rules", response_model=RuleOut, status_code=201)
async def create_rule(body: RuleIn, session: AsyncSession = Depends(get_session)):
    r = SpreadRule(**body.model_dump())
    session.add(r)
    await session.commit()
    return await _rule_out(session, r)


@router.put("/spread-rules/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: int, body: RuleIn, session: AsyncSession = Depends(get_session)):
    r = await session.get(SpreadRule, rule_id)
    if r is None:
        raise HTTPException(404, "Rule not found")
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    await session.commit()
    return await _rule_out(session, r)


@router.delete("/spread-rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(SpreadRule, rule_id)
    if r is None:
        raise HTTPException(404, "Rule not found")
    await session.delete(r)
    await session.commit()
    return Response(status_code=204)
