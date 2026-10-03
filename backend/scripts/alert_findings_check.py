"""Compare emailed alert events with findings (anomalies), e.g. `uv run python scripts/alert_findings_check.py`."""

import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_engine

QUERIES = {
    "transactions behind alerts whose finding is gone": """--sql
        SELECT e.id AS event_id, e.link, e.body, t.id, t.deleted_at, t.merchant, t.category_id, t.transfer_match_id,
               t.account_id, t.amount
        FROM alert_event e
        LEFT JOIN anomaly a ON a.id = substring(e.subject_key FROM 'anomaly:(\\d+)')::int
        LEFT JOIN "transaction" t ON t.id = substring(e.link FROM '/transactions/(\\d+)')::int
        WHERE e.subject_key LIKE 'anomaly:%' AND a.id IS NULL
    """,
    "events by kind/status": """--sql
        SELECT kind, status, count(*) AS n, min(created_at)::date AS first, max(created_at)::date AS last
        FROM alert_event GROUP BY 1, 2 ORDER BY 1, 2
    """,
    "anomaly-backed events vs current findings": """--sql
        SELECT e.kind,
               CASE WHEN a.id IS NULL THEN 'finding gone' ELSE 'finding ' || a.status END AS state,
               count(*) AS n
        FROM alert_event e
        LEFT JOIN anomaly a ON a.id = substring(e.subject_key FROM 'anomaly:(\\d+)')::int
        WHERE e.subject_key LIKE 'anomaly:%'
        GROUP BY 1, 2 ORDER BY 1, 2
    """,
    "sample events whose finding is gone": """--sql
        SELECT e.id, e.created_at::date AS sent, e.status, e.title
        FROM alert_event e
        LEFT JOIN anomaly a ON a.id = substring(e.subject_key FROM 'anomaly:(\\d+)')::int
        WHERE e.subject_key LIKE 'anomaly:%' AND a.id IS NULL
        ORDER BY e.id DESC LIMIT 25
    """,
    "same title alerted more than once": """--sql
        SELECT title, count(*) AS n, string_agg(subject_key, ' ') AS keys
        FROM alert_event WHERE subject_key LIKE 'anomaly:%'
        GROUP BY title HAVING count(*) > 1 ORDER BY n DESC LIMIT 20
    """,
    "open findings by kind / alerted?": """--sql
        SELECT a.kind, (e.id IS NOT NULL) AS alerted,
               (a.created_at >= now() - interval '7 days') AS recent, count(*) AS n
        FROM anomaly a
        LEFT JOIN alert_event e ON e.subject_key = 'anomaly:' || a.id
        WHERE a.status = 'open'
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    """,
    "alerted findings whose text changed since": """--sql
        SELECT e.id, e.title AS alert_title, a.title AS finding_title, a.amount
        FROM alert_event e
        JOIN anomaly a ON a.id = substring(e.subject_key FROM 'anomaly:(\\d+)')::int
        WHERE position(a.title IN e.title) = 0 OR e.body NOT LIKE a.detail || '%'
        ORDER BY e.id DESC LIMIT 15
    """,
    "rules": "SELECT kind, enabled, params FROM alert_rule ORDER BY kind",
    "all findings by kind/status/alerted": """--sql
        SELECT a.kind, a.status, (e.id IS NOT NULL) AS alerted, count(*) AS n,
               min(a.created_at)::date AS first_created, max(a.created_at)::date AS last_created,
               min(a.period) AS min_period, max(a.period) AS max_period
        FROM anomaly a LEFT JOIN alert_event e ON e.subject_key = 'anomaly:' || a.id
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    """,
    "all alert events": """--sql
        SELECT e.id, e.created_at, e.kind, e.subject_key, e.title, a.kind AS akind, a.period, a.status AS astatus,
               a.amount, a.created_at AS a_created
        FROM alert_event e
        LEFT JOIN anomaly a ON a.id = substring(e.subject_key FROM 'anomaly:(\\d+)')::int
        ORDER BY e.id
    """,
    "findings for recent periods": """--sql
        SELECT a.id, a.kind, a.period, a.status, a.created_at, a.title, a.amount,
               (SELECT e.id FROM alert_event e WHERE e.subject_key = 'anomaly:' || a.id) AS event_id
        FROM anomaly a WHERE a.period >= date '2026-08-01' ORDER BY a.period, a.kind, a.id
    """,
}


async def main() -> None:
    async with get_engine().connect() as conn:
        for name, sql in QUERIES.items():
            print(f"\n== {name}")
            for row in await conn.execute(text(sql)):
                print(dict(row._mapping))
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
