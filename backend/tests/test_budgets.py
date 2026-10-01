from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture(scope="module")
async def cats(client):
    g = (await client.post("/api/category-groups", json={"name": "TB Group", "type": "expense"})).json()
    groc = (await client.post("/api/categories", json={"name": "TB Groceries", "group_id": g["id"]})).json()
    ins = (await client.post("/api/categories", json={"name": "TB Insurance", "group_id": g["id"]})).json()
    for d, desc, amt, cat, spread in (
        ("2025-03-02", "TB MARKET", "-150.00", groc, None),
        ("2025-03-09", "TB MARKET", "-120.00", groc, None),
        ("2025-03-10", "TB MARKET REFUND", "20.00", groc, None),
        ("2025-03-05", "TB BULK WAREHOUSE", "-600.00", groc, 3),
        ("2025-01-10", "TB STATE FARM ANNUAL", "-1200.00", ins, None),
    ):
        body = {"txn_date": d, "description": desc, "amount": amt, "category_id": cat["id"], "budget_spread_months": spread}
        assert (await client.post("/api/transactions", json=body)).status_code == 201
    return {"group": g, "groc": groc, "ins": ins}


async def test_budget_status_with_spreading(client, cats):
    r = await client.post(
        "/api/spread-rules",
        json={"name": "Annual insurance", "category_id": cats["ins"]["id"], "min_amount": "500", "months": 12},
    )
    assert r.status_code == 201, r.text
    assert r.json()["matches_12m"] >= 0
    assert (await client.post("/api/spread-rules", json={"name": "bad"})).status_code == 422

    b1 = (await client.post("/api/budgets", json={"category_id": cats["groc"]["id"], "amount": "400"})).json()
    await client.post("/api/budgets", json={"category_id": cats["ins"]["id"], "period_type": "year", "amount": "1200"})
    assert (await client.post("/api/budgets", json={"category_id": cats["groc"]["id"], "amount": "1"})).status_code == 409
    assert (await client.post("/api/budgets", json={"amount": "1"})).status_code == 422

    s = (await client.get("/api/budgets/status", params={"period": "month", "on": "2025-03-15"})).json()
    assert s["period"]["label"] == "March 2025" and s["period"]["elapsed"] == 1.0
    rows = {r["name"]: r for r in s["rows"]}
    assert rows["TB Groceries"]["actual"] == 450  # 150 + 120 - 20 + 600/3
    assert rows["TB Groceries"]["status"] == "over"
    assert rows["TB Insurance"]["budget"] == 100 and rows["TB Insurance"]["actual"] == 100
    assert rows["TB Insurance"]["spread_amount"] == 100

    q = (await client.get("/api/budgets/status", params={"period": "quarter", "on": "2025-02-01"})).json()
    rows = {r["name"]: r for r in q["rows"]}
    assert q["period"]["label"] == "Q1 2025"
    assert rows["TB Groceries"]["budget"] == 1200 and rows["TB Groceries"]["actual"] == 450
    assert rows["TB Insurance"]["actual"] == 300

    # Next month still carries the spread shares
    apr = (await client.get("/api/budgets/status", params={"period": "month", "on": "2025-04-01"})).json()
    rows = {r["name"]: r for r in apr["rows"]}
    assert rows["TB Groceries"]["actual"] == 200 and rows["TB Insurance"]["actual"] == 100

    # Group budget covers both categories; per-category budgets removed so totals don't double count
    await client.delete(f"/api/budgets/{b1['id']}")
    gb = (await client.post("/api/budgets", json={"group_id": cats["group"]["id"], "amount": "500"})).json()
    assert gb["name"] == "TB Group"
    s = (await client.get("/api/budgets/status", params={"period": "month", "on": "2025-03-15"})).json()
    rows = {r["name"]: r for r in s["rows"]}
    assert rows["TB Group"]["actual"] == 550


async def test_budget_suggestions(client, cats):
    from ledger.budgets.service import suggest_budgets
    from ledger.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        out = await suggest_budgets(session, date(2025, 4, 15), months=3)
    by = {r["name"]: r for r in out}
    # Groceries now has no category budget (deleted above); insurance still does and is excluded
    assert by["TB Groceries"]["monthly_average"] == Decimal("150.00") and by["TB Groceries"]["suggested"] == 150
    assert "TB Insurance" not in by
