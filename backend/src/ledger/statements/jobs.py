from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import JobContext, job_handler
from ledger.models import Statement
from ledger.statements.service import extract_into_statement


@job_handler("extract_bill")
async def extract_bill_job(ctx: JobContext) -> dict:
    sid = ctx.payload["statement_id"]
    try:
        async with get_sessionmaker()() as session:
            st = await session.get(Statement, sid)
            await ctx.progress(0.1, "Reading the document with Gemini")
            result = await extract_into_statement(session, st)
            await session.commit()
            return result
    except Exception as exc:
        async with get_sessionmaker()() as session:
            st = await session.get(Statement, sid)
            if st:
                st.status, st.error = "failed", str(exc)[:2000]
                await session.commit()
        raise
