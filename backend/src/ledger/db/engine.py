import logging
import ssl
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ledger.config import Settings, get_settings

log = logging.getLogger(__name__)

ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_credential = None


def _entra_token(settings: Settings) -> str:
    global _credential
    if _credential is None:
        from azure.identity import ClientSecretCredential

        if not (settings.azure_tenant_id and settings.azure_client_id and settings.azure_client_secret):
            raise RuntimeError("AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET are required for Entra DB auth")
        _credential = ClientSecretCredential(
            settings.azure_tenant_id, settings.azure_client_id, settings.azure_client_secret.get_secret_value()
        )
    # The credential caches tokens in memory and refreshes shortly before expiry.
    return _credential.get_token(ENTRA_SCOPE).token


def _ssl_arg(settings: Settings) -> ssl.SSLContext | bool:
    if settings.db_ssl == "disable":
        return False
    ctx = ssl.create_default_context()
    if settings.db_ssl == "prefer":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def build_engine(settings: Settings | None = None, search_path: str | None = None, **kwargs) -> AsyncEngine:
    settings = settings or get_settings()
    search_path = search_path or f"{settings.db_schema},public"
    url = f"postgresql+asyncpg://{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    engine = create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "ssl": _ssl_arg(settings),
            "server_settings": {"search_path": search_path, "application_name": "ledger"},
        },
        **kwargs,
    )

    @event.listens_for(engine.sync_engine, "do_connect")
    def _provide_password(dialect, conn_rec, cargs, cparams):
        if settings.db_auth_mode == "entra":
            cparams["password"] = _entra_token(settings)
        elif settings.db_password is not None:
            cparams["password"] = settings.db_password.get_secret_value()

    return engine


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = build_engine()
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
