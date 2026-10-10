from datetime import date, timedelta
from decimal import Decimal

import pytest

from ledger.assets import rentcast
from ledger.assets.service import depreciate, record_value
from ledger.db.engine import get_sessionmaker

TODAY = date.today()
YEAR_AGO = TODAY - timedelta(days=365)


def test_depreciate():
    assert abs(depreciate(Decimal("30000"), YEAR_AGO, TODAY, Decimal("0.15")) - Decimal("25500")) < 10
    assert depreciate(Decimal("30000"), TODAY, TODAY, Decimal("0.15")) == Decimal("30000.00")


@pytest.fixture
def fake_rentcast(monkeypatch):
    calls = []

    async def estimate(address):
        calls.append(address)
        if "nowhere" in address:
            raise rentcast.RentCastError("RentCast couldn't find that address")
        return {"price": 512345, "priceRangeLow": 480000, "priceRangeHigh": 540000, "comparables": [{}, {}]}

    monkeypatch.setattr(rentcast, "estimate", estimate)
    return calls


def _asset(info, account_id):
    return next(a for a in info["assets"] if a["account_id"] == account_id)


async def test_home_with_rentcast_and_loan(client, fake_rentcast):
    loan = (await client.post("/api/accounts", json={"name": "AS Mortgage", "account_type": "mortgage"})).json()
    r = await client.post(
        "/api/assets",
        json={
            "name": "AS House",
            "account_type": "property",
            "method": "rentcast",
            "address": "1 Main St, Town, VA, 22000",
            "loan_account_id": loan["id"],
        },
    )
    assert r.status_code == 201, r.text
    home_id = r.json()["account_id"]
    assert r.json()["valuation"]["value"] == 512345
    assert fake_rentcast == ["1 Main St, Town, VA, 22000"]

    # A recent estimate isn't re-fetched on save; an explicit refresh always calls RentCast.
    r = await client.put(
        f"/api/assets/{home_id}",
        json={"method": "rentcast", "address": "1 Main St, Town, VA, 22000", "loan_account_id": loan["id"]},
    )
    assert "skipped" in r.json()["valuation"]
    assert len(fake_rentcast) == 1
    assert (await client.post(f"/api/assets/{home_id}/refresh")).status_code == 200
    assert len(fake_rentcast) == 2

    info = (await client.get("/api/assets")).json()
    home = _asset(info, home_id)
    assert home["value"] == 512345 and home["value_source"] == "rentcast" and home["equity"] is None

    async with get_sessionmaker()() as s:
        await record_value(s, loan["id"], TODAY, Decimal("400000"), "tiller")
        await s.commit()
    home = _asset((await client.get("/api/assets")).json(), home_id)
    assert home["loan_balance"] == 400000 and home["equity"] == 112345

    bal = (await client.get("/api/balances")).json()
    assert any(a["account_id"] == home_id and a["balance"] == 512345 for a in bal["accounts"])

    r = await client.put(
        f"/api/assets/{home_id}",
        json={"method": "rentcast", "address": "nowhere", "loan_account_id": loan["id"]},
    )
    assert r.json()["valuation"]["error"]
    home = _asset((await client.get("/api/assets")).json(), home_id)
    assert "find that address" in home["last_error"] and home["value"] == 512345
    assert (await client.post(f"/api/assets/{home_id}/refresh")).status_code == 502


async def test_vehicle_depreciation_and_manual_anchor(client):
    r = await client.post(
        "/api/assets",
        json={
            "name": "AS Car",
            "account_type": "vehicle",
            "method": "depreciation",
            "purchase_price": "30000",
            "purchase_date": YEAR_AGO.isoformat(),
            "depreciation_rate": "0.15",
        },
    )
    assert r.status_code == 201, r.text
    car = r.json()["account_id"]
    assert r.json()["valuation"]["value"] == float(depreciate(Decimal("30000"), YEAR_AGO, TODAY, Decimal("0.15")))

    # A back-dated recorded value re-anchors the curve.
    r = await client.post(f"/api/assets/{car}/valuations", json={"value": "20000", "as_of": YEAR_AGO.isoformat()})
    expected = float(depreciate(Decimal("20000"), YEAR_AGO, TODAY, Decimal("0.15")))
    assert r.json()["valuation"]["value"] == expected
    assert _asset((await client.get("/api/assets")).json(), car)["value"] == expected

    # Today's recorded value wins over the curve.
    await client.post(f"/api/assets/{car}/valuations", json={"value": "21000"})
    a = _asset((await client.get("/api/assets")).json(), car)
    assert a["value"] == 21000 and a["value_source"] == "manual"

    assert (await client.post(f"/api/assets/{car}/valuations", json={"value": "1", "as_of": "2999-01-01"})).status_code == 422


async def test_asset_validation(client):
    r = await client.post("/api/assets", json={"name": "AS Bad", "account_type": "property", "method": "rentcast"})
    assert r.status_code == 422
    chk = (await client.post("/api/accounts", json={"name": "AS Checking", "account_type": "checking"})).json()
    r = await client.post(
        "/api/assets", json={"name": "AS Boat", "account_type": "vehicle", "loan_account_id": chk["id"]}
    )
    assert r.status_code == 422
    assert (await client.post(f"/api/assets/{chk['id']}/valuations", json={"value": "5"})).status_code == 404
