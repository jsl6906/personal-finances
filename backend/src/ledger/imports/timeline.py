"""One account's statements over time: committed ones with their check totals, ones still being imported, and
placeholders where the statement cadence says one is missing."""

from datetime import date, timedelta
from statistics import median

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.imports.parsing import parse_date
from ledger.models import Account, Attachment, BackfillFile, ImportBatch, StatementCheck, Transaction
from ledger.models.backfill import STATEMENT_KINDS

IN_PROGRESS = ("extracting", "mapping", "preparing", "review")
SAME_CLOSE_DAYS = 5  # statements ending this close together cover the same cycle
DEFAULT_CYCLE = 30


def _entry(kind: str, start: date | None, end: date | None, **kw) -> dict:
    return {
        "kind": kind,
        "period_start": start,
        "period_end": end,
        "import_batch_id": None,
        "backfill_file_id": None,
        "filename": None,
        "status": None,
        "statement_total": None,
        "ledger_total": None,
        "difference": None,
        "statement_rows": None,
        "ledger_rows": None,
        "note": None,
        "estimated": None,
        **kw,
    }


def _same_account(a: str, b: str) -> bool:
    a, b = "".join(ch for ch in a if ch.isdigit()), "".join(ch for ch in b if ch.isdigit())
    return min(len(a), len(b)) >= 3 and (a.endswith(b) or b.endswith(a))


async def account_timeline(session: AsyncSession, account_id: int) -> list[dict]:
    """Newest first. kind: statement (committed) | review (being imported) | queued (in the backfill) | missing."""
    acct = await session.get(Account, account_id)
    if acct is None:
        return []
    items: list[dict] = []
    listed: set[int] = set()
    checks = await session.execute(
        select(StatementCheck, ImportBatch.status, Attachment.filename)
        .join(ImportBatch, ImportBatch.id == StatementCheck.import_batch_id)
        .outerjoin(Attachment, Attachment.id == ImportBatch.attachment_id)
        .where(StatementCheck.account_id == account_id, ImportBatch.status.in_(("committed", *IN_PROGRESS)))
    )
    for c, status, filename in checks.all():
        listed.add(c.import_batch_id)
        items.append(
            _entry(
                "statement" if status == "committed" else "review",
                c.period_start,
                c.period_end,
                import_batch_id=c.import_batch_id,
                filename=filename,
                status=c.status if status == "committed" else status,
                statement_total=c.statement_total,
                ledger_total=c.ledger_total,
                difference=c.difference,
                statement_rows=c.statement_rows,
                ledger_rows=c.ledger_rows,
            )
        )

    # Imports not checked yet: matched by the account chosen for them.
    pending = (
        await session.execute(
            text(
                """--sql
                SELECT b.id
                FROM import_batch b
                WHERE b.source_type = 'document' AND b.status = ANY(:statuses)
                  AND (b.defaults ->> 'account_id' = :acct
                       OR EXISTS (
                         SELECT 1 FROM jsonb_each_text(CASE WHEN jsonb_typeof(b.defaults -> 'account_map') = 'object'
                                                            THEN b.defaults -> 'account_map' ELSE '{}'::jsonb END) m
                         WHERE m.value = :acct)
                       OR EXISTS (SELECT 1 FROM import_row r WHERE r.batch_id = b.id AND r.account_id = :acct_id))
                """
            ),
            {"statuses": list(IN_PROGRESS), "acct": str(account_id), "acct_id": account_id},
        )
    ).scalars()
    for bid in [b for b in pending if b not in listed]:
        b = await session.get(ImportBatch, bid)
        meta = b.doc_meta or {}
        listed.add(bid)
        items.append(
            _entry(
                "review",
                parse_date(meta.get("period_start")),
                parse_date(meta.get("period_end")),
                import_batch_id=bid,
                filename=b.attachment.filename if b.attachment else None,
                status=b.status,
            )
        )

    # Backfill files classified as this account's statements but not imported yet.
    if acct.mask:
        files = await session.scalars(
            select(BackfillFile).where(
                BackfillFile.status.in_(("classified", "review")),
                BackfillFile.kind.in_(STATEMENT_KINDS),
                BackfillFile.detail["classification"]["account_last4"].astext.is_not(None),
            )
        )
        for f in files.all():
            c = f.detail["classification"]
            if f.import_batch_id in listed or not _same_account(c["account_last4"], acct.mask):
                continue
            items.append(
                _entry(
                    "queued",
                    parse_date(c.get("period_start")),
                    parse_date(c.get("period_end")),
                    backfill_file_id=f.id,
                    filename=f.name,
                    status=f.status,
                    note=f.message,
                )
            )

    items += await _gaps(session, acct, items)
    items.sort(key=lambda i: i["period_end"] or i["period_start"] or date.min, reverse=True)
    return items


async def _gaps(session: AsyncSession, acct: Account, items: list[dict]) -> list[dict]:
    """Placeholders for cycles no statement covers, judged by the spacing of the statements' closing dates."""
    known = sorted((i for i in items if i["period_end"]), key=lambda i: i["period_end"])
    if not known:
        return []
    cycles: list[list[dict]] = []
    for i in known:
        if cycles and (i["period_end"] - cycles[-1][-1]["period_end"]).days <= SAME_CLOSE_DAYS:
            cycles[-1].append(i)
        else:
            cycles.append([i])
    ends = [c[-1]["period_end"] for c in cycles]
    spacing = [(b - a).days for a, b in zip(ends, ends[1:], strict=False)]
    cycle = max(7, median(spacing)) if spacing else DEFAULT_CYCLE
    out = []
    for prev, nxt in zip(cycles, cycles[1:], strict=False):
        last, days = prev[-1]["period_end"], (nxt[-1]["period_end"] - prev[-1]["period_end"]).days
        if days <= 1.5 * cycle:
            continue
        starts = [i["period_start"] for i in nxt if i["period_start"] and i["period_start"] > last]
        end = min(starts) - timedelta(days=1) if starts else nxt[-1]["period_end"] - timedelta(days=round(cycle))
        out.append(_missing(last + timedelta(days=1), end, max(1, round(days / cycle) - 1)))
    if not acct.is_closed:
        last = ends[-1]
        latest = await session.scalar(
            select(func.max(Transaction.txn_date)).where(Transaction.account_id == acct.id, Transaction.deleted_at.is_(None))
        )
        # Activity past the next closing date means that statement exists somewhere.
        if latest and (latest - last).days > cycle:
            out.append(_missing(last + timedelta(days=1), latest, max(1, int((latest - last).days // cycle))))
    return out


def _missing(start: date, end: date, n: int) -> dict:
    note = f"About {n} statements" if n > 1 else "About 1 statement"
    return _entry("missing", start, max(start, end), estimated=n, note=f"{note} not uploaded")
