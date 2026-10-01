import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import get_settings
from ledger.db.engine import get_sessionmaker
from ledger.models import Job

log = logging.getLogger(__name__)

Handler = Callable[["JobContext"], Awaitable[dict[str, Any] | None]]
HANDLERS: dict[str, Handler] = {}
MAX_ATTEMPTS = 3


class JobCancelled(Exception):
    pass


def job_handler(job_type: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return register


@dataclass
class JobContext:
    job_id: int
    type: str
    payload: dict[str, Any]

    async def progress(self, fraction: float, message: str | None = None) -> None:
        async with get_sessionmaker()() as session:
            status = await session.scalar(select(Job.status).where(Job.id == self.job_id))
            if status == "cancelled":
                raise JobCancelled()
            await session.execute(
                update(Job).where(Job.id == self.job_id).values(progress=max(0.0, min(1.0, fraction)), message=message)
            )
            await session.commit()


async def enqueue(session: AsyncSession, job_type: str, payload: dict[str, Any] | None = None) -> Job:
    if job_type not in HANDLERS:
        raise ValueError(f"Unknown job type {job_type}")
    job = Job(type=job_type, payload=payload or {})
    session.add(job)
    await session.flush()
    return job


_CLAIM_SQL = text(
    """--sql
    UPDATE job SET status = 'running', started_at = now(), attempts = attempts + 1
    WHERE id = (
        SELECT id FROM job WHERE status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
    )
    RETURNING id, type, payload
    """
)


class Worker:
    def __init__(self) -> None:
        settings = get_settings()
        self._slots = asyncio.Semaphore(settings.job_concurrency)
        self._poll = settings.job_poll_seconds
        self._task: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()
        self._wake = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="job-worker")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in list(self._running):
            t.cancel()
        await asyncio.gather(*self._running, return_exceptions=True)

    async def _recover(self) -> None:
        # Jobs left 'running' by a previous process were interrupted by a restart.
        async with get_sessionmaker()() as session:
            await session.execute(
                update(Job).where(Job.status == "running", Job.attempts < MAX_ATTEMPTS).values(status="queued")
            )
            await session.execute(
                update(Job)
                .where(Job.status == "running")
                .values(status="failed", error="Interrupted too many times", finished_at=datetime.now(UTC))
            )
            await session.commit()

    async def _loop(self) -> None:
        try:
            await self._recover()
        except Exception:
            log.exception("Job recovery failed")
        while True:
            try:
                await self._slots.acquire()
                claimed = await self._claim()
                if claimed is None:
                    self._slots.release()
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self._poll)
                    except TimeoutError:
                        pass
                    continue
                task = asyncio.create_task(self._run(*claimed))
                self._running.add(task)
                task.add_done_callback(self._running.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._slots.release()
                log.exception("Job worker loop error")
                await asyncio.sleep(self._poll * 5)

    async def _claim(self) -> tuple[int, str, dict] | None:
        async with get_sessionmaker()() as session:
            row = (await session.execute(_CLAIM_SQL)).first()
            await session.commit()
            return (row.id, row.type, row.payload) if row else None

    async def _finish(self, job_id: int, **values: Any) -> None:
        async with get_sessionmaker()() as session:
            await session.execute(
                update(Job).where(Job.id == job_id, Job.status == "running").values(finished_at=datetime.now(UTC), **values)
            )
            await session.commit()

    async def _run(self, job_id: int, job_type: str, payload: dict) -> None:
        try:
            handler = HANDLERS.get(job_type)
            if handler is None:
                await self._finish(job_id, status="failed", error=f"No handler for {job_type}")
                return
            log.info("Job %s (%s) started", job_id, job_type)
            result = await handler(JobContext(job_id, job_type, payload))
            await self._finish(job_id, status="succeeded", progress=1, result=result or {})
            log.info("Job %s (%s) succeeded", job_id, job_type)
        except JobCancelled:
            log.info("Job %s cancelled", job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Job %s (%s) failed", job_id, job_type)
            frames = "".join(traceback.format_tb(exc.__traceback__))[-6000:]
            await self._finish(job_id, status="failed", error=f"{repr(exc)[:1500]}\n{frames}")
        finally:
            self._slots.release()


worker: Worker | None = None


def notify_worker() -> None:
    if worker is not None:
        worker.wake()
