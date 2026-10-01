"""Print who the app connects as and the migration state, e.g. `uv run python scripts/db_check.py`."""

import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_engine

SQL = """--sql
SELECT current_user AS login,
       (SELECT version_num FROM alembic_version) AS revision,
       (SELECT count(*) FROM category) AS categories,
       (SELECT count(*) FROM transaction) AS transactions,
       (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = current_schema()) AS schema_owner,
       (SELECT string_agg(extname, ',') FROM pg_extension) AS extensions
"""


async def main() -> None:
    async with get_engine().connect() as conn:
        print(dict((await conn.execute(text(SQL))).one()._mapping))
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
