"""Profile the Tiller Transactions tab (headers and blank counts, no values).

uv run python scripts/tiller_profile.py <sheet-url>
"""

import asyncio
import sys
from collections import Counter

import httpx

from ledger.sources import tiller


async def main(sheet: str) -> None:
    sid = tiller.parse_sheet_id(sheet)
    async with httpx.AsyncClient(timeout=120) as client:
        rows = await tiller._values(client, sid, "Transactions")
    print("rows:", len(rows))
    headers = list(rows[0].keys()) if rows else []
    print("headers:", headers)
    blank = Counter()
    for r in rows:
        for h in headers:
            if str(r.get(h, "")).strip() == "":
                blank[h] += 1
    print("blank counts:", {h: blank[h] for h in headers})
    empty_rows = sum(1 for r in rows if not any(str(v).strip() for v in r.values()))
    print("fully empty rows:", empty_rows)
    no_id = [r for r in rows if not str(r.get("Transaction ID", "")).strip() and any(str(v).strip() for v in r.values())]
    years = Counter(
        tiller.parse_date(str(r.get("Date", ""))).year for r in no_id if tiller.parse_date(str(r.get("Date", "")))
    )
    print("rows without Transaction ID by year:", dict(sorted(years.items())))


asyncio.run(main(sys.argv[1]))
