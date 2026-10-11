from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILES = (Path(__file__).resolve().parents[3] / ".env", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, env_file_encoding="utf-8-sig", extra="ignore")

    # Database
    db_host: str = "jsl6906.postgres.database.azure.com"
    db_port: int = 5432
    db_name: str = "personal_storage"
    db_schema: str = "personal_finances"
    db_user: str = "ledger-app"
    db_auth_mode: Literal["entra", "password"] = "entra"
    db_password: SecretStr | None = None
    db_ssl: Literal["require", "disable", "prefer"] = "require"
    db_pool_size: int = 5

    # Entra service principal used as the Postgres login
    azure_tenant_id: str | None = None
    azure_client_id: str | None = None
    azure_client_secret: SecretStr | None = None

    # App auth
    app_password_hash: str | None = None
    session_secret: SecretStr = SecretStr("dev-only-change-me")
    session_max_age_days: int = 30
    cookie_secure: bool = False

    # AI
    gemini_key: SecretStr | None = None
    gemini_model_main: str = "gemini-3.8-flash"
    gemini_model_lite: str = "gemini-3.5-flash-lite"
    gemini_model_reasoning: str = "gemini-3.1-pro-preview"

    # Jobs
    job_concurrency: int = Field(default=2, ge=1, le=8)
    job_poll_seconds: float = 2.0
    run_worker: bool = True
    timezone: str = "America/New_York"
    nightly_hour: int = Field(default=3, ge=0, le=23)

    # Analytics
    large_txn_threshold: float = 500.0

    # Email alerts (SMTP relay); recipients are managed in the app
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    digest_weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"
    app_base_url: str | None = None  # used for links in emails, e.g. http://homeserver:8470

    # Inbound email: bills/receipts forwarded to a mailbox the app polls over IMAP
    imap_host: str | None = None
    imap_port: int = 993
    imap_username: str | None = None
    imap_password: SecretStr | None = None
    imap_folder: str = "INBOX"
    imap_poll_minutes: int = Field(default=5, ge=1, le=1440)
    mail_allowed_senders: str | None = None  # comma-separated; mail from anyone else is left untouched
    # Address that lands in the polled folder (e.g. you+ledger@gmail.com); question emails ask for replies there
    mail_inbound_address: str | None = None

    # Data sources: Google service account (share the Tiller sheet with its client_email)
    google_service_account_file: Path | None = None
    google_service_account_json: SecretStr | None = None
    # Local archive folder for backfill (mounted volume in Docker)
    inbox_dir: Path | None = None
    # Home value estimates (https://app.rentcast.io/app/api; free tier is 50 lookups/month)
    rentcast_api_key: SecretStr | None = None

    static_dir: Path | None = None
    max_upload_mb: int = 25
    log_level: str = "INFO"

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        return None if isinstance(v, str) and v.strip() == "" else v

    @field_validator("app_password_hash", mode="before")
    @classmethod
    def _unquote_hash(cls, v):
        # The hash is single-quoted in .env so docker compose doesn't expand its '$' signs; `docker run --env-file`
        # passes the quotes through literally.
        if isinstance(v, str) and len(v) > 1 and v[0] == v[-1] and v[0] in "'\"":
            return v[1:-1]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
