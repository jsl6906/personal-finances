import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import Select, and_, case, cast, func, or_, select, update
from sqlalchemy import String as SAString
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import get_session
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import Account, Category, HouseholdMember, Tag, Transaction, transaction_tag
from ledger.schemas import (
    BulkUpdate,
    IdList,
    JobOut,
    SuggestRequest,
    TagOut,
    TransactionCreate,
    TransactionOut,
    TransactionPage,
    TransactionUpdate,
)
from ledger.services import merchants
from ledger.services.categorize import learn_rule, rule_category
from ledger.services.normalize import fingerprint

router = APIRouter(prefix="/transactions", tags=["transactions"])

SORTS = {
    "date_desc": (Transaction.txn_date.desc(), Transaction.id.desc()),
    "date_asc": (Transaction.txn_date.asc(), Transaction.id.asc()),
    "amount_desc": (Transaction.amount.desc(), Transaction.id.desc()),
    "amount_asc": (Transaction.amount.asc(), Transaction.id.asc()),
    "added_desc": (Transaction.created_at.desc(), Transaction.id.desc()),
}


def to_out(t: Transaction) -> TransactionOut:
    return TransactionOut(
        id=t.id,
        txn_date=t.txn_date,
        posted_date=t.posted_date,
        description=t.description,
        original_description=t.original_description,
        merchant=t.merchant,
        merchant_name=(t.merchant_profile and t.merchant_profile.display_name) or merchants.default_name(t.merchant),
        merchant_source=t.merchant_source,
        amount=t.amount,
        account_id=t.account_id,
        account_name=t.account.name if t.account else None,
        institution_name=t.account.institution.name if t.account and t.account.institution else None,
        category_id=t.category_id,
        category_name=t.category.name if t.category else None,
        category_group_id=t.category.group_id if t.category else None,
        category_group_name=t.category.group.name if t.category else None,
        category_type=t.category.type if t.category else None,
        category_source=t.category_source,
        suggested_category_id=t.suggested_category_id,
        suggested_category_name=t.suggested_category.name if t.suggested_category else None,
        suggestion_confidence=t.suggestion_confidence,
        suggestion_reason=t.suggestion_reason,
        member_id=t.member_id,
        member_initials=t.member.initials if t.member else None,
        notes=t.notes,
        check_number=t.check_number,
        budget_spread_months=t.budget_spread_months,
        source_type=t.source_type,
        external_id=t.external_id,
        import_batch_id=t.import_batch_id,
        transfer_match_id=t.transfer_match_id,
        has_statement=bool(t.has_statement),
        tags=[TagOut.model_validate(tag) for tag in t.tags],
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


async def _load(session: AsyncSession, txn_id: int) -> Transaction:
    t = await session.scalar(
        select(Transaction)
        .where(Transaction.id == txn_id, Transaction.deleted_at.is_(None))
        .execution_options(populate_existing=True)
    )
    if t is None:
        raise HTTPException(404, "Transaction not found")
    return t


async def _check_refs(session: AsyncSession, account_id=None, category_id=None, member_id=None) -> None:
    for model, obj_id in ((Account, account_id), (Category, category_id), (HouseholdMember, member_id)):
        if obj_id is not None and await session.get(model, obj_id) is None:
            raise HTTPException(422, f"{model.__name__} {obj_id} does not exist")


async def _tags(session: AsyncSession, tag_ids: list[int]) -> list[Tag]:
    if not tag_ids:
        return []
    tags = list((await session.scalars(select(Tag).where(Tag.id.in_(tag_ids)))).all())
    if len(tags) != len(set(tag_ids)):
        raise HTTPException(422, "Unknown tag id")
    return tags


def _filtered(
    q: str | None,
    start: date | None,
    end: date | None,
    account_id: list[int] | None,
    category_id: list[int] | None,
    group_id: int | None,
    member_id: int | None,
    tag_id: int | None,
    status: str,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
    import_batch_id: int | None = None,
    merchant: str | None = None,
) -> list:
    conds = [Transaction.deleted_at.is_(None)]
    if import_batch_id:
        conds.append(Transaction.import_batch_id == import_batch_id)
    if merchant:
        conds.append(func.coalesce(Transaction.merchant, func.lower(Transaction.description)) == merchant)
    if start:
        conds.append(Transaction.txn_date >= start)
    if end:
        conds.append(Transaction.txn_date <= end)
    if account_id:
        conds.append(Transaction.account_id.in_(account_id))
    if category_id:
        conds.append(Transaction.category_id.in_(category_id))
    if group_id:
        conds.append(Transaction.category_id.in_(select(Category.id).where(Category.group_id == group_id)))
    if member_id:
        conds.append(Transaction.member_id == member_id)
    if tag_id:
        conds.append(Transaction.id.in_(select(transaction_tag.c.transaction_id).where(transaction_tag.c.tag_id == tag_id)))
    if min_amount is not None:
        conds.append(func.abs(Transaction.amount) >= min_amount)
    if max_amount is not None:
        conds.append(func.abs(Transaction.amount) <= max_amount)
    if status == "uncategorized":
        conds.append(Transaction.category_id.is_(None))
    elif status == "suggested":
        conds.append(and_(Transaction.category_id.is_(None), Transaction.suggested_category_id.is_not(None)))
    elif status == "with_statement":
        conds.append(Transaction.has_statement)
    if q:
        like = f"%{q.strip()}%"
        conds.append(
            or_(
                Transaction.description.ilike(like),
                Transaction.original_description.ilike(like),
                Transaction.notes.ilike(like),
                cast(Transaction.amount, SAString).like(like.replace("$", "").replace(",", "")),
            )
        )
    return conds


@router.get("", response_model=TransactionPage)
async def list_transactions(
    q: str | None = None,
    start: date | None = None,
    end: date | None = None,
    account_id: list[int] | None = Query(None),
    category_id: list[int] | None = Query(None),
    group_id: int | None = None,
    member_id: int | None = None,
    tag_id: int | None = None,
    status: Literal["all", "uncategorized", "suggested", "with_statement"] = "all",
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    import_batch_id: int | None = None,
    merchant: str | None = None,
    sort: Literal["date_desc", "date_asc", "amount_desc", "amount_asc", "added_desc"] = "date_desc",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    conds = _filtered(
        q, start, end, account_id, category_id, group_id, member_id, tag_id, status, min_amount, max_amount, import_batch_id,
        merchant,
    )
    agg = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)), 0),
                func.coalesce(func.sum(case((Transaction.amount < 0, Transaction.amount), else_=0)), 0),
            ).where(*conds)
        )
    ).one()
    stmt: Select = select(Transaction).where(*conds).order_by(*SORTS[sort]).limit(limit).offset(offset)
    rows = (await session.scalars(stmt)).unique().all()
    return TransactionPage(items=[to_out(t) for t in rows], total=agg[0], total_in=agg[1], total_out=agg[2])


@router.get("/export.csv")
async def export_csv(
    q: str | None = None,
    start: date | None = None,
    end: date | None = None,
    account_id: list[int] | None = Query(None),
    category_id: list[int] | None = Query(None),
    group_id: int | None = None,
    tag_id: int | None = None,
    status: Literal["all", "uncategorized", "suggested", "with_statement"] = "all",
    import_batch_id: int | None = None,
    merchant: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    conds = _filtered(
        q, start, end, account_id, category_id, group_id, None, tag_id, status, None, None, import_batch_id, merchant
    )
    rows = (await session.scalars(select(Transaction).where(*conds).order_by(*SORTS["date_asc"]))).unique().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "id",
            "date",
            "posted_date",
            "description",
            "amount",
            "category",
            "group",
            "account",
            "institution",
            "member",
            "tags",
            "notes",
            "source",
            "date_added",
        ]
    )
    for t in rows:
        o = to_out(t)
        w.writerow(
            [
                o.id,
                o.txn_date,
                o.posted_date or "",
                _csv_safe(o.description),
                o.amount,
                o.category_name or "",
                o.category_group_name or "",
                o.account_name or "",
                o.institution_name or "",
                o.member_initials or "",
                ";".join(tg.name for tg in o.tags),
                _csv_safe(o.notes or ""),
                o.source_type,
                o.created_at.date(),
            ]
        )
    return Response(
        buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="transactions.csv"'}
    )


def _csv_safe(value: str) -> str:
    # Neutralize spreadsheet formula injection from bank-provided text.
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


@router.get("/{txn_id}", response_model=TransactionOut)
async def get_transaction(txn_id: int, session: AsyncSession = Depends(get_session)):
    return to_out(await _load(session, txn_id))


@router.post("", response_model=TransactionOut, status_code=201)
async def create_transaction(body: TransactionCreate, session: AsyncSession = Depends(get_session)):
    await _check_refs(session, body.account_id, body.category_id, body.member_id)
    data = body.model_dump(exclude={"tag_ids", "merchant_name"})
    t = Transaction(**data, source_type="manual")
    try:
        await merchants.set_transaction_merchant(session, t, body.merchant_name)
    except merchants.MerchantError as exc:
        raise HTTPException(422, str(exc)) from None
    t.fingerprint = fingerprint(body.account_id, body.txn_date, body.amount, body.description)
    if body.category_id is not None:
        t.category_source = "user"
    else:
        rule = await rule_category(session, t.merchant)
        if rule:
            t.category_id, t.category_source = rule, "rule"
    t.tags = await _tags(session, body.tag_ids)
    session.add(t)
    await session.commit()
    return to_out(await _load(session, t.id))


@router.patch("/{txn_id}", response_model=TransactionOut)
async def update_transaction(txn_id: int, body: TransactionUpdate, session: AsyncSession = Depends(get_session)):
    t = await _load(session, txn_id)
    fields = set(body.model_fields_set)
    if "category_id" in fields and body.category_id == t.category_id:
        fields.discard("category_id")
    await _check_refs(
        session,
        body.account_id if "account_id" in fields else None,
        body.category_id if "category_id" in fields else None,
        body.member_id if "member_id" in fields else None,
    )
    for name in fields - {"tag_ids", "merchant_name"}:
        value = getattr(body, name)
        if name in ("txn_date", "description", "amount") and value is None:
            raise HTTPException(422, f"{name} cannot be null")
        setattr(t, name, value)
    if "merchant_name" in fields:
        try:
            await merchants.set_transaction_merchant(session, t, body.merchant_name)
        except merchants.MerchantError as exc:
            raise HTTPException(422, str(exc)) from None
    elif "description" in fields and t.merchant_source != "user":
        t.merchant = await merchants.auto_key(session, t.description)
    if fields & {"account_id", "txn_date", "amount", "description"}:
        t.fingerprint = fingerprint(t.account_id, t.txn_date, t.amount, t.description)
    if "category_id" in fields:
        t.category_source = "user" if t.category_id else None
        t.suggested_category_id = t.suggestion_confidence = t.suggestion_reason = None
        await learn_rule(session, t.merchant, t.category_id)
    if "tag_ids" in fields and body.tag_ids is not None:
        t.tags = await _tags(session, body.tag_ids)
    await session.commit()
    return to_out(await _load(session, txn_id))


@router.delete("/{txn_id}", status_code=204)
async def delete_transaction(txn_id: int, session: AsyncSession = Depends(get_session)):
    t = await _load(session, txn_id)
    t.deleted_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=204)


@router.post("/bulk")
async def bulk_update(body: BulkUpdate, session: AsyncSession = Depends(get_session)):
    live = and_(Transaction.id.in_(body.ids), Transaction.deleted_at.is_(None))
    if body.delete:
        res = await session.execute(update(Transaction).where(live).values(deleted_at=datetime.now(UTC)))
        await session.commit()
        return {"updated": res.rowcount}

    await _check_refs(session, body.account_id, body.category_id, body.member_id)
    values: dict = {}
    if body.category_id is not None:
        values |= {
            "category_id": body.category_id,
            "category_source": "user",
            "suggested_category_id": None,
            "suggestion_confidence": None,
            "suggestion_reason": None,
        }
    if body.member_id is not None:
        values["member_id"] = body.member_id
    if body.account_id is not None:
        values["account_id"] = body.account_id
    updated = 0
    if values:
        res = await session.execute(update(Transaction).where(live).values(**values))
        updated = res.rowcount
    if body.category_id is not None:
        merchants = (await session.scalars(select(Transaction.merchant).where(live).distinct())).all()
        for m in merchants:
            await learn_rule(session, m, body.category_id)
    if body.add_tag_ids or body.remove_tag_ids:
        await _tags(session, body.add_tag_ids + body.remove_tag_ids)
        ids = (await session.scalars(select(Transaction.id).where(live))).all()
        if body.remove_tag_ids:
            await session.execute(
                transaction_tag.delete().where(
                    transaction_tag.c.transaction_id.in_(ids), transaction_tag.c.tag_id.in_(body.remove_tag_ids)
                )
            )
        if body.add_tag_ids and ids:
            await session.execute(
                insert(transaction_tag)
                .values([{"transaction_id": i, "tag_id": tg} for i in ids for tg in body.add_tag_ids])
                .on_conflict_do_nothing()
            )
        updated = max(updated, len(ids))
    await session.commit()
    return {"updated": updated}


@router.post("/suggestions/run", response_model=JobOut, status_code=202)
async def run_suggestions(body: SuggestRequest, session: AsyncSession = Depends(get_session)):
    job = await enqueue(session, "categorize", {"ids": body.ids} if body.ids else {})
    await session.commit()
    notify_worker()
    await session.refresh(job)
    return job


@router.post("/suggestions/accept")
async def accept_suggestions(body: IdList, session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Transaction.id, Transaction.merchant, Transaction.suggested_category_id).where(
                Transaction.id.in_(body.ids),
                Transaction.suggested_category_id.is_not(None),
                Transaction.deleted_at.is_(None),
            )
        )
    ).all()
    for txn_id, merchant, cat in rows:
        await session.execute(
            update(Transaction)
            .where(Transaction.id == txn_id)
            .values(
                category_id=cat,
                category_source="ai",
                suggested_category_id=None,
                suggestion_confidence=None,
                suggestion_reason=None,
            )
        )
        await learn_rule(session, merchant, cat)
    await session.commit()
    return {"accepted": len(rows)}


@router.post("/suggestions/reject")
async def reject_suggestions(body: IdList, session: AsyncSession = Depends(get_session)):
    res = await session.execute(
        update(Transaction)
        .where(Transaction.id.in_(body.ids))
        .values(suggested_category_id=None, suggestion_confidence=None, suggestion_reason=None)
    )
    await session.commit()
    return {"rejected": res.rowcount}
