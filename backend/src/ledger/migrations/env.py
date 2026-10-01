import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection

import ledger.models  # noqa: F401  (register tables on metadata)
from ledger.config import get_settings
from ledger.db.base import Base
from ledger.db.engine import build_engine

settings = get_settings()
target_metadata = Base.metadata
SCHEMA = settings.db_schema


def _include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == SCHEMA
    return True


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=_include_name,
        version_table_schema=SCHEMA,
        compare_type=True,
    )


def _run_sync(connection: Connection) -> None:
    # CREATE SCHEMA IF NOT EXISTS still needs database CREATE privilege, so check first.
    exists = connection.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}).scalar()
    if not exists:
        connection.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
    # End the autobegun transaction so alembic's begin_transaction() owns (and commits) the next one.
    connection.commit()
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Keep the app schema off the search_path so autogenerate reflects it as a named (non-default) schema.
    engine = build_engine(settings, search_path="public")
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url="postgresql://offline",
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async_migrations())
