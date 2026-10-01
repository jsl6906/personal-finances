"""Historical backfill: scan an archive folder, classify each file with Gemini, then route it — statements and
spreadsheets to the import pipeline, bills to Bills & statements — committing whatever passes the checks and
flagging the rest for review. Files are imported oldest-first, bills after the transactions they pay."""

import hashlib
import logging
from collections import Counter
from datetime import UTC, datetime

from google.genai import errors as genai_errors
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.imports.parsing import detect_kind, mapping_complete, parse_date
from ledger.imports.service import (
    ImportError_,
    commit_batch,
    extract_into_batch,
    load_spreadsheet,
    prepare_batch,
    store_attachment,
)
from ledger.models import AppSetting, Attachment, BackfillFile, ImportBatch, Source, Statement, StatementSeries
from ledger.models.backfill import BILL_KINDS, STATEMENT_KINDS
from ledger.sources import archive
from ledger.sources.service import FeedAccount, auto_resolve, ensure_account

log = logging.getLogger(__name__)

SETTINGS_KEY = "backfill"
DEFAULTS = {
    "provider": "drive",
    "folder_id": None,
    "folder_name": None,
    "local_path": "",
    "paused": True,
    "auto_approve_bills": True,
    "last_scan": None,
    "last_error": None,
}
SKIP_KINDS = {
    "receipt": "Receipt",
    "tax_document": "Tax document",
    "annual_summary": "Annual summary (its transactions come from the statements)",
    "other": "Not a statement or bill",
}
MIN_CONFIDENCE = 0.6
FILES_PER_JOB = 20


async def load_settings(session: AsyncSession) -> dict:
    row = await session.get(AppSetting, SETTINGS_KEY)
    return {**DEFAULTS, **(row.value if row else {})}


async def save_settings(session: AsyncSession, **values) -> dict:
    cfg = {**await load_settings(session), **values}
    stmt = insert(AppSetting).values(key=SETTINGS_KEY, value=cfg)
    await session.execute(stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": stmt.excluded.value}))
    return cfg


async def scan(session: AsyncSession) -> dict:
    cfg = await load_settings(session)
    files = await archive.list_files(cfg)
    provider = "local" if cfg["provider"] == "local" else "drive"
    known = set((await session.scalars(select(BackfillFile.external_id).where(BackfillFile.provider == provider))).all())
    tiller_sheet = await session.scalar(select(Source.config["sheet_id"].astext).where(Source.kind == "tiller"))
    new = unsupported = 0
    for f in files:
        if f.external_id in known:
            continue
        ok = f.supported
        new += 1
        unsupported += 0 if ok else 1
        message = None if ok else "Unsupported file type"
        if tiller_sheet and f.external_id == tiller_sheet:
            ok, message = False, "This is the Tiller sheet, which syncs directly"
        await session.execute(
            insert(BackfillFile)
            .values(
                provider=provider,
                external_id=f.external_id[:500],
                path=f.path[:1000],
                name=f.name[:300],
                mime_type=f.mime_type,
                size_bytes=f.size_bytes,
                modified_at=f.modified_at,
                status="pending" if ok else "skipped",
                message=message,
            )
            .on_conflict_do_nothing()
        )
    result = {"found": len(files), "new": new, "unsupported": unsupported, "at": datetime.now(UTC).isoformat()}
    await save_settings(session, last_scan=result)
    await session.commit()
    return result


def _label(kind: str | None) -> str:
    return (kind or "file").replace("_", " ")


async def _already_imported(session: AsyncSession, bf: BackfillFile, att: Attachment) -> str | None:
    other = await session.scalar(
        select(BackfillFile).where(BackfillFile.attachment_id == att.id, BackfillFile.id != bf.id).limit(1)
    )
    if other:
        return f"Same content as {'/'.join(p for p in (other.path, other.name) if p)}"
    if await session.scalar(
        select(ImportBatch.id).where(ImportBatch.attachment_id == att.id, ImportBatch.status == "committed").limit(1)
    ):
        return "Already imported from an upload"
    if await session.scalar(select(Statement.id).where(Statement.attachment_id == att.id).limit(1)):
        return "Already filed under Bills & statements"
    return None


async def classify(session: AsyncSession, bf: BackfillFile) -> None:
    from ledger.ai.backfill import classify_document

    data = await archive.download(bf.provider, bf.external_id, bf.mime_type)
    bf.detail = {**bf.detail, "sha256": hashlib.sha256(data).hexdigest()}
    att = await store_attachment(session, data, bf.name, None, source="backfill")
    dup = await _already_imported(session, bf, att)
    bf.attachment_id = att.id
    if dup:
        bf.status, bf.message = "skipped", dup
        return
    if detect_kind(bf.name) == "spreadsheet":
        bf.kind, bf.status = "spreadsheet", "classified"
        return
    c = await classify_document(data, att.mime_type, bf.name, bf.path)
    bf.kind = c.document_type
    bf.period_start = parse_date(c.period_start) or parse_date(c.period_end)
    bf.detail = {**bf.detail, "classification": c.model_dump()}
    if c.confidence < MIN_CONFIDENCE:
        bf.status, bf.message = "review", f"Unclear document ({c.reason})"
    elif c.document_type in SKIP_KINDS:
        bf.status, bf.message = "skipped", f"{SKIP_KINDS[c.document_type]}: {c.reason}"
    else:
        bf.status = "classified"
        bf.message = (
            " · ".join(x for x in (c.institution, f"···{c.account_last4}" if c.account_last4 else None) if x) or None
        )


def mark_done(bf: BackfillFile, stats: dict) -> None:
    bf.status = "done"
    bf.detail = {**bf.detail, "inserted": stats["inserted"], "skipped_duplicates": stats["skipped_duplicates"]}
    bf.message = f"{stats['inserted']} transactions added" + (
        f", {stats['skipped_duplicates']} duplicates skipped" if stats["skipped_duplicates"] else ""
    )


async def _finish_batch(session: AsyncSession, bf: BackfillFile, batch: ImportBatch, gate: str | None) -> list[int]:
    """Commit a prepared batch unless a check needs a person; returns uncategorized transaction ids."""
    ambiguous = await auto_resolve(session, batch)
    if ambiguous:
        gate = gate or f"{ambiguous} possible duplicate{'s' if ambiguous > 1 else ''} to decide"
    if gate:
        bf.status, bf.message = "review", gate
        return []
    stats = await commit_batch(session, batch)
    mark_done(bf, stats)
    return stats["uncategorized_ids"]


def _account_label(a: dict) -> str:
    return f"···{a['last4']}" if a.get("last4") else a["ref"]


async def _resolve_accounts(session: AsyncSession, batch: ImportBatch) -> None:
    """Link every account on the statement to a ledger account, creating ones the ledger hasn't seen."""
    meta = batch.doc_meta or {}
    inst = meta.get("institution")
    accounts = meta.get("accounts") or []
    account_map = dict(batch.defaults.get("account_map") or {})
    for a in accounts:
        if account_map.get(a["ref"]) or not (inst and a.get("last4")):
            continue
        acct = await ensure_account(
            session,
            "statement",
            FeedAccount(
                key=f"{inst}|{a['last4']}".lower(),
                name=" ".join(x for x in (inst, a.get("name"), f"···{a['last4']}") if x),
                institution=inst,
                mask=a["last4"],
                type_hint=a.get("account_type"),
            ),
        )
        account_map[a["ref"]] = acct.id
    defaults = {**batch.defaults, "account_map": account_map}
    if len(accounts) == 1 and not defaults.get("account_id"):
        defaults["account_id"] = account_map.get(accounts[0]["ref"])
    batch.defaults = defaults


async def _import_statement(session: AsyncSession, bf: BackfillFile, att: Attachment) -> list[int]:
    batch = ImportBatch(attachment_id=att.id, source_type="document", origin="backfill", status="extracting")
    batch.attachment = att
    session.add(batch)
    await session.flush()
    bf.import_batch_id = batch.id
    await extract_into_batch(session, batch)
    await _resolve_accounts(session, batch)
    await prepare_batch(session, batch)
    meta = batch.doc_meta or {}
    accounts = meta.get("accounts") or []
    off = [a for a in accounts if a.get("reconciliation") and not a["reconciliation"]["reconciles"]]
    recon = meta.get("reconciliation")
    gate = None
    if off:
        r = off[0]["reconciliation"]
        gate = f"{_account_label(off[0])} rows sum to {r['sum_of_rows']} but its balance changed by {r['balance_change']}"
    elif recon and not recon.get("reconciles"):
        gate = f"Rows sum to {recon['sum_of_rows']} but balances changed by {recon['balance_change']}"
    elif meta.get("low_confidence_rows"):
        gate = f"{meta['low_confidence_rows']} hard-to-read rows to check"
    elif batch.stats.get("no_account"):
        missing = [_account_label(a) for a in accounts if not batch.defaults["account_map"].get(a["ref"])]
        gate = f"Pick the account for {', '.join(missing)}" if missing else "Pick the account for this statement"
    return await _finish_batch(session, bf, batch, gate)


async def _import_spreadsheet(session: AsyncSession, bf: BackfillFile, att: Attachment) -> list[int]:
    data = await session.scalar(select(Attachment.content).where(Attachment.id == att.id))
    batch = ImportBatch(attachment_id=att.id, source_type="spreadsheet", origin="backfill")
    batch.attachment = att
    session.add(batch)
    await session.flush()
    bf.import_batch_id = batch.id
    await load_spreadsheet(session, batch, data, att.filename, None)
    if len(batch.sheets) > 1:
        bf.detail = {**bf.detail, "sheets": batch.sheets}
    if not mapping_complete(batch.mapping):
        bf.status, bf.message = "review", "Map the columns to finish this import"
        return []
    if "account" not in batch.mapping.values() and not batch.defaults.get("account_id"):
        # A transaction export filed next to statements belongs to the statements' account.
        defaults = (
            await session.scalars(
                select(ImportBatch.defaults)
                .join(BackfillFile, BackfillFile.import_batch_id == ImportBatch.id)
                .where(BackfillFile.path == bf.path, BackfillFile.kind.in_(STATEMENT_KINDS))
            )
        ).all()
        accounts = Counter(d.get("account_id") for d in defaults if d.get("account_id"))
        if accounts:
            batch.defaults = {**batch.defaults, "account_id": accounts.most_common(1)[0][0]}
    await prepare_batch(session, batch)
    gate = f"Only the first of {len(batch.sheets)} sheets was imported" if len(batch.sheets) > 1 else None
    return await _finish_batch(session, bf, batch, gate)


async def _import_bill(session: AsyncSession, bf: BackfillFile, att: Attachment, auto_approve: bool) -> None:
    from ledger.schemas import StatementApprove
    from ledger.statements.service import approve_statement, extract_into_statement

    st = Statement(attachment_id=att.id, suggestion={}, usage=[])
    st.attachment = att
    session.add(st)
    await session.flush()
    bf.statement_id = st.id
    await extract_into_statement(session, st)
    if not auto_approve:
        bf.status, bf.message = "review", "Waiting for approval in Bills & statements"
        return
    ids = st.suggestion.get("transaction_ids", [])
    await approve_statement(session, st, StatementApprove(transaction_ids=ids))
    series = await session.get(StatementSeries, st.series_id)
    bf.status = "done"
    bf.message = f"Filed under {series.name}" + (" and linked to its payment" if ids else "; no matching payment found")


async def import_file(session: AsyncSession, bf: BackfillFile, cfg: dict) -> list[int]:
    att = await session.get(Attachment, bf.attachment_id) if bf.attachment_id else None
    if att is None:
        raise ImportError_("The downloaded file is missing; retry the file")
    if bf.kind in STATEMENT_KINDS:
        return await _import_statement(session, bf, att)
    if bf.kind == "spreadsheet":
        return await _import_spreadsheet(session, bf, att)
    if bf.kind in BILL_KINDS:
        await _import_bill(session, bf, att, cfg["auto_approve_bills"])
        return []
    bf.status, bf.message = "skipped", f"Nothing to import for a {_label(bf.kind)}"
    return []


async def next_file(session: AsyncSession) -> BackfillFile | None:
    pending = await session.scalar(
        select(BackfillFile).where(BackfillFile.status == "pending").order_by(BackfillFile.path, BackfillFile.name).limit(1)
    )
    if pending:
        return pending
    # Import transactions before the bills that link to them, oldest first.
    bills_last = case((BackfillFile.kind.in_(BILL_KINDS), 1), else_=0)
    return await session.scalar(
        select(BackfillFile)
        .where(BackfillFile.status == "classified")
        .order_by(bills_last, BackfillFile.period_start.asc().nulls_last(), BackfillFile.path, BackfillFile.name)
        .limit(1)
    )


def _is_quota_error(exc: Exception) -> bool:
    return isinstance(exc, genai_errors.APIError) and exc.code in (429, 503)


async def process_next(session: AsyncSession) -> dict | None:
    """Classify or import one file; returns None when nothing is left."""
    from ledger.jobs.worker import enqueue

    bf = await next_file(session)
    if bf is None:
        return None
    file_id, stage = bf.id, bf.status
    cfg = await load_settings(session)
    try:
        if stage == "pending":
            await classify(session, bf)
        else:
            uncategorized = await import_file(session, bf, cfg)
            if uncategorized:
                await enqueue(session, "categorize", {"ids": uncategorized})
        bf.error, bf.processed_at = None, datetime.now(UTC)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        if _is_quota_error(exc):
            await save_settings(session, paused=True, last_error=f"Paused: Gemini unavailable ({exc.code}); resume later")
            await session.commit()
            raise
        log.exception("Backfill of file %s failed", file_id)
        bf = await session.get(BackfillFile, file_id, populate_existing=True)
        bf.status, bf.error, bf.processed_at = "failed", str(exc)[:1000], datetime.now(UTC)
        await session.commit()
    return {"id": file_id, "stage": stage, "status": bf.status}


async def summary(session: AsyncSession) -> dict:
    by_status = dict((await session.execute(select(BackfillFile.status, func.count()).group_by(BackfillFile.status))).all())
    by_kind = {
        k or "unclassified": {"total": total, "done": done, "flagged": flagged, "skipped": skipped, "queued": queued}
        for k, total, done, flagged, skipped, queued in await session.execute(
            select(
                BackfillFile.kind,
                func.count(),
                func.count().filter(BackfillFile.status == "done"),
                func.count().filter(BackfillFile.status.in_(("review", "failed"))),
                func.count().filter(BackfillFile.status == "skipped"),
                func.count().filter(BackfillFile.status.in_(("pending", "classified"))),
            ).group_by(BackfillFile.kind)
        )
    }
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(BackfillFile.detail["inserted"].as_integer()), 0),
                func.coalesce(func.sum(BackfillFile.detail["skipped_duplicates"].as_integer()), 0),
                func.min(BackfillFile.period_start),
                func.max(BackfillFile.period_start),
            )
        )
    ).one()
    total = sum(by_status.values())
    finished = sum(by_status.get(s, 0) for s in ("done", "skipped", "review", "failed"))
    return {
        "total": total,
        "processed": finished,
        "by_status": by_status,
        "by_kind": by_kind,
        "transactions_added": totals[0],
        "duplicates_skipped": totals[1],
        "earliest": totals[2],
        "latest": totals[3],
    }
