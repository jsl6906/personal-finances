import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.alerts.mailer import smtp_configured
from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.jobs import worker as jobs
from ledger.models import AiCallLog

public = APIRouter(tags=["system"])
router = APIRouter(tags=["system"])

# file name -> query; views keep names instead of ids where that reads better in a spreadsheet
EXPORTS = {
    "transactions.csv": "SELECT * FROM v_transactions ORDER BY date, id",
    "accounts.csv": """--sql
        SELECT a.id, a.name, a.account_type, i.name AS institution, a.mask, a.is_hidden, a.is_closed, a.notes
        FROM account a LEFT JOIN institution i ON i.id = a.institution_id ORDER BY a.id""",
    "categories.csv": "SELECT * FROM v_categories ORDER BY category_group, category",
    "tags.csv": "SELECT id, name, color FROM tag ORDER BY name",
    "budgets.csv": "SELECT * FROM v_budgets",
    "statements.csv": "SELECT * FROM v_statements ORDER BY series, period_start",
    "statement_usage.csv": "SELECT * FROM v_statement_usage ORDER BY series, period_start",
    "balances.csv": "SELECT * FROM v_balances ORDER BY account, as_of",
    "holdings.csv": "SELECT * FROM v_holdings ORDER BY account, as_of",
    "merchant_rules.csv": """--sql
        SELECT r.merchant, c.name AS category, r.source, r.hits
        FROM merchant_rule r JOIN category c ON c.id = r.category_id""",
    "spread_rules.csv": "SELECT name, merchant_pattern, min_amount, months, is_active FROM spread_rule",
}


@lru_cache
def _head() -> str:
    from ledger.cli import head_revision

    return head_revision()


@public.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    s = get_settings()
    body = {
        "db": "ok",
        "schema": None,
        "schema_current": False,
        "worker": jobs.worker is not None,
        "ai_configured": s.gemini_key is not None,
        "smtp_configured": smtp_configured(),
    }
    try:
        body["schema"] = await session.scalar(text("SELECT version_num FROM alembic_version"))
        body["schema_current"] = body["schema"] == _head()
    except Exception as exc:
        body["db"] = f"error: {type(exc).__name__}"
        return JSONResponse(body, status_code=503)
    return body


@router.get("/export.zip")
async def export(session: AsyncSession = Depends(get_session)):
    """Every table as CSV (documents stay in the database; Azure keeps point-in-time backups of both)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, sql in EXPORTS.items():
            result = await session.execute(text(sql))
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(result.keys())
            writer.writerows(result.all())
            zf.writestr(name, out.getvalue())
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="ledger-export-{stamp}.zip"'},
    )


@router.get("/ai-usage")
async def ai_usage(days: int = Query(30, ge=1, le=365), session: AsyncSession = Depends(get_session)):
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await session.execute(
        select(
            AiCallLog.purpose,
            AiCallLog.model,
            func.count(),
            func.count().filter(AiCallLog.ok.is_(False)),
            func.coalesce(func.sum(AiCallLog.input_tokens), 0),
            func.coalesce(func.sum(AiCallLog.output_tokens), 0),
            func.avg(AiCallLog.latency_ms),
        )
        .where(AiCallLog.created_at >= since)
        .group_by(AiCallLog.purpose, AiCallLog.model)
        .order_by(func.count().desc())
    )
    items = [
        {
            "purpose": p,
            "model": m,
            "calls": n,
            "errors": e,
            "input_tokens": int(i),
            "output_tokens": int(o),
            "avg_latency_ms": round(float(lat)) if lat is not None else None,
        }
        for p, m, n, e, i, o, lat in rows
    ]
    last_error = await session.scalar(
        select(AiCallLog.error).where(AiCallLog.ok.is_(False), AiCallLog.created_at >= since).order_by(AiCallLog.id.desc())
    )
    return {"days": days, "items": items, "last_error": (last_error or "")[:500] or None}
