"""Probe SimpleFIN with the protocol's public demo: uv run python scripts/simplefin_demo.py [--apply]"""

import asyncio
import base64
import sys

from ledger.sources import simplefin


async def main():
    for host in ("beta-bridge.simplefin.org", "bridge.simplefin.org"):
        token = base64.b64encode(f"https://{host}/simplefin/claim/demo".encode()).decode()
        try:
            access = await simplefin.claim(token)
        except simplefin.SimpleFinError as exc:
            print(host, "claim failed:", exc)
            access = f"https://demo:demo@{host}/simplefin"
        print(host, "using", access.split("@")[-1])
        try:
            feed = await simplefin.fetch(access, {"lookback_days": 30})
        except simplefin.SimpleFinError as exc:
            print("  fetch failed:", exc)
            continue
        print(
            f"accounts={len(feed.accounts)} txns={len(feed.transactions)} balances={len(feed.balances)} "
            f"holdings={len(feed.holdings)} warnings={feed.warnings}"
        )
        for a in list(feed.accounts.values())[:5]:
            print("  ", a)
        for t in feed.transactions[:3]:
            print("  ", t)
        if "--apply" in sys.argv:
            await apply_demo(access)
        return


async def apply_demo(access: str) -> None:
    """Connect the demo as the SimpleFIN source in the configured (dev!) database and sync it."""
    from sqlalchemy import select

    import ledger.main  # noqa: F401  (registers job handlers enqueued after the sync)
    from ledger.crypto import encrypt
    from ledger.db.engine import dispose_engine, get_sessionmaker
    from ledger.models import Source
    from ledger.sources.jobs import run_sync

    async with get_sessionmaker()() as s:
        src = await s.scalar(select(Source).where(Source.kind == "simplefin"))
        if src is None:
            src = Source(kind="simplefin", name="SimpleFIN", config={"lookback_days": 60})
            s.add(src)
        src.secret = encrypt(access)
        await s.commit()
        print("sync:", await run_sync(s, src.id))
    await dispose_engine()


asyncio.run(main())
