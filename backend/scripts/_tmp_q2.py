import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_sessionmaker


async def main() -> None:
    async with get_sessionmaker()() as s:
        cat = (await s.execute(text("SELECT id FROM category WHERE name = 'Synoptic Expense'"))).scalar_one()
        rows = await s.execute(
            text("""UPDATE "transaction" SET category_id = :c WHERE merchant = 'microsoft azure'"""), {"c": cat}
        )
        rules = await s.execute(
            text("UPDATE category_rule SET category_id = :c WHERE match_type = 'regex' AND pattern = :p"),
            {"c": cat, "p": "MICROSOFT ?[#*-] ?G[0-9]"},
        )
        await s.commit()
        print(f"Azure rows -> Synoptic Expense: {rows.rowcount}; rules repointed: {rules.rowcount}")
    await dispose_engine()


asyncio.run(main())
