from datetime import date

import pytest

from ledger.analytics.entity import cadence


@pytest.fixture(scope="module")
async def td(client):
    g = (await client.post("/api/category-groups", json={"name": "TD Group"})).json()
    subs = (await client.post("/api/categories", json={"name": "TD Subscriptions", "group_id": g["id"]})).json()
    misc = (await client.post("/api/categories", json={"name": "TD Misc", "group_id": g["id"]})).json()
    acct = (await client.post("/api/accounts", json={"name": "TD Card", "account_type": "credit_card"})).json()
    ids = []
    for m in range(1, 7):
        r = await client.post(
            "/api/transactions",
            json={
                "txn_date": f"2025-{m:02d}-05",
                "description": "TD STREAMFLIX",
                "amount": "-15.00" if m < 6 else "-45.00",
                "category_id": subs["id"],
                "account_id": acct["id"],
            },
        )
        ids.append(r.json()["id"])
    await client.post(
        "/api/transactions",
        json={"txn_date": "2025-03-05", "description": "TD CORNER SHOP", "amount": "-7.50", "category_id": misc["id"],
              "account_id": acct["id"]},
    )
    await client.post(
        "/api/transactions",
        json={"txn_date": "2025-04-01", "description": "TD REFUND", "amount": "20.00", "category_id": misc["id"],
              "account_id": acct["id"]},
    )
    return {"group": g, "subs": subs, "misc": misc, "acct": acct, "ids": ids}


async def test_transaction_context(client, td):
    last = (await client.get(f"/api/transactions/{td['ids'][-1]}")).json()
    assert last["category_group_id"] == td["group"]["id"]
    ctx = (await client.get(f"/api/transactions/{td['ids'][-1]}/context")).json()
    assert ctx["merchant"] == last["merchant"]
    assert len(ctx["charges"]) == 6 and ctx["stats"]["spent"] == 120.0
    assert ctx["comparison"]["median"] == 15.0 and ctx["comparison"]["rank_pct"] == 1.0
    assert ctx["cadence"]["label"] == "monthly"
    assert {t["description"] for t in ctx["same_day"]} == set()
    assert (await client.get("/api/transactions/999999999/context")).status_code == 404


async def test_merchant_detail(client, td):
    key = (await client.get(f"/api/transactions/{td['ids'][0]}")).json()["merchant"]
    d = (await client.get("/api/merchants/detail", params={"key": key, "start": "2025-01-01", "end": "2025-06-30"})).json()
    assert d["name"] == "Td Streamflix" and d["latest_description"] == "TD STREAMFLIX" and d["stats"]["count"] == 6
    assert [m["spent"] for m in d["monthly"]] == [15.0] * 5 + [45.0]
    assert d["categories"][0]["name"] == "TD Subscriptions" and d["accounts"][0]["name"] == "TD Card"
    assert "rule" in d
    assert d["yearly"][0] == {"year": 2025, "spent": 120.0, "received": 0.0, "net": -120.0, "count": 6}
    page = (await client.get("/api/transactions", params={"merchant": key, "status": "all"})).json()
    assert page["total"] == 6
    assert (await client.get("/api/merchants/detail", params={"key": "td no such merchant"})).status_code == 404


async def test_account_category_group_detail(client, td):
    params = {"start": "2025-01-01", "end": "2025-06-30"}
    a = (await client.get(f"/api/accounts/{td['acct']['id']}/detail", params=params)).json()
    assert a["account"]["name"] == "TD Card" and a["stats"]["count"] == 8
    assert a["stats"]["received"] == 20.0 and a["balance"] is None
    assert {m["name"] for m in a["merchants"]} >= {"TD STREAMFLIX", "TD CORNER SHOP"}

    c = (await client.get(f"/api/categories/{td['misc']['id']}/detail", params=params)).json()
    assert c["category"]["group_name"] == "TD Group" and c["stats"]["spent"] == 7.5 and c["stats"]["received"] == 20.0
    assert len(c["monthly"]) == 6 and c["budget"] is None

    r = await client.post("/api/budgets", json={"group_id": td["group"]["id"], "amount": "300.00", "period_type": "quarter"})
    assert r.status_code in (200, 201), r.text
    g = (await client.get(f"/api/category-groups/{td['group']['id']}/detail", params=params)).json()
    assert g["budget"]["monthly"] == 100.0
    assert {x["name"]: x["spent"] for x in g["categories"]} == {"TD Misc": 7.5, "TD Subscriptions": 120.0}
    assert g["trend"]["series"][0]["name"] == "TD Subscriptions" and len(g["trend"]["months"]) == 6

    anomalies = await client.get("/api/anomalies", params={"status": "all", "group_id": td["group"]["id"]})
    assert anomalies.status_code == 200


def test_cadence():
    rows = [{"date": date(2025, m, 1), "amount": -10.0} for m in range(1, 5)]
    c = cadence(rows, today=date(2025, 5, 2))
    assert c["label"] == "monthly" and c["next_date"] == date(2025, 5, 2) and not c["lapsed"]
    assert cadence(rows, today=date(2025, 7, 1))["lapsed"]
    assert cadence(rows[:2]) is None
    days = (date(2025, 1, 1), date(2025, 1, 9), date(2025, 3, 20), date(2025, 3, 22))
    assert cadence([{"date": d, "amount": -5.0} for d in days]) is None
