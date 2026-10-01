from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import get_session
from ledger.models import (
    Account,
    Category,
    CategoryGroup,
    HouseholdMember,
    Institution,
    Tag,
    Transaction,
)
from ledger.schemas import (
    AccountIn,
    AccountOut,
    CategoryGroupIn,
    CategoryGroupOut,
    CategoryIn,
    CategoryOut,
    InstitutionIn,
    InstitutionOut,
    MemberIn,
    MemberOut,
    TagIn,
    TagOut,
)

router = APIRouter(tags=["reference"])


async def _get(session: AsyncSession, model, obj_id: int):
    obj = await session.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {obj_id} not found")
    return obj


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        msg = "Name already exists" if "unique" in str(exc.orig).lower() else "Record is still referenced"
        raise HTTPException(409, msg) from None


def _account_out(a: Account) -> AccountOut:
    return AccountOut.model_validate(a).model_copy(
        update={"institution_name": a.institution.name if a.institution else None}
    )


def _category_out(c: Category) -> CategoryOut:
    return CategoryOut(
        id=c.id,
        name=c.name,
        group_id=c.group_id,
        type=c.type,
        hide_from_reports=c.hide_from_reports,
        is_active=c.is_active,
        description=c.description,
        group_name=c.group.name,
    )


# Institutions
@router.get("/institutions", response_model=list[InstitutionOut])
async def list_institutions(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(Institution).order_by(Institution.name))).all()


@router.post("/institutions", response_model=InstitutionOut, status_code=201)
async def create_institution(body: InstitutionIn, session: AsyncSession = Depends(get_session)):
    obj = Institution(**body.model_dump())
    session.add(obj)
    await _commit(session)
    return obj


@router.put("/institutions/{obj_id}", response_model=InstitutionOut)
async def update_institution(obj_id: int, body: InstitutionIn, session: AsyncSession = Depends(get_session)):
    obj = await _get(session, Institution, obj_id)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    await _commit(session)
    return obj


@router.delete("/institutions/{obj_id}", status_code=204)
async def delete_institution(obj_id: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _get(session, Institution, obj_id))
    await _commit(session)
    return Response(status_code=204)


# Accounts
@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Account).order_by(Account.is_closed, Account.name))).all()
    return [_account_out(a) for a in rows]


@router.post("/accounts", response_model=AccountOut, status_code=201)
async def create_account(body: AccountIn, session: AsyncSession = Depends(get_session)):
    obj = Account(**body.model_dump())
    session.add(obj)
    await _commit(session)
    await session.refresh(obj, ["institution"])
    return _account_out(obj)


@router.put("/accounts/{obj_id}", response_model=AccountOut)
async def update_account(obj_id: int, body: AccountIn, session: AsyncSession = Depends(get_session)):
    obj = await _get(session, Account, obj_id)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    await _commit(session)
    await session.refresh(obj, ["institution"])
    return _account_out(obj)


@router.delete("/accounts/{obj_id}", status_code=204)
async def delete_account(obj_id: int, session: AsyncSession = Depends(get_session)):
    used = await session.scalar(select(func.count()).where(Transaction.account_id == obj_id))
    if used:
        raise HTTPException(409, f"Account has {used} transactions; close or hide it instead")
    await session.delete(await _get(session, Account, obj_id))
    await _commit(session)
    return Response(status_code=204)


# Household members
@router.get("/members", response_model=list[MemberOut])
async def list_members(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(HouseholdMember).order_by(HouseholdMember.name))).all()


@router.post("/members", response_model=MemberOut, status_code=201)
async def create_member(body: MemberIn, session: AsyncSession = Depends(get_session)):
    obj = HouseholdMember(**body.model_dump())
    session.add(obj)
    await _commit(session)
    return obj


@router.put("/members/{obj_id}", response_model=MemberOut)
async def update_member(obj_id: int, body: MemberIn, session: AsyncSession = Depends(get_session)):
    obj = await _get(session, HouseholdMember, obj_id)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    await _commit(session)
    return obj


@router.delete("/members/{obj_id}", status_code=204)
async def delete_member(obj_id: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _get(session, HouseholdMember, obj_id))
    await _commit(session)
    return Response(status_code=204)


# Category groups
@router.get("/category-groups", response_model=list[CategoryGroupOut])
async def list_groups(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(CategoryGroup).order_by(CategoryGroup.sort_order, CategoryGroup.name))).all()


@router.post("/category-groups", response_model=CategoryGroupOut, status_code=201)
async def create_group(body: CategoryGroupIn, session: AsyncSession = Depends(get_session)):
    obj = CategoryGroup(**body.model_dump())
    session.add(obj)
    await _commit(session)
    return obj


@router.put("/category-groups/{obj_id}", response_model=CategoryGroupOut)
async def update_group(obj_id: int, body: CategoryGroupIn, session: AsyncSession = Depends(get_session)):
    obj = await _get(session, CategoryGroup, obj_id)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    await _commit(session)
    return obj


@router.delete("/category-groups/{obj_id}", status_code=204)
async def delete_group(obj_id: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _get(session, CategoryGroup, obj_id))
    await _commit(session)
    return Response(status_code=204)


# Categories
@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.scalars(
            select(Category).join(Category.group).order_by(CategoryGroup.sort_order, CategoryGroup.name, Category.name)
        )
    ).all()
    return [_category_out(c) for c in rows]


@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(body: CategoryIn, session: AsyncSession = Depends(get_session)):
    await _get(session, CategoryGroup, body.group_id)
    obj = Category(**body.model_dump())
    session.add(obj)
    await _commit(session)
    await session.refresh(obj, ["group"])
    return _category_out(obj)


@router.put("/categories/{obj_id}", response_model=CategoryOut)
async def update_category(obj_id: int, body: CategoryIn, session: AsyncSession = Depends(get_session)):
    obj = await _get(session, Category, obj_id)
    await _get(session, CategoryGroup, body.group_id)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    await _commit(session)
    await session.refresh(obj, ["group"])
    return _category_out(obj)


@router.delete("/categories/{obj_id}", status_code=204)
async def delete_category(obj_id: int, session: AsyncSession = Depends(get_session)):
    used = await session.scalar(select(func.count()).where(Transaction.category_id == obj_id))
    if used:
        raise HTTPException(409, f"Category is used by {used} transactions; deactivate it instead")
    await session.delete(await _get(session, Category, obj_id))
    await _commit(session)
    return Response(status_code=204)


# Tags
@router.get("/tags", response_model=list[TagOut])
async def list_tags(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(Tag).order_by(Tag.name))).all()


@router.post("/tags", response_model=TagOut, status_code=201)
async def create_tag(body: TagIn, session: AsyncSession = Depends(get_session)):
    obj = Tag(**body.model_dump())
    session.add(obj)
    await _commit(session)
    return obj


@router.put("/tags/{obj_id}", response_model=TagOut)
async def update_tag(obj_id: int, body: TagIn, session: AsyncSession = Depends(get_session)):
    obj = await _get(session, Tag, obj_id)
    for k, v in body.model_dump().items():
        setattr(obj, k, v)
    await _commit(session)
    return obj


@router.delete("/tags/{obj_id}", status_code=204)
async def delete_tag(obj_id: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _get(session, Tag, obj_id))
    await _commit(session)
    return Response(status_code=204)
