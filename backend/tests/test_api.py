import httpx
import pytest


async def test_requires_login(database):
    from ledger.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get("/api/transactions")).status_code == 401
        assert (await anon.post("/api/auth/login", json={"password": "wrong"})).status_code == 401
        assert (await anon.get("/api/auth/me")).json() == {"authenticated": False}


@pytest.fixture(scope="module")
async def refs(client):
    inst = (await client.post("/api/institutions", json={"name": "Chase"})).json()
    acct = (
        await client.post(
            "/api/accounts",
            json={"name": "Chase Sapphire", "institution_id": inst["id"], "account_type": "credit_card", "mask": "4471"},
        )
    ).json()
    cats = {c["name"]: c for c in (await client.get("/api/categories")).json()}
    tag = (await client.post("/api/tags", json={"name": "Water bills"})).json()
    return {"acct": acct, "cats": cats, "tag": tag}


async def test_seeded_categories(client):
    cats = (await client.get("/api/categories")).json()
    assert len(cats) >= 85  # other test modules add their own categories
    water = next(c for c in cats if c["name"] == "Water")
    assert water["group_name"] == "Utilities & Home Services"


async def test_account_includes_institution(refs):
    assert refs["acct"]["institution_name"] == "Chase"


async def test_duplicate_name_conflict(client, refs):
    r = await client.post("/api/accounts", json={"name": "Chase Sapphire"})
    assert r.status_code == 409


async def test_category_rename_keeps_alias(client, refs):
    group_id = refs["cats"]["Water"]["group_id"]
    cat = (await client.post("/api/categories", json={"name": "Renamable Api", "group_id": group_id})).json()
    body = {k: cat[k] for k in ("group_id", "type", "hide_from_reports", "is_active", "description")}
    r = await client.put(f"/api/categories/{cat['id']}", json=body | {"name": "Renamed Api"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed Api"
    aliases = {a["alias"]: a["category_id"] for a in (await client.get("/api/category-aliases")).json()}
    assert aliases["renamable api"] == cat["id"]
    # renaming again (or a no-op save) must not fail on the existing alias
    assert (await client.put(f"/api/categories/{cat['id']}", json=body | {"name": "Renamed Api"})).status_code == 200


async def test_transaction_crud_and_rule_learning(client, refs):
    acct_id = refs["acct"]["id"]
    groceries = refs["cats"]["Groceries"]["id"]
    r = await client.post(
        "/api/transactions",
        json={
            "txn_date": "2026-09-26",
            "description": "KROGER #412 SPRINGFIELD IL",
            "amount": "-142.18",
            "account_id": acct_id,
            "tag_ids": [refs["tag"]["id"]],
        },
    )
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["merchant"] == "kroger springfield"
    assert t["category_id"] is None
    assert t["tags"][0]["name"] == "Water bills"
    assert t["institution_name"] == "Chase"

    r = await client.patch(f"/api/transactions/{t['id']}", json={"category_id": groceries, "notes": "weekly"})
    assert r.status_code == 200, r.text
    assert r.json()["category_name"] == "Groceries"
    assert r.json()["category_source"] == "user"
    # Editing a category no longer learns a rule silently.
    assert (await client.get(f"/api/rules/for-transaction/{t['id']}")).json() == []
    rule = await client.post(
        "/api/rules", json={"match_type": "merchant", "pattern": "Kroger Springfield", "category_id": groceries}
    )
    assert rule.status_code == 201, rule.text
    rule = rule.json()
    assert rule["pattern"] == "kroger springfield" and rule["source"] == "user"

    # A new transaction from the same merchant (different store #) picks up the rule
    r = await client.post(
        "/api/transactions",
        json={
            "txn_date": "2026-09-30",
            "description": "KROGER #999 SPRINGFIELD IL",
            "amount": "-20.00",
            "account_id": acct_id,
        },
    )
    t2 = r.json()
    assert t2["category_id"] == groceries
    assert t2["category_source"] == "rule"
    assert t2["category_rule_id"] == rule["id"] and "kroger springfield" in t2["category_rule"]

    page = (await client.get("/api/transactions", params={"q": "kroger", "start": "2026-09-01"})).json()
    assert page["total"] == 2
    assert page["total_out"] == "-162.18"

    page = (await client.get("/api/transactions", params={"q": "142.18"})).json()
    assert page["total"] == 1

    assert (await client.delete(f"/api/transactions/{t2['id']}")).status_code == 204
    assert (await client.get(f"/api/transactions/{t2['id']}")).status_code == 404


async def test_bulk_update_and_tags(client, refs):
    acct_id = refs["acct"]["id"]
    ids = []
    for i in range(3):
        r = await client.post(
            "/api/transactions",
            json={
                "txn_date": f"2026-08-0{i + 1}",
                "description": f"CITY WATER UTIL {i}",
                "amount": "-80.00",
                "account_id": acct_id,
            },
        )
        ids.append(r.json()["id"])
    water = refs["cats"]["Water"]["id"]
    r = await client.post(
        "/api/transactions/bulk", json={"ids": ids, "category_id": water, "add_tag_ids": [refs["tag"]["id"]]}
    )
    assert r.json()["updated"] == 3
    page = (await client.get("/api/transactions", params={"tag_id": refs["tag"]["id"], "category_id": water})).json()
    assert page["total"] == 3


async def test_transaction_sorting(client, refs):
    acct = (await client.post("/api/accounts", json={"name": "Sort Check", "mask": "7101"})).json()["id"]
    water = refs["cats"]["Water"]["id"]
    rows = [
        ("2026-07-03", "ZULU SORTER", "-5.00", None),
        ("2026-07-01", "alpha sorter", "-50.00", water),
        ("2026-07-02", "Mike Sorter", "20.00", None),
    ]
    for day, desc, amount, cat in rows:
        body = {"txn_date": day, "description": desc, "amount": amount, "account_id": acct, "category_id": cat}
        assert (await client.post("/api/transactions", json=body)).status_code == 201

    async def order(sort: str) -> list[str]:
        page = (await client.get("/api/transactions", params={"account_id": acct, "sort": sort})).json()
        return [t["description"] for t in page["items"]]

    assert await order("date_desc") == ["ZULU SORTER", "Mike Sorter", "alpha sorter"]
    assert await order("description_asc") == ["alpha sorter", "Mike Sorter", "ZULU SORTER"]
    assert await order("amount_asc") == ["alpha sorter", "ZULU SORTER", "Mike Sorter"]
    # Uncategorized rows go last either way; ties fall back to newest first.
    assert await order("category_desc") == ["alpha sorter", "ZULU SORTER", "Mike Sorter"]
    assert await order("account_asc") == ["ZULU SORTER", "Mike Sorter", "alpha sorter"]
    assert (await client.get("/api/transactions", params={"sort": "merchant_up"})).status_code == 422


async def test_categorize_job_with_mocked_ai(client, refs, monkeypatch):
    from ledger.ai import categorize
    from ledger.jobs.worker import JobContext

    acct_id = refs["acct"]["id"]
    r = await client.post(
        "/api/transactions",
        json={
            "txn_date": "2026-09-10",
            "description": "SPECTRUM INTERNET 855-707",
            "amount": "-79.99",
            "account_id": acct_id,
        },
    )
    txn_id = r.json()["id"]
    internet = refs["cats"]["Cable/Internet"]["id"]

    async def fake_generate(prompt, **kwargs):
        assert "SPECTRUM INTERNET" in prompt
        return categorize.Suggestions(
            items=[
                categorize.Suggestion(id=txn_id, category_id=internet, confidence=0.93, reason="ISP bill"),
                categorize.Suggestion(id=999999, category_id=internet, confidence=0.9, reason="ignored"),
            ]
        )

    monkeypatch.setattr(categorize, "generate", fake_generate)
    job = (await client.post("/api/transactions/suggestions/run", json={"ids": [txn_id]})).json()
    assert job["status"] == "queued"
    result = await categorize.categorize_job(JobContext(job["id"], "categorize", {"ids": [txn_id]}))
    assert result["suggested"] == 1

    t = (await client.get(f"/api/transactions/{txn_id}")).json()
    assert t["suggested_category_name"] == "Cable/Internet"
    assert t["category_id"] is None

    page = (await client.get("/api/transactions", params={"status": "suggested"})).json()
    assert txn_id in [i["id"] for i in page["items"]]

    assert (await client.post("/api/transactions/suggestions/accept", json={"ids": [txn_id]})).json() == {"accepted": 1}
    t = (await client.get(f"/api/transactions/{txn_id}")).json()
    assert t["category_name"] == "Cable/Internet"
    assert t["category_source"] == "ai"


async def test_summary(client):
    s = (await client.get("/api/summary", params={"start": "2026-09-01", "end": "2026-09-30"})).json()
    assert s["count"] >= 2
    assert float(s["spent"]) > 0
