import pytest


@pytest.fixture(scope="module")
async def tm(client):
    cat = (await client.get("/api/categories")).json()[0]

    async def add(desc, amt="-5.00", d="2025-02-01"):
        r = await client.post("/api/transactions", json={"txn_date": d, "description": desc, "amount": amt})
        assert r.status_code == 201, r.text
        return r.json()

    a = await add("TM COFFEE #123 SPRINGFIELD IL")
    b = await add("TM COFFEE SHOP 99999", d="2025-02-03")
    # Category chosen on the source merchant creates a learned rule that should follow the merge.
    r = await client.patch(f"/api/transactions/{b['id']}", json={"category_id": cat["id"]})
    assert r.status_code == 200
    return {"a": a, "b": b, "cat": cat, "add": add}


async def test_keys_and_merge(client, tm):
    a, b = tm["a"], tm["b"]
    assert a["merchant"] == "tm coffee springfield" and a["merchant_name"] == "Tm Coffee Springfield"
    assert b["merchant"] == "tm coffee shop" and a["merchant_source"] is None

    r = await client.post("/api/merchants/merge", json={"source": "tm coffee shop", "target": "tm coffee springfield"})
    assert r.json() == {"key": "tm coffee springfield", "moved": 1}
    assert (await client.get(f"/api/transactions/{b['id']}")).json()["merchant"] == "tm coffee springfield"

    d = (await client.get("/api/merchants/detail", params={"key": "tm coffee shop"})).json()
    assert d["key"] == "tm coffee springfield" and d["aliases"] == ["tm coffee shop"] and d["stats"]["count"] == 2
    assert d["rule"]["category_id"] == tm["cat"]["id"]
    assert {x["description"] for x in d["descriptions"]} == {"TM COFFEE #123 SPRINGFIELD IL", "TM COFFEE SHOP 99999"}

    c = await tm["add"]("TM COFFEE SHOP 5555")
    assert c["merchant"] == "tm coffee springfield" and c["category_id"] == tm["cat"]["id"]

    same = {"source": "tm coffee springfield", "target": "tm coffee springfield"}
    assert (await client.post("/api/merchants/merge", json=same)).status_code == 422

    r = await client.put("/api/merchants/name", json={"key": "tm coffee springfield", "display_name": "TM Coffee"})
    assert r.json()["name"] == "TM Coffee"
    assert (await client.get(f"/api/transactions/{a['id']}")).json()["merchant_name"] == "TM Coffee"
    found = (await client.get("/api/merchants/search", params={"q": "tm coffee"})).json()
    assert [(m["key"], m["name"], m["count"]) for m in found] == [("tm coffee springfield", "TM Coffee", 3)]

    r = await client.post("/api/merchants/unmerge", json={"key": "tm coffee shop"})
    assert r.json()["moved"] == 2
    assert (await client.get(f"/api/transactions/{c['id']}")).json()["merchant"] == "tm coffee shop"


async def test_transaction_override(client, tm):
    a = tm["a"]
    r = await client.patch(f"/api/transactions/{a['id']}", json={"merchant_name": "TM Beans & Co"})
    t = r.json()
    assert (t["merchant"], t["merchant_name"], t["merchant_source"]) == ("tm beans & co", "TM Beans & Co", "user")

    t = (await client.patch(f"/api/transactions/{a['id']}", json={"description": "TM COFFEE #9 SHELBYVILLE"})).json()
    assert t["merchant"] == "tm beans & co"

    from ledger.db.engine import get_sessionmaker
    from ledger.maintenance import renormalize

    async with get_sessionmaker()() as session:
        await renormalize(session)
    assert (await client.get(f"/api/transactions/{a['id']}")).json()["merchant"] == "tm beans & co"

    # Typing an existing display name picks that merchant rather than a new key.
    t = (await client.patch(f"/api/transactions/{a['id']}", json={"merchant_name": "tm coffee"})).json()
    assert t["merchant"] == "tm coffee springfield" and t["merchant_source"] == "user"

    t = (await client.patch(f"/api/transactions/{a['id']}", json={"merchant_name": ""})).json()
    assert (t["merchant"], t["merchant_source"]) == ("tm coffee shelbyville", None)

    body = {"txn_date": "2025-03-01", "description": "SQ *TM TRUCK 12", "amount": "-3.00", "merchant_name": "TM Taco Truck"}
    n = await client.post("/api/transactions", json=body)
    assert n.json()["merchant"] == "tm taco truck" and n.json()["merchant_source"] == "user"
