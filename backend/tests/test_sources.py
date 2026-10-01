import base64
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from ledger.db.engine import get_sessionmaker
from ledger.models import Account, AccountBalance, ImportBatch, Source, Transaction
from ledger.sources import simplefin, tiller
from ledger.sources.jobs import run_sync
from ledger.sources.service import apply_feed

TODAY = date.today()


def _us(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


@pytest.fixture
def no_ai(monkeypatch):
    from ledger.ai import imports as ai_imports

    verdicts: dict[str, float] = {}

    async def fake_adjudicate(pairs):
        out = {}
        for p in pairs:
            prob = verdicts.get(p["b"]["description"])
            if prob is not None:
                out[p["id"]] = ai_imports.PairVerdict(pair_id=p["id"], probability_same=prob, reason="test")
        return out

    async def fake_labels(labels, taxonomy):
        return []

    monkeypatch.setattr(ai_imports, "adjudicate_pairs", fake_adjudicate)
    monkeypatch.setattr(ai_imports, "map_category_labels", fake_labels)
    return verdicts


async def _source(kind: str, **config) -> int:
    async with get_sessionmaker()() as s:
        src = await s.scalar(select(Source).where(Source.kind == kind))
        if src is None:
            src = Source(kind=kind, name=kind.title(), config=config)
            s.add(src)
        await s.commit()
        return src.id


def _tiller_row(d, desc, amount, tid, category="", account="TS Checking", number="xxxx4821"):
    return {
        "Date": _us(d),
        "Description": desc,
        "Category": category,
        "Amount": amount,
        "Account": account,
        "Account #": number,
        "Institution": "TS Bank",
        "Transaction ID": tid,
        "Account ID": f"acct-{account}",
        "Full Description": f"{desc} FULL",
        "Check Number": "",
    }


async def test_tiller_feed_sync(client, no_ai):
    g = (await client.post("/api/category-groups", json={"name": "TS Group"})).json()
    cat = (await client.post("/api/categories", json={"name": "TS Coffee", "group_id": g["id"]})).json()
    d = TODAY - timedelta(days=3)
    rows = [
        _tiller_row(d, "TS BEANERY", -4.5, "tid-1", "TS Coffee"),
        _tiller_row(d, "TS PAYCHECK", 2500, "tid-2"),
        _tiller_row(TODAY - timedelta(days=400), "TS OLD", -1, "tid-old"),
        {"Date": "", "Description": "", "Amount": "", "Transaction ID": ""},
    ]
    balances = [
        {
            "Date": _us(d),
            "Account": "TS Checking",
            "Account #": "xxxx4821",
            "Institution": "TS Bank",
            "Account ID": "acct-TS Checking",
            "Balance": 1834.12,
        },
    ]
    feed = tiller.build_feed(rows, balances, since=TODAY - timedelta(days=60))
    assert [t.external_id for t in feed.transactions] == ["tid-1", "tid-2"]
    assert feed.transactions[0].amount == Decimal("-4.50")

    sid = await _source("tiller", sheet_id="x" * 30)
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    assert res["new"] == 2 and res["inserted"] == 2, res

    async with get_sessionmaker()() as s:
        acct = await s.scalar(select(Account).where(Account.name == "TS Checking"))
        assert acct.external_refs["tiller"] == "acct-TS Checking" and acct.mask == "4821"
        txns = (await s.scalars(select(Transaction).where(Transaction.external_id.in_(["tid-1", "tid-2"])))).unique().all()
        assert {t.source_type for t in txns} == {"tiller"}
        assert next(t for t in txns if t.external_id == "tid-1").category_id == cat["id"]
        assert (await s.scalar(select(AccountBalance.balance).where(AccountBalance.account_id == acct.id))) == Decimal(
            "1834.12"
        )

    # Re-sync is idempotent; a category assigned later in Tiller fills the uncategorized row
    rows[1]["Category"] = "TS Coffee"
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), tiller.build_feed(rows, balances, None))
    assert res["new"] == 1 and res["categories_filled"] == 1  # only the >60-day-old row is new when not limited

    bal = (await client.get("/api/balances")).json()
    assert any(a["account"] == "TS Checking" and a["balance"] == 1834.12 for a in bal["accounts"])
    trend = (await client.get("/api/balances/trend")).json()
    assert trend[str(acct.id)] == [{"as_of": d.isoformat(), "balance": 1834.12}]


async def test_feed_duplicates_against_manual_entries(client, no_ai):
    acct = (
        await client.post("/api/accounts", json={"name": "TS Card", "account_type": "credit_card", "mask": "6173"})
    ).json()
    d = TODAY - timedelta(days=2)
    for desc, amt in (("TS GROCER", "-52.10"), ("TS HARDWARE STORE", "-19.99")):
        r = await client.post(
            "/api/transactions",
            json={"txn_date": d.isoformat(), "description": desc, "amount": amt, "account_id": acct["id"]},
        )
        assert r.status_code == 201
    sid = await _source("simplefin")
    feed = tiller.build_feed(
        [
            _tiller_row(d, "TS GROCER", -52.10, "dup-exact", account="Visa 6173", number="6173"),
            _tiller_row(d, "HARDWARE #22 TS", -19.99, "dup-fuzzy", account="Visa 6173", number="6173"),
            _tiller_row(d, "TS FRESH", -8.37, "new-1", account="Visa 6173", number="6173"),
        ],
        [],
        None,
    )
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    # Matched to the existing card by last-4; the exact duplicate is skipped, the fuzzy one needs a person
    assert res["needs_review"] == 1 and res["inserted"] == 0
    async with get_sessionmaker()() as s:
        linked = await s.get(Account, acct["id"])
        assert linked.external_refs.get("simplefin") == "acct-Visa 6173"
        batch = await s.get(ImportBatch, res["batch_id"])
        assert batch.status == "review" and batch.origin == "simplefin"

    # The pending batch's rows aren't re-queued on the next sync; with a confident AI verdict it auto-commits
    async with get_sessionmaker()() as s:
        again = await apply_feed(s, await s.get(Source, sid), feed)
    assert again["new"] == 0
    await client.delete(f"/api/imports/{res['batch_id']}")
    no_ai["HARDWARE #22 TS"] = 0.97
    async with get_sessionmaker()() as s:
        res = await apply_feed(s, await s.get(Source, sid), feed)
    assert res["inserted"] == 1 and res["skipped_duplicates"] == 2


def test_tiller_rows_without_ids_get_stable_ids():
    d = date(2012, 5, 4)
    live = _tiller_row(TODAY, "TS NEW", -1.0, "tid-live")
    manual = {**_tiller_row(d, "TS DINER", -12.5, ""), "Account ID": "", "Account #": ""}
    twin = dict(manual)
    feed = tiller.build_feed([live, manual, twin], [], None)
    ids = [t.external_id for t in feed.transactions]
    assert ids[0] == "tid-live" and ids[1].startswith("tiller:") and ids[1] != ids[2]
    assert ids == [t.external_id for t in tiller.build_feed([live, manual, twin], [], None).transactions]
    # The hand-entered rows share the account id Tiller assigned to the same account
    assert {t.account_key for t in feed.transactions} == {"acct-TS Checking"}
    assert feed.synthetic_ids == 2 and feed.warnings == []


def test_simplefin_token_and_feed():
    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    assert simplefin.claim_url(token) == "https://bridge.example.org/simplefin/claim/abc"
    with pytest.raises(simplefin.SimpleFinError):
        simplefin.claim_url(base64.b64encode(b"http://insecure.example.org/claim").decode())

    payload = {
        "errors": ["Connection to Old Bank needs attention"],
        "accounts": [
            {
                "org": {"name": "SF Brokerage", "domain": "sfb.example"},
                "id": "ACT-1",
                "name": "Individual (5555)",
                "currency": "USD",
                "balance": "10250.55",
                "available-balance": "0",
                "balance-date": 1790000000,
                "transactions": [
                    {"id": "T1", "posted": 1789900000, "amount": "-25.00", "description": "SF FEE", "payee": ""},
                    {"id": "T2", "posted": 0, "amount": "-1.00", "description": "PENDING", "pending": True},
                ],
                "holdings": [
                    {
                        "id": "H1",
                        "symbol": "VTI",
                        "description": "Vanguard Total",
                        "shares": "10",
                        "market_value": "2800.10",
                        "cost_basis": "2000",
                    },
                ],
            }
        ],
    }
    feed = simplefin.build_feed(payload)
    assert feed.warnings == ["Connection to Old Bank needs attention"]
    assert [t.external_id for t in feed.transactions] == ["sfin:ACT-1:T1"]
    assert feed.balances[0].balance == Decimal("10250.55")
    assert feed.holdings[0].symbol == "VTI" and feed.holdings[0].shares == Decimal("10")
    assert feed.accounts["ACT-1"].institution == "SF Brokerage"

    v2 = simplefin.build_feed(
        {
            "errlist": [{"code": "con.auth", "msg": "Authentication failed for My Bank", "conn_id": "C1"}],
            "connections": [{"conn_id": "C1", "name": "My Bank - Jo", "org_name": "My Bank", "org_id": "O1"}],
            "accounts": [
                {
                    "id": "A",
                    "conn_id": "C1",
                    "name": "Checking",
                    "balance": "1",
                    "balance-date": 1790000000,
                    "transactions": [{"id": "X", "posted": 1789900000, "amount": "1.00", "description": "D"}],
                },
            ],
        }
    )
    assert v2.warnings == ["Authentication failed for My Bank"]
    assert v2.accounts["C1/A"].institution == "My Bank"
    assert v2.transactions[0].external_id == "sfin:C1/A:X"


async def test_simplefin_connect_and_sync(client, no_ai, monkeypatch):
    async def fake_claim(token):
        return "https://user:pass@bridge.example.org/simplefin"

    seen = {}

    async def fake_fetch(access_url, config, full=False):
        seen["url"] = access_url
        return simplefin.build_feed(
            {
                "accounts": [
                    {
                        "org": {"name": "SF Bank"},
                        "id": "ACT-9",
                        "name": "SF Savings (7070)",
                        "balance": "500.00",
                        "balance-date": 1790000000,
                        "transactions": [
                            {"id": "SF-T1", "posted": 1789990000, "amount": "12.00", "description": "INTEREST"}
                        ],
                        "holdings": [{"id": "HX", "symbol": "CASH", "market_value": "500"}],
                    }
                ]
            }
        )

    monkeypatch.setattr(simplefin, "claim", fake_claim)
    monkeypatch.setattr(simplefin, "fetch", fake_fetch)
    r = await client.post("/api/sources/simplefin/claim", json={"setup_token": "x" * 40})
    assert r.status_code == 200, r.text
    src = r.json()
    assert src["connected"] and "secret" not in src

    async with get_sessionmaker()() as s:
        stored = await s.get(Source, src["id"])
        assert "pass" not in stored.secret  # encrypted at rest
        res = await run_sync(s, src["id"])
    assert seen["url"] == "https://user:pass@bridge.example.org/simplefin"
    assert res["inserted"] == 1 and res["holdings"] == 1

    listed = (await client.get("/api/sources")).json()["sources"]
    sf = next(x for x in listed if x["kind"] == "simplefin")
    assert sf["last_status"] == "ok" and sf["linked_accounts"] >= 1
    holdings = (await client.get("/api/holdings")).json()
    assert any(h["symbol"] == "CASH" and h["account"] == "SF Savings (7070)" for h in holdings)


async def test_sync_failure_recorded(client, monkeypatch):
    async def boom(config, full=False):
        raise tiller.TillerError("Google returned 403: share the sheet")

    monkeypatch.setattr(tiller, "fetch", boom)
    sid = await _source("tiller", sheet_id="y" * 30)
    async with get_sessionmaker()() as s:
        with pytest.raises(tiller.TillerError):
            await run_sync(s, sid)
    async with get_sessionmaker()() as s:
        src = await s.get(Source, sid)
        assert src.last_status == "failed" and "403" in src.last_error
