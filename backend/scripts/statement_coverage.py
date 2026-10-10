"""Read-only report: which accounts/periods have ledger transactions but no uploaded statement covering them.

A statement covers [period_start, period_end] of the account its statement_check resolved to (committed document
imports only). Uncovered stretches of each account's transaction history are listed with counts by source.
Run: scripts/with_env.ps1 .env.azure python scripts/statement_coverage.py
"""

import asyncio
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_engine

ROOT = Path(__file__).resolve().parents[2]
SLACK_DAYS = 3  # gaps this short between consecutive statements are cycle-boundary noise, not missing statements

ACCOUNTS_SQL = """--sql
SELECT a.id, a.name, a.account_type, a.is_closed, a.is_hidden, i.name AS institution
FROM account a LEFT JOIN institution i ON i.id = a.institution_id
"""

PERIODS_SQL = """--sql
SELECT c.account_id, c.period_start, c.period_end, c.import_batch_id, c.status
FROM statement_check c
JOIN import_batch b ON b.id = c.import_batch_id
WHERE b.status = 'committed' AND c.account_id IS NOT NULL
  AND c.period_start IS NOT NULL AND c.period_end IS NOT NULL
ORDER BY c.account_id, c.period_start
"""

UNCHECKED_SQL = """--sql
SELECT b.id, b.doc_meta->>'period_start' AS ps, b.doc_meta->>'period_end' AS pe, b.defaults->>'account_id' AS acct
FROM import_batch b
WHERE b.source_type = 'document' AND b.status = 'committed'
  AND NOT EXISTS (SELECT 1 FROM statement_check c WHERE c.import_batch_id = b.id AND c.account_id IS NOT NULL)
ORDER BY b.id
"""

TXNS_SQL = """--sql
SELECT account_id, txn_date, source_type, amount
FROM "transaction"
WHERE deleted_at IS NULL AND account_id IS NOT NULL
ORDER BY account_id, txn_date
"""


def merge(periods: list[tuple[date, date]]) -> list[tuple[date, date]]:
    out: list[list[date]] = []
    for s, e in sorted(periods):
        if out and s <= out[-1][1] + timedelta(days=SLACK_DAYS + 1):
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def covered(d: date, spans: list[tuple[date, date]]) -> bool:
    return any(s <= d <= e for s, e in spans)


def months(s: date, e: date) -> str:
    return f"{s:%Y-%m}" if (s.year, s.month) == (e.year, e.month) else f"{s:%Y-%m}..{e:%Y-%m}"


async def main() -> None:
    async with get_engine().connect() as conn:
        accounts = {r.id: r for r in (await conn.execute(text(ACCOUNTS_SQL))).all()}
        periods_rows = (await conn.execute(text(PERIODS_SQL))).all()
        unchecked = (await conn.execute(text(UNCHECKED_SQL))).all()
        txns = (await conn.execute(text(TXNS_SQL))).all()
    await dispose_engine()

    periods: dict[int, list[tuple[date, date]]] = defaultdict(list)
    batches: dict[int, set[int]] = defaultdict(set)
    for r in periods_rows:
        periods[r.account_id].append((r.period_start, r.period_end))
        batches[r.account_id].add(r.import_batch_id)
    by_acct: dict[int, list] = defaultdict(list)
    for t in txns:
        by_acct[t.account_id].append(t)

    lines: list[str] = []
    summary: list[tuple] = []
    detail: list[str] = []
    for aid, rows in by_acct.items():
        a = accounts[aid]
        spans = merge(periods.get(aid, []))
        first, last = rows[0].txn_date, rows[-1].txn_date
        out_rows = [t for t in rows if not covered(t.txn_date, spans)]
        tiller_total = sum(t.source_type == "tiller" for t in rows)
        tiller_out = sum(t.source_type == "tiller" for t in out_rows)
        summary.append((aid, a, len(batches.get(aid, ())), first, last, len(rows), len(out_rows), tiller_total, tiller_out, spans))
        if not out_rows:
            continue
        # Group uncovered transactions into runs separated by a statement span (or a >45-day silence).
        runs: list[list] = []
        for t in out_rows:
            if runs:
                prev = runs[-1][-1].txn_date
                split = any(prev < s and e < t.txn_date for s, e in spans) or (t.txn_date - prev).days > 45
                if not split:
                    runs[-1].append(t)
                    continue
            runs.append([t])
        label = f"[{aid}] {a.institution or '-'} / {a.name} ({a.account_type}{', closed' if a.is_closed else ''})"
        detail.append(label)
        detail.append(
            f"    statements: {len(batches.get(aid, ()))}  coverage: "
            + (", ".join(f"{s}..{e}" for s, e in spans) or "none")
        )
        for run in runs:
            src = Counter(t.source_type for t in run)
            net = sum((t.amount for t in run), Decimal(0))
            s, e = run[0].txn_date, run[-1].txn_date
            detail.append(
                f"    MISSING {months(s, e):<17} {s}..{e}  {len(run):>5} txns  net {net:>12,.2f}  "
                + ", ".join(f"{k}={v}" for k, v in src.most_common())
            )
        detail.append("")

    summary.sort(key=lambda x: (-x[8], -x[6]))
    lines.append(f"Statement coverage report (gaps <= {SLACK_DAYS} days between statements ignored)")
    lines.append("")
    lines.append(
        f"{'id':>4}  {'account':<55} {'stmts':>5} {'txn span':<23} {'txns':>6} {'uncov':>6} {'tiller':>6} {'t-uncov':>7}"
    )
    for aid, a, n, first, last, total, out, tt, to, _ in summary:
        name = f"{a.institution or '-'} / {a.name}"[:55]
        lines.append(f"{aid:>4}  {name:<55} {n:>5} {first}..{last} {total:>6} {out:>6} {tt:>6} {to:>7}")
    lines.append("")
    lines.append("=== Uncovered stretches by account ===")
    lines.append("")
    lines.extend(detail)
    if unchecked:
        lines.append("=== Committed statements without an account-resolved check (not counted above) ===")
        for r in unchecked:
            lines.append(f"    batch #{r.id}  {r.ps}..{r.pe}  default account {r.acct}")
    report = "\n".join(lines)
    out_path = ROOT / "logs" / "statement_coverage.txt"
    out_path.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
