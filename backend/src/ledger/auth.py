import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, status

from ledger.config import get_settings

_hasher = PasswordHasher()
_failures: dict[str, deque[float]] = defaultdict(deque)
MAX_FAILURES = 5
WINDOW_SECONDS = 300


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_login(request: Request, password: str) -> None:
    key = _client_key(request)
    now = time.monotonic()
    attempts = _failures[key]
    while attempts and now - attempts[0] > WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= MAX_FAILURES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; try again later")

    stored = get_settings().app_password_hash
    if not stored:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "APP_PASSWORD_HASH is not configured")
    try:
        _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        attempts.append(now)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect password") from None
    attempts.clear()
    request.session.clear()
    request.session["auth"] = True
    request.session["sid"] = secrets.token_urlsafe(16)


def logout(request: Request) -> None:
    request.session.clear()


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("auth"))


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
