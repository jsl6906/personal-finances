"""Encryption for credentials stored in the database, keyed from SESSION_SECRET (rotating it requires reconnecting)."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ledger.config import get_settings


def _fernet() -> Fernet:
    secret = get_settings().session_secret.get_secret_value().encode()
    key = hashlib.sha256(b"ledger-credentials:" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("Stored credential can't be decrypted (SESSION_SECRET changed?); reconnect the source") from None
