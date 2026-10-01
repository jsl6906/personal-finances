"""Out-of-norm spending detection (robust statistics; Gemini only adds a one-line note)."""

import logging
import statistics
from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.alerts.service import large_txn_threshold
from ledger.analytics.reports import FROM, REPORTABLE, monthly_category_history
from ledger.analytics.transfers import MATCH_WINDOW_DAYS, match_transfers
from ledger.budgets.service import add_months
from ledger.config import get_settings
from ledger.models import Anomaly, Category, Statement, StatementSeries

log = logging.getLogger(__name__)

MIN_SPIKE = Decimal("50")
MIN_MERCHANT = Decimal("50")
NEW_MERCHANT_MIN = Decimal("200")


def _money(v) -> str:
    return f"${Decimal(v):,.2f}"


def robust(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    """Median and scaled MAD (≈ σ for normal data); MAD floor keeps flat histories from flagging tiny changes."""
    med = Decimal(str(statistics.median(values)))
    mad = Decimal(str(statistics.median([abs(v - med) for v in values]))) * Decimal("1.4826")
    return med, max(mad, med * Decimal("0.15"), Decimal("5"))


async def _category_spikes(session: AsyncSession, month: date) -> list[dict]:
    hist = await monthly_category_history(session, add_months(month, -12), add_months(month, 1) - timedelta(days=1))
    names = dict((await session.execute(select(Category.id, Category.name))).all())
    out = []
    for cid, by_month in hist.items():
        current = by_month.get(month, Decimal(0))
        prior = [by_month.get(add_months(month, -i), Decimal(0)) for i in range(1, 13)]
        if sum(1 for v in prior if v > 0) < 4 or current < MIN_SPIKE:
            continue
        med, spread = robust(prior)
        z = (current - med) / spread
        if z >= 3 and current >= med * Decimal("1.5"):
            ratio = f"{current / med:.1f}×" if med > 0 else "well above"
            out.append(
                {
                    "kind": "category_spike",
                    "subject_key": f"cat:{cid}:{month:%Y-%m}",
                    "period": month,
                    "category_id": cid,
                    "amount": current,
                    "baseline": med,
                    "score": z,
                    "title": f"{names.get(cid, 'Category')} · {month:%b %Y}",
                    "detail": f"{names.get(cid, 'Category')} is {ratio} its monthly norm ({_money(med)}) "
                    f"at {_money(current)}.",
                }
            )
    return out


_MONTH_TXNS = text(
    f"""--sql
    SELECT t.id, t.txn_date, t.description, t.merchant, -t.amount AS spent, c.id AS category_id
    {FROM}
    WHERE {REPORTABLE} AND t.amount < 0 AND t.txn_date BETWEEN :start AND :end
    """
)

_MERCHANT_HISTORY = text(
    """--sql
    SELECT t.merchant, -t.amount AS spent
    FROM "transaction" t
    WHERE t.deleted_at IS NULL AND t.amount < 0 AND t.merchant = ANY(:merchants)
      AND t.txn_date >= :since AND t.txn_date < :before
    """
)


async def _transaction_outliers(session: AsyncSession, month: date) -> list[dict]:
    end = add_months(month, 1) - timedelta(days=1)
    txns = (await session.execute(_MONTH_TXNS, {"start": month, "end": end})).all()
    merchants = sorted({t.merchant for t in txns if t.merchant})
    history: dict[str, list[Decimal]] = {}
    if merchants:
        for r in await session.execute(
            _MERCHANT_HISTORY, {"merchants": merchants, "since": add_months(month, -24), "before": month}
        ):
            history.setdefault(r.merchant, []).append(Decimal(r.spent))
    threshold = await large_txn_threshold(session)
    out = []
    for t in txns:
        spent = Decimal(t.spent)
        past = history.get(t.merchant, []) if t.merchant else []
        base = {
            "period": month,
            "transaction_id": t.id,
            "category_id": t.category_id,
            "amount": spent,
            "subject_key": f"txn:{t.id}",
        }
        if len(past) >= 3 and spent >= MIN_MERCHANT:
            med, spread = robust(past)
            if spent >= max(med * Decimal("2.5"), med + 3 * spread):
                out.append(
                    {
                        **base,
                        "kind": "large_for_merchant",
                        "baseline": med,
                        "score": (spent - med) / spread,
                        "title": f"{t.description} · {t.txn_date:%d %b}",
                        "detail": f"{_money(spent)} is {spent / med:.1f}× the usual {_money(med)} at this merchant "
                        f"({len(past)} earlier purchases).",
                    }
                )
                continue
        recurring = sum(1 for p in past if abs(p - spent) <= spent * Decimal("0.15")) >= 3
        if spent >= threshold and not recurring:
            out.append(
                {
                    **base,
                    "kind": "large_transaction",
                    "baseline": threshold,
                    "score": spent / threshold,
                    "title": f"{t.description} · {t.txn_date:%d %b}",
                    "detail": f"Single transaction of {_money(spent)}, above the {_money(threshold)} alert level "
                    "and not a known recurring payment.",
                }
            )
        elif not past and spent >= NEW_MERCHANT_MIN:
            out.append(
                {
                    **base,
                    "kind": "new_merchant",
                    "baseline": None,
                    "score": spent / NEW_MERCHANT_MIN,
                    "title": f"{t.description} · {t.txn_date:%d %b}",
                    "detail": f"First purchase at this merchant in two years, for {_money(spent)}.",
                }
            )
    return out


async def _bill_increases(session: AsyncSession, month: date) -> list[dict]:
    end = add_months(month, 1) - timedelta(days=1)
    out = []
    for sr in (await session.scalars(select(StatementSeries).where(StatementSeries.is_active))).all():
        rows = (
            (
                await session.scalars(
                    select(Statement)
                    .where(Statement.series_id == sr.id, Statement.status == "approved", Statement.amount_due.is_not(None))
                    .order_by(Statement.statement_date.desc().nulls_last(), Statement.period_end.desc().nulls_last())
                    .limit(7)
                )
            )
            .unique()
            .all()
        )
        if len(rows) < 4:
            continue
        latest, prior = rows[0], rows[1:]
        stamp = latest.statement_date or latest.period_end
        if not stamp or not (month <= stamp <= end):
            continue
        avg = sum((p.amount_due for p in prior), Decimal(0)) / len(prior)
        if latest.amount_due > avg * Decimal("1.25") and latest.amount_due - avg >= 15:
            out.append(
                {
                    "kind": "bill_increase",
                    "subject_key": f"bill:{latest.id}",
                    "period": month,
                    "series_id": sr.id,
                    "category_id": sr.category_id,
                    "amount": latest.amount_due,
                    "baseline": avg,
                    "score": latest.amount_due / avg,
                    "title": f"{sr.name} bill · {stamp:%b %Y}",
                    "detail": f"{_money(latest.amount_due)} vs a recent average of {_money(avg)} "
                    f"(+{(latest.amount_due / avg - 1) * 100:.0f}%).",
                }
            )
    return out


_UNMATCHED_TRANSFERS = text(
    """--sql
    SELECT t.id, t.txn_date, t.description, t.amount, t.category_id, acc.name AS account,
           p.txn_date AS p_date, p.account AS p_account, p.category AS p_category
    FROM "transaction" t
    JOIN category c ON c.id = t.category_id AND c.type = 'transfer'
    LEFT JOIN account acc ON acc.id = t.account_id
    LEFT JOIN LATERAL (
        SELECT b.txn_date, ab.name AS account, cb.name AS category
        FROM "transaction" b
        LEFT JOIN account ab ON ab.id = b.account_id
        LEFT JOIN category cb ON cb.id = b.category_id
        WHERE b.deleted_at IS NULL AND b.transfer_match_id IS NULL AND b.amount = -t.amount
          AND b.account_id IS DISTINCT FROM t.account_id
          AND abs(b.txn_date - t.txn_date) <= CAST(:window AS integer)
        ORDER BY abs(b.txn_date - t.txn_date), b.id LIMIT 1
    ) p ON true
    WHERE t.deleted_at IS NULL AND t.transfer_match_id IS NULL AND t.amount <> 0
      AND t.txn_date BETWEEN :start AND :end AND t.txn_date <= :settled
    """
)


async def _unmatched_transfers(session: AsyncSession, month: date) -> list[dict]:
    """Transfers with no opposite leg in another account (once the other side has had time to post)."""
    params = {
        "start": month,
        "end": add_months(month, 1) - timedelta(days=1),
        "settled": date.today() - timedelta(days=MATCH_WINDOW_DAYS),
        "window": MATCH_WINDOW_DAYS,
    }
    threshold = await large_txn_threshold(session)
    out = []
    for t in await session.execute(_UNMATCHED_TRANSFERS, params):
        amount = abs(Decimal(t.amount))
        where = f"{'out of' if t.amount < 0 else 'into'} {t.account or 'an unassigned account'}"
        if t.p_date:
            hint = (
                f"A {_money(amount)} entry on {t.p_date:%d %b} in {t.p_account or 'another account'} is categorized as "
                f"{t.p_category}; recategorize it as a transfer if it is the other side."
            )
        else:
            hint = (
                f"No opposite {_money(amount)} entry in another account within {MATCH_WINDOW_DAYS} days; the other "
                "account may not be tracked, or this may not be a transfer."
            )
        out.append(
            {
                "kind": "unmatched_transfer",
                "subject_key": f"txn:{t.id}:transfer",
                "period": month,
                "transaction_id": t.id,
                "category_id": t.category_id,
                "amount": amount,
                "baseline": None,
                "score": amount / threshold,
                "title": f"{t.description} · {t.txn_date:%d %b}",
                "detail": f"Transfer of {_money(amount)} {where} has no matching transfer. {hint}",
            }
        )
    return out


async def detect(session: AsyncSession, month: date) -> dict:
    month = date(month.year, month.month, 1)
    await match_transfers(session)
    found = [
        *await _category_spikes(session, month),
        *await _transaction_outliers(session, month),
        *await _bill_increases(session, month),
        *await _unmatched_transfers(session, month),
    ]
    for a in found:
        values = {k: (Decimal(str(round(v, 3))) if k == "score" else v) for k, v in a.items()}
        stmt = insert(Anomaly).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Anomaly.subject_key],
                set_={k: stmt.excluded[k] for k in ("amount", "baseline", "score", "title", "detail")},
            )
        )
    keys = [a["subject_key"] for a in found]
    # Open findings that no longer hold (e.g. re-categorized, deleted) are withdrawn.
    stale = await session.execute(
        delete(Anomaly).where(Anomaly.period == month, Anomaly.status == "open", Anomaly.subject_key.not_in(keys or [""]))
    )
    await session.commit()
    return {"month": month.isoformat(), "found": len(found), "withdrawn": stale.rowcount or 0}


class _Note(BaseModel):
    id: int
    note: str


class _Notes(BaseModel):
    items: list[_Note]


async def narrate(session: AsyncSession, limit: int = 20) -> int:
    """Ask Gemini for a one-line, practical note on new findings. Best-effort."""
    if not get_settings().gemini_key:
        return 0
    from ledger.ai.client import generate

    rows = (
        await session.scalars(
            select(Anomaly)
            .where(Anomaly.status == "open", Anomaly.ai_note.is_(None))
            .order_by(Anomaly.score.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return 0
    listing = "\n".join(f"{a.id} | {a.kind} | {a.title} | {a.detail}" for a in rows)
    try:
        result: _Notes = await generate(
            listing,
            purpose="anomaly_note",
            tier="lite",
            schema=_Notes,
            temperature=0.2,
            system="For each household spending finding, write one short, neutral sentence suggesting what to check "
            "(e.g. an annual renewal, a duplicate charge, a one-off purchase). No moralizing.",
        )
    except Exception:
        log.exception("Anomaly narration failed")
        return 0
    notes = {n.id: n.note[:300] for n in result.items}
    for a in rows:
        if a.id in notes:
            a.ai_note = notes[a.id]
    await session.commit()
    return len(notes)
