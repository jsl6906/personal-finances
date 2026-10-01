"""SimpleFIN Bridge (https://bridge.simplefin.org): a setup token is claimed once for an access URL that
carries its own Basic-auth credentials; that URL is stored encrypted and used to pull accounts, balances,
transactions and (where the bank provides them) holdings."""

import base64
import binascii
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from ledger.config import get_settings
from ledger.sources.service import Feed, FeedAccount, FeedBalance, FeedHolding, FeedTxn

DEFAULT_CONFIG = {"lookback_days": 30}
MAX_LOOKBACK_DAYS = 90


class SimpleFinError(RuntimeError):
    pass


def claim_url(setup_token: str) -> str:
    token = re.sub(r"\s", "", setup_token)
    try:
        url = base64.b64decode(token + "=" * (-len(token) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise SimpleFinError("That isn't a SimpleFIN setup token") from None
    if not url.startswith("https://"):
        raise SimpleFinError("That isn't a SimpleFIN setup token")
    return url


async def claim(setup_token: str) -> str:
    """Exchange a one-time setup token for the access URL."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(claim_url(setup_token), headers={"Content-Length": "0"})
    if r.status_code == 403:
        raise SimpleFinError(
            "SimpleFIN refused the token (already claimed or expired). If you didn't claim it, disable it at SimpleFIN "
            "Bridge, then create a new one"
        )
    if r.status_code != 200:
        raise SimpleFinError(f"SimpleFIN claim failed (HTTP {r.status_code}); check the token and try again")
    access = r.text.strip()
    parts = urlsplit(access)
    if parts.scheme != "https" or not parts.username or not parts.password:
        raise SimpleFinError("SimpleFIN returned an unexpected access URL")
    return access


def _split(access_url: str) -> tuple[str, tuple[str, str]]:
    p = urlsplit(access_url)
    base = f"{p.scheme}://{p.hostname}{f':{p.port}' if p.port else ''}{p.path.rstrip('/')}"
    return base, (p.username or "", p.password or "")


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _day(epoch, tz: ZoneInfo) -> date | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), UTC).astimezone(tz).date()


def build_feed(payload: dict) -> Feed:
    tz = ZoneInfo(get_settings().timezone)
    errors = [str(e.get("msg") or e.get("code")) for e in payload.get("errlist") or [] if isinstance(e, dict)]
    errors += [str(e) for e in payload.get("errors") or [] if str(e) not in errors]
    feed = Feed(warnings=[e[:300] for e in errors][:20])
    connections = {c.get("conn_id"): c for c in payload.get("connections") or [] if isinstance(c, dict)}
    for a in payload.get("accounts", []):
        # Account ids are only unique within a connection, transaction ids only within an account.
        key = f"{a['conn_id']}/{a['id']}" if a.get("conn_id") else str(a["id"])
        org = a.get("org") or {}
        conn = connections.get(a.get("conn_id")) or {}
        institution = conn.get("org_name") or conn.get("name") or org.get("name") or org.get("domain")
        feed.accounts[key] = FeedAccount(key=key, name=a.get("name") or key, institution=institution)
        as_of = _day(a.get("balance-date"), tz) or date.today()
        bal = _dec(a.get("balance"))
        if bal is not None:
            feed.balances.append(FeedBalance(key, as_of, bal.quantize(Decimal("0.01")), _dec(a.get("available-balance"))))
        for h in a.get("holdings") or []:
            feed.holdings.append(
                FeedHolding(
                    account_key=key,
                    as_of=as_of,
                    external_id=str(h.get("id") or h.get("symbol") or h.get("description")),
                    symbol=h.get("symbol") or None,
                    description=(h.get("description") or "")[:300] or None,
                    shares=_dec(h.get("shares")),
                    market_value=_dec(h.get("market_value")),
                    cost_basis=_dec(h.get("cost_basis")),
                    currency=h.get("currency") or a.get("currency"),
                )
            )
        for t in a.get("transactions") or []:
            posted = _day(t.get("posted"), tz)
            if t.get("pending") or not posted:
                continue
            amount = _dec(t.get("amount"))
            if amount is None:
                continue
            desc = (t.get("payee") or "").strip()
            full = (t.get("description") or "").strip()
            if len(desc) < 4:  # payees like "You" say less than the description
                desc = full or desc
            memo = (t.get("memo") or "").strip()
            feed.transactions.append(
                FeedTxn(
                    external_id=f"sfin:{key}:{t['id']}"[:200],
                    account_key=key,
                    txn_date=_day(t.get("transacted_at"), tz) or posted,
                    posted_date=posted,
                    amount=amount.quantize(Decimal("0.01")),
                    description=desc or full or "(no description)",
                    full_description=" · ".join(x for x in (full, memo) if x and x != desc) or None,
                )
            )
    return feed


async def fetch(access_url: str, config: dict, full: bool = False) -> Feed:
    days = MAX_LOOKBACK_DAYS if full else min(int({**DEFAULT_CONFIG, **config}["lookback_days"]), MAX_LOOKBACK_DAYS)
    start = datetime.combine(date.today() - timedelta(days=days), datetime.min.time(), UTC)
    base, auth = _split(access_url)
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.get(f"{base}/accounts", params={"start-date": int(start.timestamp()), "version": 2}, auth=auth)
    if r.status_code == 403:
        raise SimpleFinError("SimpleFIN access was revoked; reconnect with a new setup token")
    if r.status_code == 402:
        raise SimpleFinError("SimpleFIN subscription payment required")
    if r.status_code != 200:
        raise SimpleFinError(f"SimpleFIN returned HTTP {r.status_code}")
    return build_feed(r.json())
