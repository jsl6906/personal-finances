"""Google service-account access (read-only Sheets + Drive), shared by the Tiller feed and the Drive backfill."""

import asyncio
import json

from ledger.config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

_creds = None


class GoogleNotConfigured(RuntimeError):
    pass


def service_account_info() -> dict | None:
    s = get_settings()
    if s.google_service_account_json:
        return json.loads(s.google_service_account_json.get_secret_value())
    if s.google_service_account_file and s.google_service_account_file.exists():
        return json.loads(s.google_service_account_file.read_text(encoding="utf-8"))
    return None


def service_account_email() -> str | None:
    info = service_account_info()
    return info.get("client_email") if info else None


def _token_sync() -> str:
    global _creds
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    if _creds is None:
        info = service_account_info()
        if not info:
            raise GoogleNotConfigured("No Google service account configured (GOOGLE_SERVICE_ACCOUNT_FILE or _JSON)")
        _creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds.token


async def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {await asyncio.to_thread(_token_sync)}"}
