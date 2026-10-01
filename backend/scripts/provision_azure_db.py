"""Provision the Azure Postgres side for Ledger, run as the server's Entra admin (your `az login`).

    uv run python scripts/provision_azure_db.py --check              # report only
    uv run python scripts/provision_azure_db.py --principal ledger-app  # create/grant

Creates the Postgres role for the app's Entra service principal, lets it create the
personal_finances schema (migrations then own it), and creates the read-only chat role.
"""

import argparse
import asyncio

import asyncpg
from azure.identity import AzureCliCredential

HOST = "jsl6906.postgres.database.azure.com"
DB = "personal_storage"
SCHEMA = "personal_finances"
DEFAULT_ADMIN = "jsl6906_gmail.com#EXT#@jsl6906gmail.onmicrosoft.com"
SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def connect(user: str, database: str) -> asyncpg.Connection:
    token = AzureCliCredential().get_token(SCOPE).token
    return await asyncpg.connect(host=HOST, user=user, password=token, database=database, ssl="require")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--admin-user", default=DEFAULT_ADMIN)
    p.add_argument("--principal", default="ledger-app", help="Display name of the app's service principal")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()

    pg = await connect(args.admin_user, "postgres")
    try:
        print("connected as", await pg.fetchval("SELECT current_user"), "|", await pg.fetchval("SELECT version()"))
        role = await pg.fetchrow("SELECT rolname FROM pg_roles WHERE rolname = $1", args.principal)
        print(f"role {args.principal!r}:", "exists" if role else "missing")
        if not role and not args.check:
            await pg.execute("SELECT * FROM pgaadauth_create_principal($1, false, false)", args.principal)
            print(f"created Entra principal role {args.principal!r}")
    finally:
        await pg.close()

    db = await connect(args.admin_user, DB)
    try:
        schema_owner = await db.fetchval("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = $1", SCHEMA)
        print(f"schema {SCHEMA}:", f"owned by {schema_owner}" if schema_owner else "missing")
        exts = [r["extname"] for r in await db.fetch("SELECT extname FROM pg_extension")]
        print("extensions:", ", ".join(exts))
        if args.check:
            return
        principal = ident(args.principal)
        await db.execute(f"GRANT CONNECT, CREATE ON DATABASE {ident(DB)} TO {principal}")
        await db.execute(
            """--sql
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pf_readonly') THEN
                    CREATE ROLE pf_readonly NOLOGIN;
                END IF;
            END $$;
            """
        )
        await db.execute(f"GRANT pf_readonly TO {principal}")
        print(f"granted CONNECT, CREATE on {DB} and pf_readonly to {args.principal}")
        # Requires pg_trgm in the server's azure.extensions allow-list.
        await db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public")
        print("pg_trgm extension ready")
        if schema_owner and schema_owner != args.principal:
            print(
                f"NOTE: schema exists and is owned by {schema_owner}; run as that owner:\n"
                f"  ALTER SCHEMA {SCHEMA} OWNER TO {principal};"
            )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
