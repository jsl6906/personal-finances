import hashlib
import re
from datetime import date
from decimal import Decimal

_NOISE = re.compile(
    r"""
    \b(pos|debit|credit|purchase|recurring|payment|card|visa|mastercard|ach|web|pmt|ppd|id|ref|des|indn|co)\b
    | \$?\b\d[\d,]*\.\d{2}\b   # embedded amounts like 'Future Amount: 212.73'
    | \#\s*\d+             # store numbers like #412
    | \b\d{3,}\b           # long digit runs (refs, store ids, phone)
    | \*[a-z0-9]+          # AMAZON.COM*2K4 style suffixes
    | \b[a-z]{2}\s*$       # trailing state code
    """,
    re.IGNORECASE | re.VERBOSE,
)
_NON_WORD = re.compile(r"[^a-z0-9&' ]+")
_SPACES = re.compile(r"\s+")


def normalize_merchant(description: str | None) -> str | None:
    """Collapse a raw bank description to a stable merchant key ('KROGER #412 SPRINGFIELD IL' -> 'kroger springfield')."""
    if not description:
        return None
    s = description.lower()
    s = _NOISE.sub(" ", s)
    s = _NON_WORD.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    return s[:200] or None


def fingerprint(account_id: int | None, txn_date: date, amount: Decimal, description: str | None) -> str:
    key = f"{account_id or 0}|{txn_date.isoformat()}|{Decimal(amount):.2f}|{normalize_merchant(description) or ''}"
    return hashlib.sha256(key.encode()).hexdigest()
