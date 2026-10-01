"""Tiller: read the Tiller Foundation template's Transactions and Balance History sheets via the Sheets API,
authenticated as a Google service account the sheet is shared with (read-only)."""

import hashlib
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx

from ledger.imports.parsing import parse_amount, parse_date
from ledger.sources.google import GoogleNotConfigured, auth_headers, service_account_email
from ledger.sources.service import Feed, FeedAccount, FeedBalance, FeedTxn

API = "https://sheets.googleapis.com/v4/spreadsheets"
SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
DEFAULT_CONFIG = {"transactions_sheet": "Transactions", "balances_sheet": "Balance History", "lookback_days": 60}


class TillerError(RuntimeError):
    pass


def parse_sheet_id(value: str) -> str:
    value = value.strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    sheet_id = m.group(1) if m else value
    if not SHEET_ID_RE.match(sheet_id):
        raise TillerError("That doesn't look like a Google Sheet URL or ID")
    return sheet_id


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
    try:
        headers = await auth_headers()
    except GoogleNotConfigured as exc:
        raise TillerError(str(exc)) from None
    r = await client.get(url, params=params, headers=headers)
    if r.status_code in (403, 404):
        raise TillerError(
            f"Google returned {r.status_code}: share the sheet (Viewer) with {service_account_email()} and check the ID"
        )
    if r.status_code == 400:
        raise TillerError(f"Google rejected the request: {r.json().get('error', {}).get('message', r.text)[:300]}")
    r.raise_for_status()
    return r.json()


async def sheet_info(sheet_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        meta = await _get(client, f"{API}/{sheet_id}", {"fields": "properties.title,sheets.properties.title"})
    return {"title": meta["properties"]["title"], "sheets": [s["properties"]["title"] for s in meta.get("sheets", [])]}


async def _values(client: httpx.AsyncClient, sheet_id: str, sheet: str) -> list[dict]:
    rng = quote(f"'{sheet}'!A1:ZZ", safe="")
    data = await _get(
        client,
        f"{API}/{sheet_id}/values/{rng}",
        {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"},
    )
    values = data.get("values", [])
    if not values:
        return []
    headers = [str(h).strip() for h in values[0]]
    return [{h: (row[i] if i < len(row) else "") for i, h in enumerate(headers) if h} for row in values[1:]]


def _get_ci(row: dict, *names: str) -> str:
    lowered = {k.lower(): v for k, v in row.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _amount(v) -> Decimal | None:
    if isinstance(v, (int, float)):
        try:
            return Decimal(str(v)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    return parse_amount(str(v)) if v not in (None, "") else None


def _account(row: dict, ids_by_name: dict[tuple[str, str], str] | None = None) -> FeedAccount | None:
    name = _get_ci(row, "Account")
    if not name:
        return None
    number, inst = _get_ci(row, "Account #"), _get_ci(row, "Institution")
    key = _get_ci(row, "Account ID") or (ids_by_name or {}).get((inst, name)) or f"{inst}|{name}|{number}"
    return FeedAccount(
        key=key, name=name, institution=inst or None, mask=number or None, type_hint=_get_ci(row, "Type", "Class") or None
    )


def build_feed(transactions: list[dict], balances: list[dict], since: date | None) -> Feed:
    feed = Feed()
    skipped = synthetic = 0
    # Rows imported into Tiller by hand have no Account ID; reuse the id Tiller gave the same account elsewhere.
    ids_by_name: dict[tuple[str, str], str] = {}
    for row in (*transactions, *balances):
        if aid := _get_ci(row, "Account ID"):
            ids_by_name.setdefault((_get_ci(row, "Institution"), _get_ci(row, "Account")), aid)
    seen: Counter[str] = Counter()
    for row in transactions:
        acct = _account(row, ids_by_name)
        tid = _get_ci(row, "Transaction ID")
        d = parse_date(_get_ci(row, "Date"))
        amount = _amount(row.get("Amount", _get_ci(row, "Amount")))
        desc = _get_ci(row, "Description", "Full Description")
        if not (acct and d and amount is not None and desc):
            skipped += 1 if any(str(v).strip() for v in row.values()) else 0
            continue
        if not tid:
            # Stable id from the row's content; the counter separates identical same-day purchases.
            base = f"{d.isoformat()}|{acct.key}|{amount}|{_get_ci(row, 'Full Description') or desc}"
            seen[base] += 1
            tid = "tiller:" + hashlib.sha1(f"{base}|{seen[base]}".encode()).hexdigest()[:24]
            synthetic += 1
        if since and d < since:
            continue
        feed.accounts.setdefault(acct.key, acct)
        feed.transactions.append(
            FeedTxn(
                external_id=tid,
                account_key=acct.key,
                txn_date=d,
                amount=amount,
                description=desc,
                full_description=_get_ci(row, "Full Description") or None,
                category=_get_ci(row, "Category") or None,
                check_number=_get_ci(row, "Check Number") or None,
                note=_get_ci(row, "Note", "Notes") or None,
            )
        )
    if skipped:
        feed.warnings.append(f"{skipped} transaction rows skipped (missing date, amount, account or description)")
    feed.synthetic_ids = synthetic

    latest: dict[tuple[str, date], FeedBalance] = {}
    for row in balances:
        acct = _account(row, ids_by_name)
        d = parse_date(_get_ci(row, "Date"))
        bal = _amount(row.get("Balance", _get_ci(row, "Balance")))
        if not (acct and d and bal is not None) or (since and d < since):
            continue
        feed.accounts.setdefault(acct.key, acct)
        latest[(acct.key, d)] = FeedBalance(account_key=acct.key, as_of=d, balance=bal)
    feed.balances = list(latest.values())
    return feed


async def fetch(config: dict, full: bool = False) -> Feed:
    cfg = {**DEFAULT_CONFIG, **config}
    sheet_id = cfg.get("sheet_id")
    if not sheet_id:
        raise TillerError("Set the Tiller sheet first")
    since = None if full else date.today() - timedelta(days=int(cfg["lookback_days"]))
    async with httpx.AsyncClient(timeout=60) as client:
        txns = await _values(client, sheet_id, cfg["transactions_sheet"])
        if txns and not {"date", "amount", "transaction id"} <= {k.lower() for k in txns[0]}:
            raise TillerError(
                f"Sheet '{cfg['transactions_sheet']}' is missing Tiller columns (Date, Amount, Transaction ID)"
            )
        try:
            balances = await _values(client, sheet_id, cfg["balances_sheet"]) if cfg.get("balances_sheet") else []
        except TillerError:
            balances = []
    return build_feed(txns, balances, since)
