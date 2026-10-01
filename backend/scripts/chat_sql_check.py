"""Verify the chat SQL guard runs as pf_readonly against the configured database."""

import asyncio

from ledger.chat.sql import run_readonly
from ledger.db.engine import dispose_engine


async def main():
    print(await run_readonly("SELECT current_user AS who, count(*) AS categories FROM v_categories"))
    try:
        await run_readonly("SELECT * FROM transaction")
    except Exception as exc:
        print("blocked:", type(exc).__name__, exc)
    await dispose_engine()


asyncio.run(main())
