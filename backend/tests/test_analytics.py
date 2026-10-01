from datetime import date

import pytest


@pytest.fixture(scope="module")
async def data(client):
    g = (await client.post("/api/category-groups", json={"name": "TA Group"})).json()
    dining = (await client.post("/api/categories", json={"name": "TA Dining", "group_id": g["id"]})).json()
    shop = (await client.post("/api/categories", json={"name": "TA Shopping", "group_id": g["id"]})).json()
    salary = (await client.post("/api/category-groups", json={"name": "TA Income", "type": "income"})).json()
    pay = (await client.post("/api/categories", json={"name": "TA Pay", "group_id": salary["id"], "type": "income"})).json()

    async def add(d, desc, amt, cat):
        r = await client.post(
            "/api/transactions", json={"txn_date": d, "description": desc, "amount": amt, "category_id": cat}
        )
        assert r.status_code == 201, r.text
        return r.json()

    for i, m in enumerate(
        [
            "2023-06",
            "2023-07",
            "2023-08",
            "2023-09",
            "2023-10",
            "2023-11",
            "2023-12",
            "2024-01",
            "2024-02",
            "2024-03",
            "2024-04",
            "2024-05",
        ]
    ):
        await add(f"{m}-10", "TA BISTRO", f"-{95 + i % 3 * 5}.00", dining["id"])
    for m in ("2024-02", "2024-03", "2024-04", "2024-05"):
        await add(f"{m}-12", "TA HARDWARE", "-40.00", shop["id"])
    await add("2024-06-03", "TA BISTRO", "-180.00", dining["id"])
    await add("2024-06-10", "TA STEAKHOUSE", "-220.00", dining["id"])
    await add("2024-06-12", "TA HARDWARE", "-180.00", shop["id"])
    jewel = await add("2024-06-15", "TA JEWELRY", "-900.00", shop["id"])
    await add("2024-06-18", "TA NEW PLACE", "-250.00", shop["id"])
    await add("2024-06-01", "TA PAYROLL", "3000.00", pay["id"])
    return {"dining": dining, "jewel": jewel}


async def test_detect_anomalies(client, data):
    from ledger.analytics.anomalies import detect
    from ledger.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        res = await detect(session, date(2024, 6, 1))
    assert res["found"] >= 4

    found = [a for a in (await client.get("/api/anomalies")).json() if a["period"] == "2024-06-01"]
    kinds = {a["kind"]: a for a in found}
    assert kinds["category_spike"]["category_name"] == "TA Dining"
    assert kinds["category_spike"]["amount"] == 400.0
    assert kinds["large_for_merchant"]["transaction_description"] == "TA HARDWARE"
    assert kinds["large_transaction"]["transaction_description"] == "TA JEWELRY"
    new = {a["transaction_description"] for a in found if a["kind"] == "new_merchant"}
    assert new == {"TA NEW PLACE", "TA STEAKHOUSE"}

    # Idempotent; dismissed findings stay dismissed
    spike = kinds["category_spike"]
    assert (await client.post(f"/api/anomalies/{spike['id']}/dismiss")).json()["status"] == "dismissed"
    async with get_sessionmaker()() as session:
        await detect(session, date(2024, 6, 1))
    again = [a for a in (await client.get("/api/anomalies")).json() if a["period"] == "2024-06-01"]
    assert len(again) == len(found) - 1

    # A finding that no longer holds is withdrawn
    await client.delete(f"/api/transactions/{data['jewel']['id']}")
    async with get_sessionmaker()() as session:
        res = await detect(session, date(2024, 6, 1))
    assert res["withdrawn"] == 1


async def test_reports(client, data):
    flow = (await client.get("/api/analytics/cashflow", params={"start": "2024-05-01", "end": "2024-06-30"})).json()
    assert [m["month"] for m in flow] == ["2024-05-01", "2024-06-01"]
    june = flow[1]
    assert june["income"] >= 3000 and june["expenses"] >= 830  # jewelry deleted in the previous test

    cats = (await client.get("/api/analytics/categories", params={"start": "2024-06-01", "end": "2024-06-30"})).json()
    by = {r["category"]: r for r in cats["rows"]}
    assert by["TA Dining"]["spent"] == 400.0 and by["TA Dining"]["group"] == "TA Group"

    trend = (
        await client.get(
            "/api/analytics/category-trend",
            params={"start": "2024-01-01", "end": "2024-06-30", "level": "category", "ids": [data["dining"]["id"]]},
        )
    ).json()
    assert trend["series"][0]["name"] == "TA Dining" and trend["series"][0]["values"][-1] == 400.0

    june_only = {"start": "2024-06-01", "end": "2024-06-30", "level": "category"}
    spend = (await client.get("/api/analytics/category-trend", params={**june_only, "top": 1, "other": True})).json()
    assert sum(s["values"][0] for s in spend["series"]) == pytest.approx(june["expenses"])
    assert spend["series"][-1]["name"] == "Other"
    income = (await client.get("/api/analytics/category-trend", params={**june_only, "kind": "income"})).json()
    assert {s["name"]: s["values"][0] for s in income["series"]}["TA Pay"] == 3000.0

    june_range = {"start": "2024-06-01", "end": "2024-06-30"}
    dining = data["dining"]["id"]
    contrib = (
        await client.get(
            "/api/analytics/contributors", params={**june_range, "kind": "expense", "ids": [dining], "limit": 1}
        )
    ).json()
    assert contrib["count"] == 2 and contrib["out"] == 400.0
    assert contrib["merchants"][0]["name"] == "TA STEAKHOUSE" and len(contrib["merchants"]) == 1
    assert contrib["transactions"][0]["amount"] == -220.0
    everything = (
        await client.get("/api/analytics/contributors", params={**june_range, "kind": "expense", "basis": "all"})
    ).json()
    rest = (
        await client.get(
            "/api/analytics/contributors", params={**june_range, "kind": "expense", "exclude": [dining], "basis": "all"}
        )
    ).json()
    assert rest["count"] == everything["count"] - 2
    assert rest["out"] == pytest.approx(everything["out"] - 400.0)

    merch = (await client.get("/api/analytics/merchants", params={"start": "2024-06-01", "end": "2024-06-30"})).json()
    assert merch[0]["merchant"] == "ta new place" or merch[0]["spent"] >= 250

    csv_text = (
        await client.get("/api/transactions/export.csv", params={"start": "2024-06-01", "end": "2024-06-30", "q": "TA "})
    ).text
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("id,date,posted_date,description,amount")
    assert any("TA STEAKHOUSE" in line for line in lines[1:])

    span = (await client.get("/api/analytics/span")).json()
    assert span["start"] <= "2024-05-01" and span["end"] >= "2024-06-01"
