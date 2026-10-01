"""Where backfill time goes: `scripts/with_env.ps1 .env.azure python scripts/backfill_timing.py`."""

import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_engine

QUERIES = {
    "status x kind": """--sql
SELECT status, coalesce(kind, '-') AS kind, count(*) AS n
FROM backfill_file GROUP BY 1, 2 ORDER BY 1, 2
""",
    "settings": """--sql
SELECT value - 'folder_id' AS value, updated_at FROM app_setting WHERE key = 'backfill'
""",
    "files processed per hour (last 48h)": """--sql
SELECT date_trunc('hour', processed_at) AS hour, count(*) AS files,
       count(*) FILTER (WHERE detail ? 'classification') AS classified_ai
FROM backfill_file WHERE processed_at > now() - interval '48 hours'
GROUP BY 1 ORDER BY 1
""",
    "gaps between consecutive processed files (top 15)": """--sql
WITH t AS (
  SELECT id, name, status, kind, processed_at,
         processed_at - lag(processed_at) OVER (ORDER BY processed_at) AS gap
  FROM backfill_file WHERE processed_at IS NOT NULL
)
SELECT id, left(name, 50) AS name, status, kind, processed_at, gap FROM t
WHERE gap IS NOT NULL ORDER BY gap DESC LIMIT 15
""",
    "per-file gap percentiles by kind/status": """--sql
WITH t AS (
  SELECT status, kind, extract(epoch FROM processed_at - lag(processed_at) OVER (ORDER BY processed_at)) AS s
  FROM backfill_file WHERE processed_at IS NOT NULL
)
SELECT coalesce(kind, '-') AS kind, status, count(*) AS n,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY s)::numeric, 1) AS p50_s,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY s)::numeric, 1) AS p90_s,
       round(max(s)::numeric, 1) AS max_s
FROM t WHERE s IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2
""",
    "backfill_run jobs": """--sql
SELECT status, count(*) AS n, min(created_at) AS first, max(finished_at) AS last,
       round(avg(extract(epoch FROM finished_at - started_at))::numeric, 1) AS avg_run_s,
       round(max(extract(epoch FROM finished_at - started_at))::numeric, 1) AS max_run_s,
       round(sum(extract(epoch FROM finished_at - started_at))::numeric / 3600, 2) AS total_run_h,
       sum((result->>'processed')::int) AS files
FROM job WHERE type = 'backfill_run' GROUP BY 1
""",
    "idle time between backfill_run jobs (top 10)": """--sql
WITH j AS (
  SELECT id, status, started_at, finished_at,
         started_at - lag(finished_at) OVER (ORDER BY started_at) AS idle
  FROM job WHERE type = 'backfill_run' AND started_at IS NOT NULL
)
SELECT id, status, started_at, finished_at, idle FROM j WHERE idle IS NOT NULL ORDER BY idle DESC LIMIT 10
""",
    "recent backfill_run errors": """--sql
SELECT id, status, attempts, started_at, left(error, 300) AS error
FROM job WHERE type = 'backfill_run' AND error IS NOT NULL ORDER BY id DESC LIMIT 5
""",
    "other jobs competing (last 48h)": """--sql
SELECT type, status, count(*) AS n,
       round(sum(extract(epoch FROM finished_at - started_at))::numeric / 60, 1) AS total_min
FROM job WHERE created_at > now() - interval '48 hours' AND type <> 'backfill_run'
GROUP BY 1, 2 ORDER BY total_min DESC NULLS LAST
""",
    "AI calls (last 48h)": """--sql
SELECT purpose, model, ok, count(*) AS n,
       round(avg(latency_ms) / 1000.0, 1) AS avg_s,
       round((percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) / 1000.0)::numeric, 1) AS p90_s,
       round(max(latency_ms) / 1000.0, 1) AS max_s,
       round(sum(latency_ms) / 3600000.0, 2) AS total_h,
       sum(input_tokens) AS in_tok, sum(output_tokens) AS out_tok
FROM ai_call_log WHERE created_at > now() - interval '48 hours'
GROUP BY 1, 2, 3 ORDER BY total_h DESC NULLS LAST
""",
    "AI errors (last 48h)": """--sql
SELECT left(error, 160) AS error, count(*) AS n, max(created_at) AS last
FROM ai_call_log WHERE NOT ok AND created_at > now() - interval '48 hours'
GROUP BY 1 ORDER BY n DESC LIMIT 10
""",
    "failed / review files": """--sql
SELECT status, left(coalesce(error, message), 140) AS why, count(*) AS n
FROM backfill_file WHERE status IN ('failed', 'review') GROUP BY 1, 2 ORDER BY n DESC LIMIT 15
""",
}


async def main() -> None:
    async with get_engine().connect() as conn:
        for title, sql in QUERIES.items():
            print(f"\n== {title}")
            res = await conn.execute(text(sql))
            print(" | ".join(res.keys()))
            for row in res:
                print(" | ".join(str(v) for v in row))
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
