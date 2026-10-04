import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.db.filters import id_in
from ledger.imports.coverage import ACTIONABLE, AUTO_CONFIDENCE, account_names, apply_fixes, save_checks
from ledger.imports.service import (
    ImportError_,
    commit_batch,
    create_batch,
    load_spreadsheet,
    rollback_batch,
    save_template,
)
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import (
    Account,
    Attachment,
    BackfillFile,
    Category,
    DuplicatePair,
    ImportBatch,
    ImportMappingTemplate,
    ImportRow,
    Job,
    StatementCheck,
    Transaction,
)
from ledger.schemas import (
    BatchDetail,
    BatchSource,
    BatchSummary,
    BulkDecisionIn,
    CheckFixIn,
    CommitIn,
    DecisionIn,
    ImportPairOut,
    ImportRowOut,
    JobOut,
    PrepareIn,
    SheetIn,
    StatementCheckOut,
    TxnBrief,
)
from ledger.sources.backfill import mark_done

router = APIRouter(tags=["imports"])


def _summary(b: ImportBatch) -> dict:
    return {
        "id": b.id,
        "filename": b.attachment.filename if b.attachment else None,
        "source_type": b.source_type,
        "origin": b.origin,
        "status": b.status,
        "row_count": b.row_count,
        "stats": b.stats,
        "error": b.error,
        "job_id": b.job_id,
        "created_at": b.created_at,
        "committed_at": b.committed_at,
    }


async def _batch(session: AsyncSession, batch_id: int) -> ImportBatch:
    b = await session.get(ImportBatch, batch_id, populate_existing=True)
    if b is None:
        raise HTTPException(404, "Import not found")
    return b


async def _detail(session: AsyncSession, b: ImportBatch) -> BatchDetail:
    decisions = dict(
        (
            await session.execute(
                select(ImportRow.decision, func.count()).where(ImportRow.batch_id == b.id).group_by(ImportRow.decision)
            )
        ).all()
    )
    preview = (
        await session.scalars(select(ImportRow.raw).where(ImportRow.batch_id == b.id).order_by(ImportRow.row_index).limit(8))
    ).all()
    template_name = (
        await session.scalar(select(ImportMappingTemplate.name).where(ImportMappingTemplate.id == b.template_id))
        if b.template_id
        else None
    )
    source = BatchSource()
    if att := b.attachment:
        source = BatchSource(
            mime_type=att.mime_type, size_bytes=att.size_bytes, sha256=att.sha256, uploaded_at=att.created_at
        )
    bf = await session.scalar(select(BackfillFile).where(BackfillFile.import_batch_id == b.id).limit(1))
    if bf:
        source.archive_provider = bf.provider
        source.archive_path = bf.path
        source.archive_kind = bf.kind
        source.archive_modified_at = bf.modified_at
    return BatchDetail(
        **_summary(b),
        source=source,
        attachment_id=b.attachment_id,
        sheet_name=b.sheet_name,
        sheets=b.sheets,
        columns=b.columns,
        mapping=b.mapping,
        mapping_source=b.mapping_source,
        options=b.options,
        defaults=b.defaults,
        doc_meta=b.doc_meta,
        template_name=template_name,
        decisions=decisions,
        preview=list(preview),
    )


def _row_out(r: ImportRow, accounts: dict, categories: dict) -> ImportRowOut:
    out = ImportRowOut.model_validate(r)
    out.account_name = accounts.get(r.account_id)
    out.category_name = categories.get(r.category_id)
    return out


def _brief(t: Transaction) -> TxnBrief:
    return TxnBrief(
        id=t.id,
        txn_date=t.txn_date,
        description=t.description,
        amount=t.amount,
        account_name=t.account.name if t.account else None,
        category_name=t.category.name if t.category else None,
        notes=t.notes,
        source_type=t.source_type,
        created_at=t.created_at,
    )


async def _names(session: AsyncSession) -> tuple[dict, dict]:
    accounts = dict((await session.execute(select(Account.id, Account.name))).all())
    categories = dict((await session.execute(select(Category.id, Category.name))).all())
    return accounts, categories


@router.get("/imports", response_model=list[BatchSummary])
async def list_imports(limit: int = Query(20, le=200), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(limit))).unique().all()
    return [BatchSummary(**_summary(b)) for b in rows]


@router.post("/imports", response_model=BatchDetail, status_code=201)
async def upload_import(
    file: UploadFile = File(...), sheet: str | None = Form(None), session: AsyncSession = Depends(get_session)
):
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    try:
        batch = await create_batch(session, data, file.filename or "upload", file.content_type, sheet)
    except ImportError_ as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from None
    await session.commit()
    notify_worker()
    return await _detail(session, await _batch(session, batch.id))


@router.get("/imports/{batch_id}", response_model=BatchDetail)
async def get_import(batch_id: int, session: AsyncSession = Depends(get_session)):
    return await _detail(session, await _batch(session, batch_id))


@router.delete("/imports/{batch_id}", status_code=204)
async def discard_import(batch_id: int, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    if b.status == "committed":
        raise HTTPException(409, "Committed imports must be rolled back, not discarded")
    await session.delete(b)
    await session.commit()
    return Response(status_code=204)


@router.post("/imports/{batch_id}/sheet", response_model=BatchDetail)
async def choose_sheet(batch_id: int, body: SheetIn, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    if b.source_type != "spreadsheet" or b.status not in ("mapping", "review"):
        raise HTTPException(409, "Sheet can only be changed while mapping a spreadsheet")
    data = await session.scalar(select(Attachment.content).where(Attachment.id == b.attachment_id))
    try:
        await load_spreadsheet(session, b, data, b.attachment.filename, body.sheet)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from None
    await session.commit()
    return await _detail(session, await _batch(session, batch_id))


@router.post("/imports/{batch_id}/extract", response_model=BatchDetail)
async def reextract_import(batch_id: int, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    if b.source_type != "document" or b.status not in ("mapping", "review", "failed"):
        raise HTTPException(409, "Only uncommitted document imports can be re-read")
    b.status, b.error = "extracting", None
    job = await enqueue(session, "extract_document", {"batch_id": b.id})
    b.job_id = job.id
    await session.commit()
    notify_worker()
    return await _detail(session, await _batch(session, batch_id))


@router.post("/imports/{batch_id}/prepare", response_model=BatchDetail)
async def prepare_import(batch_id: int, body: PrepareIn, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    if b.status not in ("mapping", "review"):
        raise HTTPException(409, f"Import is {b.status}")
    unknown = set(body.mapping) - {c["name"] for c in b.columns}
    if unknown:
        raise HTTPException(422, f"Unknown columns: {', '.join(sorted(unknown))}")
    b.mapping = dict(body.mapping)
    b.options = body.options.model_dump()
    b.defaults = body.defaults.model_dump()
    if body.save_template and b.source_type == "spreadsheet":
        await save_template(session, b, body.template_name or (b.attachment.filename if b.attachment else "Preset"))
    b.status, b.error = "preparing", None
    job = await enqueue(session, "prepare_import", {"batch_id": b.id})
    b.job_id = job.id
    await session.commit()
    notify_worker()
    return await _detail(session, await _batch(session, batch_id))


@router.get("/imports/{batch_id}/rows", response_model=list[ImportRowOut])
async def import_rows(
    batch_id: int,
    decision: str | None = None,
    limit: int = Query(200, le=5000),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    q = select(ImportRow).where(ImportRow.batch_id == batch_id).order_by(ImportRow.row_index).limit(limit).offset(offset)
    if decision:
        q = q.where(ImportRow.decision == decision)
    accounts, categories = await _names(session)
    return [_row_out(r, accounts, categories) for r in (await session.scalars(q)).all()]


@router.get("/imports/{batch_id}/duplicates", response_model=list[ImportPairOut])
async def import_duplicates(batch_id: int, include_exact: bool = False, session: AsyncSession = Depends(get_session)):
    q = (
        select(DuplicatePair, ImportRow)
        .join(ImportRow, DuplicatePair.import_row_id == ImportRow.id)
        .where(ImportRow.batch_id == batch_id)
        .order_by(ImportRow.row_index)
    )
    if not include_exact:
        q = q.where(DuplicatePair.score < 1)
    accounts, categories = await _names(session)
    out = []
    for pair, row in (await session.execute(q)).all():
        existing = await session.get(Transaction, pair.txn_a_id)
        out.append(
            ImportPairOut(
                id=pair.id,
                status=pair.status,
                score=pair.score,
                reasons=pair.reasons,
                ai_probability=pair.ai_probability,
                ai_reason=pair.ai_reason,
                row=_row_out(row, accounts, categories),
                existing=_brief(existing),
            )
        )
    return out


@router.post("/imports/{batch_id}/rows/{row_id}/decision", response_model=ImportRowOut)
async def decide_row(batch_id: int, row_id: int, body: DecisionIn, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    if b.status != "review":
        raise HTTPException(409, f"Import is {b.status}")
    row = await session.get(ImportRow, row_id)
    if row is None or row.batch_id != batch_id:
        raise HTTPException(404, "Row not found")
    if row.decision == "invalid":
        raise HTTPException(409, "Invalid rows cannot be imported")
    row.decision = body.decision
    await session.commit()
    accounts, categories = await _names(session)
    return _row_out(row, accounts, categories)


@router.post("/imports/{batch_id}/rows/decisions", response_model=BatchDetail)
async def decide_rows(batch_id: int, body: BulkDecisionIn, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    if b.status != "review":
        raise HTTPException(409, f"Import is {b.status}")
    await session.execute(
        update(ImportRow)
        .where(ImportRow.batch_id == batch_id, id_in(ImportRow.id, body.row_ids), ImportRow.decision != "invalid")
        .values(decision=body.decision)
    )
    await session.commit()
    return await _detail(session, await _batch(session, batch_id))


@router.post("/imports/{batch_id}/commit", response_model=BatchDetail)
async def commit_import(batch_id: int, body: CommitIn, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    try:
        result = await commit_batch(session, b, body.pending_as)
    except ImportError_ as exc:
        raise HTTPException(409, str(exc)) from None
    if result["uncategorized_ids"] and get_settings().gemini_key:
        await enqueue(session, "categorize", {"ids": result["uncategorized_ids"]})
    bf = await session.scalar(select(BackfillFile).where(BackfillFile.import_batch_id == b.id).limit(1))
    if bf and bf.status == "review":
        mark_done(bf, result)
    await enqueue(session, "detect_anomalies", {})
    await session.commit()
    notify_worker()
    return await _detail(session, await _batch(session, batch_id))


@router.post("/imports/{batch_id}/rollback", response_model=BatchDetail)
async def rollback_import(batch_id: int, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    try:
        await rollback_batch(session, b)
    except ImportError_ as exc:
        raise HTTPException(409, str(exc)) from None
    await session.commit()
    return await _detail(session, await _batch(session, batch_id))


async def _checks_out(session: AsyncSession, b: ImportBatch, checks: list[dict]) -> list[StatementCheckOut]:
    names = await account_names(session, [c["account_id"] for c in checks if c["account_id"]])
    return [
        StatementCheckOut(
            **c,
            import_batch_id=b.id,
            account_name=names.get(c["account_id"]),
            filename=b.attachment.filename if b.attachment else None,
        )
        for c in checks
    ]


@router.get("/imports/{batch_id}/checks", response_model=list[StatementCheckOut])
async def batch_checks(batch_id: int, session: AsyncSession = Depends(get_session)):
    """Compare the statement with the ledger for each of its accounts (recomputed on every call)."""
    b = await _batch(session, batch_id)
    checks = await save_checks(session, b)
    await session.commit()
    return await _checks_out(session, b, checks)


@router.post("/imports/{batch_id}/checks/fix")
async def fix_batch_checks(batch_id: int, body: CheckFixIn, session: AsyncSession = Depends(get_session)):
    b = await _batch(session, batch_id)
    if b.source_type != "document" or b.status not in ("review", "committed"):
        raise HTTPException(409, "Only reviewed or committed statements can be corrected")
    result = await apply_fixes(session, b, [f.model_dump() for f in body.fixes])
    if result["applied"] and b.status == "committed":
        if result["new_ids"] and get_settings().gemini_key:
            uncategorized = (
                await session.scalars(
                    select(Transaction.id).where(id_in(Transaction.id, result["new_ids"]), Transaction.category_id.is_(None))
                )
            ).all()
            if uncategorized:
                await enqueue(session, "categorize", {"ids": list(uncategorized)})
        await enqueue(session, "detect_anomalies", {})
    await session.commit()
    notify_worker()
    b = await _batch(session, batch_id)
    return {
        "applied": result["applied"],
        "skipped": result["skipped"],
        "checks": await _checks_out(session, b, result["checks"]),
    }


CHECK_JOB = "statement_checks"


def _confident(i: dict) -> bool:
    return bool(i["fix"] and i["suggested"] and i.get("confidence", 0) >= AUTO_CONFIDENCE)


@router.get("/statement-checks")
async def list_statement_checks(
    status: str | None = None, account_id: int | None = None, session: AsyncSession = Depends(get_session)
):
    """Stored check results for committed statements, problems first; issue lists are summarized."""
    base = (
        select(StatementCheck, Attachment.filename, Account.name)
        .join(ImportBatch, ImportBatch.id == StatementCheck.import_batch_id)
        .outerjoin(Attachment, Attachment.id == ImportBatch.attachment_id)
        .outerjoin(Account, Account.id == StatementCheck.account_id)
        .where(ImportBatch.status == "committed")
    )
    if account_id:
        base = base.where(StatementCheck.account_id == account_id)
    summary = dict(
        (
            await session.execute(
                select(StatementCheck.status, func.count())
                .join(ImportBatch, ImportBatch.id == StatementCheck.import_batch_id)
                .where(ImportBatch.status == "committed")
                .group_by(StatementCheck.status)
            )
        ).all()
    )
    q = base.where(StatementCheck.status == status) if status else base
    q = q.order_by(StatementCheck.period_start.desc().nulls_last(), StatementCheck.id)
    items = []
    for c, filename, name in (await session.execute(q)).all():
        issues = c.detail.get("issues", [])
        kinds: dict[str, int] = {}
        for i in issues:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        items.append(
            {
                "import_batch_id": c.import_batch_id,
                "account_ref": c.account_ref,
                "account_id": c.account_id,
                "account_name": name,
                "filename": filename,
                "period_start": c.period_start,
                "period_end": c.period_end,
                "statement_total": c.statement_total,
                "ledger_total": c.ledger_total,
                "difference": c.difference,
                "statement_rows": c.statement_rows,
                "ledger_rows": c.ledger_rows,
                "status": c.status,
                "trusted": c.trusted,
                "issue_counts": kinds,
                "fixes": sum(1 for i in issues if i["kind"] in ACTIONABLE or i["fix"] == "date"),
                "confident": sum(1 for i in issues if _confident(i)),
                "message": c.detail.get("message"),
                "checked_at": c.checked_at,
            }
        )
    job = await session.scalar(select(Job).where(Job.type == CHECK_JOB).order_by(Job.id.desc()).limit(1))
    unchecked = await session.scalar(
        select(func.count())
        .select_from(ImportBatch)
        .where(
            ImportBatch.source_type == "document",
            ImportBatch.status == "committed",
            ~select(StatementCheck.id).where(StatementCheck.import_batch_id == ImportBatch.id).exists(),
        )
    )
    statements = await session.scalar(
        select(func.count(func.distinct(StatementCheck.import_batch_id)))
        .join(ImportBatch, ImportBatch.id == StatementCheck.import_batch_id)
        .where(ImportBatch.status == "committed")
    )
    confident = await session.scalar(
        text(
            """--sql
            SELECT count(*)
            FROM statement_check c
            JOIN import_batch b ON b.id = c.import_batch_id AND b.status = 'committed'
            CROSS JOIN LATERAL jsonb_array_elements(c.detail -> 'issues') i
            WHERE i ->> 'fix' IS NOT NULL AND (i ->> 'suggested')::boolean
              AND coalesce((i ->> 'confidence')::int, 0) >= :min
            """
        ),
        {"min": AUTO_CONFIDENCE},
    )
    return {
        "summary": summary,
        "statements": statements,
        "unchecked": unchecked,
        "confident": confident,
        "auto_confidence": AUTO_CONFIDENCE,
        "job": JobOut.model_validate(job) if job else None,
        "items": items,
    }


@router.post("/statement-checks/run", response_model=JobOut, status_code=202)
async def run_statement_checks(apply: bool = False, session: AsyncSession = Depends(get_session)):
    """Re-check every committed statement; `apply` also applies the fixes confident enough to need no review."""
    busy = await session.scalar(select(Job.id).where(Job.type == CHECK_JOB, Job.status.in_(["queued", "running"])))
    if busy:
        raise HTTPException(409, "Statement checks are already running")
    job = await enqueue(session, CHECK_JOB, {"apply": apply})
    await session.commit()
    notify_worker()
    await session.refresh(job)
    return job


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


@router.get("/attachments/{attachment_id}/content", tags=["attachments"])
async def attachment_content(attachment_id: int, download: bool = False, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(
            select(Attachment.filename, Attachment.mime_type, Attachment.content).where(Attachment.id == attachment_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Attachment not found")
    name = _SAFE_NAME.sub("_", row.filename) or "file"
    disposition = "attachment" if download else "inline"
    return Response(
        content=row.content,
        media_type=row.mime_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )
