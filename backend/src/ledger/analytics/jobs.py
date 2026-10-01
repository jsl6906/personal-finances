from datetime import date

from ledger.alerts.service import evaluate, send_digest
from ledger.analytics.anomalies import detect, narrate
from ledger.budgets.service import add_months
from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import JobContext, job_handler


@job_handler("detect_anomalies")
async def detect_anomalies_job(ctx: JobContext) -> dict:
    today = date.today()
    this_month = date(today.year, today.month, 1)
    months = [date.fromisoformat(m) for m in ctx.payload.get("months", [])] or [add_months(this_month, -1), this_month]
    results = []
    async with get_sessionmaker()() as session:
        for i, m in enumerate(months):
            await ctx.progress(i / (len(months) + 1), f"Checking {m:%b %Y}")
            results.append(await detect(session, m))
        noted = await narrate(session)
        await ctx.progress(0.95, "Checking alert rules")
        alerts = await evaluate(session)
    return {"months": results, "ai_notes": noted, "alerts": alerts}


@job_handler("evaluate_alerts")
async def evaluate_alerts_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        return await evaluate(session)


@job_handler("weekly_digest")
async def weekly_digest_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        return await send_digest(session, force=bool(ctx.payload.get("force")))
