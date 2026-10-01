from ledger.jobs.worker import JobContext


async def _no_progress(self, fraction, message=None):
    return None


async def test_bill_upload_suggest_and_approve(client, monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import statements as ai_st
    from ledger.statements.jobs import extract_bill_job

    monkeypatch.setattr(worker.JobContext, "progress", _no_progress)
    acct = (await client.post("/api/accounts", json={"name": "Bills Checking"})).json()
    pay = (
        await client.post(
            "/api/transactions",
            json={
                "txn_date": "2026-09-25",
                "description": "CITY OF SPRINGFIELD WATER AUTOPAY",
                "amount": "-84.12",
                "account_id": acct["id"],
            },
        )
    ).json()
    decoy = (
        await client.post(
            "/api/transactions",
            json={"txn_date": "2026-09-20", "description": "SHELL OIL", "amount": "-84.12", "account_id": acct["id"]},
        )
    ).json()

    async def fake_extract(data, mime_type, filename):
        return ai_st.BillExtraction(
            document_type="utility_bill",
            vendor="City of Springfield Water",
            service_type="Water",
            account_last4="0042",
            statement_date="2026-09-15",
            period_start="2026-08-14",
            period_end="2026-09-13",
            due_date="2026-09-30",
            amount_due=84.12,
            usage=[
                ai_st.UsageMetric(metric="water_usage", value=5380, unit="gal", is_primary=True),
                ai_st.UsageMetric(metric="sewer_usage", value=5000, unit="gal", is_primary=False),
            ],
            summary="City water bill Aug-Sep 2026",
        )

    monkeypatch.setattr(ai_st, "extract_bill", fake_extract)

    r = await client.post("/api/statements", files={"file": ("CityWater_2026-09.pdf", b"%PDF-1.7 water", "application/pdf")})
    assert r.status_code == 201, r.text
    st = r.json()
    assert st["status"] == "processing"
    await extract_bill_job(JobContext(0, "extract_bill", {"statement_id": st["id"]}))

    st = (await client.get(f"/api/statements/{st['id']}")).json()
    assert st["status"] == "suggested"
    sug = st["suggestion"]
    assert sug["series_id"] is None and sug["new_series_name"] == "Water"
    assert sug["new_series_category_id"] is not None  # seeded "Water" category
    assert sug["candidates"][0]["transaction_id"] == pay["id"]
    assert sug["transaction_ids"] == [pay["id"]]
    assert decoy["id"] in [c["transaction_id"] for c in sug["candidates"]]

    r = await client.post(f"/api/statements/{st['id']}/approve", json={"transaction_ids": sug["transaction_ids"]})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["status"] == "approved" and st["series_name"] == "Water"
    assert [t["id"] for t in st["transactions"]] == [pay["id"]]

    t = (await client.get(f"/api/transactions/{pay['id']}")).json()
    assert t["has_statement"] is True
    assert "Water" in [tg["name"] for tg in t["tags"]]
    assert t["category_name"] == "Water"

    series = next(s for s in (await client.get("/api/statement-series")).json() if s["name"] == "Water")
    assert series["statement_count"] == 1 and series["primary_metric"] == "water_usage" and series["unit"] == "gal"
    hist = (await client.get(f"/api/statement-series/{series['id']}/history")).json()
    assert hist[0]["usage_value"] == "5380.000" and hist[0]["cost_per_unit"] == "0.0156"

    page = (await client.get("/api/transactions", params={"status": "with_statement"})).json()
    with_statement = [i["id"] for i in page["items"]]
    assert pay["id"] in with_statement and decoy["id"] not in with_statement
    linked = (await client.get(f"/api/statements/for-transaction/{pay['id']}")).json()
    assert linked[0]["id"] == st["id"]

    # Next month's bill is matched to the existing series
    async def fake_extract2(data, mime_type, filename):
        return ai_st.BillExtraction(
            document_type="utility_bill",
            vendor="City of Springfield Water",
            service_type="Water",
            statement_date="2026-10-15",
            due_date="2026-10-30",
            amount_due=81.00,
            usage=[],
            summary="Oct bill",
        )

    monkeypatch.setattr(ai_st, "extract_bill", fake_extract2)
    r = await client.post("/api/statements", files={"file": ("CityWater_2026-10.pdf", b"%PDF-1.7 oct", "application/pdf")})
    await extract_bill_job(JobContext(0, "extract_bill", {"statement_id": r.json()["id"]}))
    st2 = (await client.get(f"/api/statements/{r.json()['id']}")).json()
    assert st2["suggestion"]["series_id"] == series["id"]
    st2 = (await client.post(f"/api/statements/{st2['id']}/approve", json={"transaction_ids": []})).json()
    assert st2["series_id"] == series["id"]


async def test_statement_rejects_spreadsheets(client):
    r = await client.post("/api/statements", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 422
