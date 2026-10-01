import pytest


@pytest.fixture(scope="module")
async def rx(client):
    r = await client.post("/api/accounts", json={"name": "RX Rules Checking", "account_type": "checking", "mask": "7301"})
    assert r.status_code == 201, r.text
    acct = r.json()
    cats = {c["name"]: c["id"] for c in (await client.get("/api/categories")).json()}

    async def add(desc, amt, **kw):
        body = {"txn_date": "2026-07-15", "description": desc, "amount": amt, "account_id": acct["id"], **kw}
        r = await client.post("/api/transactions", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    return {"acct": acct, "cats": cats, "add": add}


async def test_contains_rule_preview_and_apply(client, rx):
    groc, water = rx["cats"]["Groceries"], rx["cats"]["Water"]
    a = await rx["add"]("RXQ STREAMING SVC 001", "-15.00")
    b = await rx["add"]("POS RXQ STREAMING SVC", "-15.00", category_id=water)
    c = await rx["add"]("RXQ STREAMING SVC BIG", "-150.00")

    spec = {"match_type": "contains", "pattern": "rxq streaming", "category_id": groc, "amount_max": "50"}
    p = (await client.post("/api/rules/preview", json=spec)).json()
    assert (p["matches"], p["uncategorized"], p["auto"], p["user"], p["same"]) == (2, 1, 0, 1, 0)
    assert {s["id"] for s in p["samples"]} == {a["id"], b["id"]}

    rule = (await client.post("/api/rules", json=spec)).json()
    assert rule["description"].startswith("Description contains")
    assert (await client.post("/api/rules", json=spec)).status_code == 409

    assert (await client.post(f"/api/rules/{rule['id']}/apply", json={})).json() == {"updated": 1}
    assert (await client.get(f"/api/transactions/{a['id']}")).json()["category_rule_id"] == rule["id"]
    assert (await client.get(f"/api/transactions/{b['id']}")).json()["category_id"] == water
    assert (await client.get(f"/api/transactions/{c['id']}")).json()["category_id"] is None

    r = await client.post(f"/api/rules/{rule['id']}/apply", json={"include_user": True})
    assert r.json() == {"updated": 1}
    assert (await client.get(f"/api/transactions/{b['id']}")).json()["category_source"] == "rule"
    listed = {x["id"]: x for x in (await client.get("/api/rules")).json()}
    assert listed[rule["id"]]["applied_count"] == 2
    assert (await client.get("/api/transactions", params={"rule_id": rule["id"]})).json()["total"] == 2


async def test_precedence_regex_and_delete(client, rx):
    groc, water, net = rx["cats"]["Groceries"], rx["cats"]["Water"], rx["cats"]["Cable/Internet"]
    r1 = (
        await client.post("/api/rules", json={"match_type": "regex", "pattern": "^rxz (gas|fuel)", "category_id": water})
    ).json()
    r2 = (
        await client.post(
            "/api/rules", json={"match_type": "contains", "pattern": "rxz fuel", "category_id": net, "priority": 50}
        )
    ).json()
    assert (await rx["add"]("RXZ FUEL 42", "-30.00"))["category_rule_id"] == r2["id"]
    assert (await rx["add"]("RXZ GAS 42", "-30.00"))["category_rule_id"] == r1["id"]

    keys = ("match_type", "pattern", "category_id", "account_id", "amount_min", "amount_max", "priority", "note")
    r = await client.put(f"/api/rules/{r2['id']}", json={k: r2[k] for k in keys} | {"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    t = await rx["add"]("RXZ FUEL 43", "-30.00")
    assert t["category_rule_id"] == r1["id"]
    matches = (await client.get(f"/api/rules/for-transaction/{t['id']}")).json()
    assert [m["id"] for m in matches] == [r1["id"], r2["id"]]

    bad = {"match_type": "regex", "pattern": "(", "category_id": groc}
    assert (await client.post("/api/rules", json=bad)).status_code == 422
    assert (await client.post("/api/rules/preview", json=bad)).status_code == 422

    assert (await client.delete(f"/api/rules/{r1['id']}")).status_code == 204
    t = (await client.get(f"/api/transactions/{t['id']}")).json()
    assert t["category_id"] == water and t["category_rule_id"] is None


async def test_replace_existing(client, rx):
    groc, water = rx["cats"]["Groceries"], rx["cats"]["Water"]
    a = (await client.post("/api/rules", json={"pattern": "rxw hardware", "category_id": groc})).json()
    b = await client.post("/api/rules", json={"pattern": "RXW Hardware", "category_id": water, "replace_existing": True})
    assert b.status_code == 201 and b.json()["id"] == a["id"] and b.json()["category_id"] == water


async def test_category_aliases(client, rx):
    groc, water = rx["cats"]["Groceries"], rx["cats"]["Water"]
    r = await client.post("/api/category-aliases", json={"alias": "  RX Food & Dining ", "category_id": groc})
    assert r.status_code == 201 and r.json()["alias"] == "rx food & dining"
    alias_id = r.json()["id"]
    assert (
        await client.post("/api/category-aliases", json={"alias": "rx food & dining", "category_id": water})
    ).status_code == 409
    r = await client.put(f"/api/category-aliases/{alias_id}", json={"alias": "rx food & dining", "category_id": water})
    assert r.json()["category_name"] == "Water"
    assert any(x["id"] == alias_id for x in (await client.get("/api/category-aliases")).json())
    assert (await client.delete(f"/api/category-aliases/{alias_id}")).status_code == 204
