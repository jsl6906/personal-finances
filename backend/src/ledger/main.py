import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import ledger.ai.categorize  # noqa: F401  (registers job handler)
import ledger.ai.merchant_review  # noqa: F401  (registers job handler)
import ledger.analytics.jobs  # noqa: F401  (registers job handlers)
import ledger.imports.jobs  # noqa: F401  (registers job handlers)
import ledger.maintenance  # noqa: F401  (registers job handlers)
import ledger.sources.jobs  # noqa: F401  (registers job handlers)
import ledger.statements.jobs  # noqa: F401  (registers job handlers)
from ledger import auth
from ledger.api import (
    alerts,
    analytics,
    backfill,
    budgets,
    chat,
    details,
    duplicates,
    imports,
    merchant_review,
    misc,
    reference,
    rules,
    sources,
    statements,
    system,
    transactions,
)
from ledger.config import get_settings
from ledger.db.engine import dispose_engine
from ledger.jobs import worker as jobs
from ledger.jobs.scheduler import build_scheduler
from ledger.security import SecurityMiddleware

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.run_worker:
        jobs.worker = jobs.Worker()
        jobs.worker.start()
        scheduler = build_scheduler()
        scheduler.start()
    yield
    if jobs.worker:
        scheduler.shutdown(wait=False)
        await jobs.worker.stop()
        jobs.worker = None
    await dispose_engine()


app = FastAPI(title="Ledger", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret.get_secret_value(),
    session_cookie="ledger_session",
    max_age=settings.session_max_age_days * 86400,
    same_site="lax",
    https_only=settings.cookie_secure,
)
app.add_middleware(SecurityMiddleware)

protected = [Depends(auth.require_auth)]
app.include_router(misc.public, prefix="/api")
app.include_router(system.public, prefix="/api")
app.include_router(system.router, prefix="/api", dependencies=protected)
app.include_router(misc.router, prefix="/api", dependencies=protected)
app.include_router(reference.router, prefix="/api", dependencies=protected)
app.include_router(transactions.router, prefix="/api", dependencies=protected)
app.include_router(rules.router, prefix="/api", dependencies=protected)
app.include_router(imports.router, prefix="/api", dependencies=protected)
app.include_router(duplicates.router, prefix="/api", dependencies=protected)
app.include_router(statements.router, prefix="/api", dependencies=protected)
app.include_router(budgets.router, prefix="/api", dependencies=protected)
app.include_router(analytics.router, prefix="/api", dependencies=protected)
app.include_router(details.router, prefix="/api", dependencies=protected)
app.include_router(merchant_review.router, prefix="/api", dependencies=protected)
app.include_router(chat.router, prefix="/api", dependencies=protected)
app.include_router(alerts.router, prefix="/api", dependencies=protected)
app.include_router(sources.router, prefix="/api", dependencies=protected)
app.include_router(backfill.router, prefix="/api", dependencies=protected)


def _mount_spa(static_dir: Path) -> None:
    index = static_dir / "index.html"
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


if settings.static_dir and (settings.static_dir / "index.html").is_file():
    _mount_spa(settings.static_dir)
