from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import BackfillFile, ImportBatch, Job
from ledger.models.backfill import BILL_KINDS, STATEMENT_KINDS
from ledger.schemas import JobOut
from ledger.sources import archive, backfill
from ledger.sources.google import service_account_email
from ledger.sources.jobs import backfill_job_active, backfill_watchdog

router = APIRouter(prefix="/backfill", tags=["backfill"])
KINDS = (*STATEMENT_KINDS, *BILL_KINDS, "spreadsheet")


@router.get("")
async def overview(session: AsyncSession = Depends(get_session)):
    running = await session.scalar(
        select(Job)
        .where(Job.type.in_(("backfill_run", "backfill_scan")), Job.status.in_(("queued", "running")))
        .order_by(Job.id.desc())
        .limit(1)
    )
    return {
        "settings": await backfill.load_settings(session),
        "summary": await backfill.summary(session),
        "running": await backfill_job_active(session),
        "google_service_account": service_account_email(),
        "inbox_configured": get_settings().inbox_dir is not None,
        "active_job": {"id": running.id, "type": running.type, "message": running.message} if running else None,
    }


class SettingsIn(BaseModel):
    provider: Literal["drive", "local"]
    folder: str = Field("", max_length=500)
    auto_approve_bills: bool = True


@router.put("/settings")
async def update_settings(body: SettingsIn, session: AsyncSession = Depends(get_session)):
    try:
        if body.provider == "drive":
            folder_id = archive.parse_folder_id(body.folder)
            values = {"provider": "drive", "folder_id": folder_id, "folder_name": await archive.drive_folder_name(folder_id)}
        else:
            archive.resolve_local(body.folder)
            values = {"provider": "local", "local_path": body.folder.strip(), "folder_name": body.folder.strip() or "inbox"}
    except archive.ArchiveError as exc:
        raise HTTPException(422, str(exc)) from None
    cfg = await backfill.save_settings(session, auto_approve_bills=body.auto_approve_bills, **values)
    await session.commit()
    return cfg


async def _job(session: AsyncSession, job_type: str) -> Job:
    job = await enqueue(session, job_type, {})
    await session.commit()
    notify_worker()
    await session.refresh(job)
    return job


@router.post("/scan", response_model=JobOut, status_code=202)
async def start_scan(session: AsyncSession = Depends(get_session)):
    return await _job(session, "backfill_scan")


@router.post("/start")
async def start(session: AsyncSession = Depends(get_session)):
    await backfill.save_settings(session, paused=False, last_error=None)
    await session.commit()
    return {"paused": False, "job_id": await backfill_watchdog(session)}


@router.post("/pause")
async def pause(session: AsyncSession = Depends(get_session)):
    await backfill.save_settings(session, paused=True)
    await session.commit()
    return {"paused": True}


@router.get("/files")
async def list_files(
    status: list[str] | None = Query(None),
    limit: int = Query(50, le=500),
    session: AsyncSession = Depends(get_session),
):
    q = select(BackfillFile).order_by(BackfillFile.processed_at.desc().nulls_last(), BackfillFile.id).limit(limit)
    if status:
        q = q.where(BackfillFile.status.in_(status))
    return [
        {
            "id": f.id,
            "path": f.path,
            "name": f.name,
            "status": f.status,
            "kind": f.kind,
            "period_start": f.period_start,
            "message": f.message,
            "error": f.error,
            "classification": f.detail.get("classification"),
            "attachment_id": f.attachment_id,
            "import_batch_id": f.import_batch_id,
            "statement_id": f.statement_id,
            "processed_at": f.processed_at,
        }
        for f in (await session.scalars(q)).all()
    ]


async def _file(session: AsyncSession, fid: int) -> BackfillFile:
    f = await session.get(BackfillFile, fid)
    if f is None:
        raise HTTPException(404, "File not found")
    return f


class FileAction(BaseModel):
    action: Literal["retry", "skip", "set_kind"]
    kind: str | None = None


@router.post("/files/{fid}")
async def file_action(fid: int, body: FileAction, session: AsyncSession = Depends(get_session)):
    f = await _file(session, fid)
    if body.action == "skip":
        f.status, f.message = "skipped", "Skipped by you"
    elif body.action == "set_kind":
        if body.kind not in KINDS:
            raise HTTPException(422, f"Kind must be one of {', '.join(KINDS)}")
        if not f.attachment_id:
            raise HTTPException(409, "Retry the file first so it can be downloaded")
        f.kind, f.status, f.message, f.error = body.kind, "classified", None, None
    else:
        old = await session.get(ImportBatch, f.import_batch_id) if f.import_batch_id else None
        if old and old.status != "committed":
            await session.delete(old)
        f.status = "classified" if f.attachment_id and f.kind else "pending"
        f.error = f.message = None
        f.import_batch_id = f.statement_id = None
    await session.commit()
    return {"id": f.id, "status": f.status, "kind": f.kind}
