import asyncio
import os

import pytest

os.environ.update(
    {
        "DB_HOST": os.environ.get("TEST_DB_HOST", "localhost"),
        "DB_PORT": os.environ.get("TEST_DB_PORT", "55432"),
        "DB_USER": "ledger",
        "DB_PASSWORD": "ledger",
        "DB_AUTH_MODE": "password",
        "DB_SSL": "disable",
        "DB_NAME": "ledger_test",
        "RUN_WORKER": "false",
        "GEMINI_KEY": "test-key",
        "SESSION_SECRET": "test-secret",
        # Blank = unset: tests must never reach the real mail server, Google or the inbox folder.
        "SMTP_HOST": "",
        "SMTP_PASSWORD": "",
        "IMAP_HOST": "",
        "IMAP_PASSWORD": "",
        "GOOGLE_SERVICE_ACCOUNT_FILE": "",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "",
        "INBOX_DIR": "",
        "APP_BASE_URL": "",
    }
)

from argon2 import PasswordHasher  # noqa: E402

os.environ["APP_PASSWORD_HASH"] = PasswordHasher().hash("test-pw")


def _reset_database() -> None:
    import asyncpg

    async def run():
        conn = await asyncpg.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ["DB_PORT"]),
            user="ledger",
            password="ledger",
            database="postgres",
        )
        try:
            await conn.execute("DROP DATABASE IF EXISTS ledger_test WITH (FORCE)")
            await conn.execute("CREATE DATABASE ledger_test")
        finally:
            await conn.close()

    asyncio.run(run())


@pytest.fixture(scope="session", autouse=True)
def database():
    _reset_database()
    from ledger.cli import migrate

    migrate()
    yield


@pytest.fixture(scope="session")
async def client(database):
    import httpx

    from ledger.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"password": "test-pw"})
        assert r.status_code == 200, r.text
        yield c
    from ledger.db.engine import dispose_engine

    await dispose_engine()
