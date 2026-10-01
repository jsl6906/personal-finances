import pytest

from ledger.jobs.worker import JobContext

CSV = (
    "Transaction Date,Post Date,Description,Category,Amount\n"
    "09/26/2026,09/27/2026,KROGER #412 SPRINGFIELD IL,Groceries,-142.18\n"
    "09/18/2026,09/19/2026,AMEREN ILLINOIS,Bills & Utilities,-163.90\n"
    "09/12/2026,09/12/2026,NETFLIX.COM,Entertainment,-15.49\n"
    "not a date,,BROKEN ROW,,-1.00\n"
)


@pytest.fixture(scope="module")
async def setup(client):
    acct = (
        await client.post("/api/accounts", json={"name": "Import Card", "account_type": "credit_card", "mask": "9911"})
    ).json()
    other = (await client.post("/api/accounts", json={"name": "Import Checking"})).json()
    # Existing ledger rows: exact twin of Kroger, and Ameren captured from another feed/account.
    for body in (
        {
            "txn_date": "2026-09-26",
            "description": "KROGER #412 SPRINGFIELD IL",
            "amount": "-142.18",
            "account_id": acct["id"],
        },
        {"txn_date": "2026-09-18", "description": "Ameren Electric", "amount": "-163.90", "account_id": other["id"]},
    ):
        assert (await client.post("/api/transactions", json=body)).status_code == 201
    return {"acct": acct, "other": other}


async def _run(job_type: str, payload: dict):
    import ledger.imports.jobs as jobs

    handler = {
        "prepare_import": jobs.prepare_import_job,
        "extract_document": jobs.extract_document_job,
        "dup_scan": jobs.dup_scan_job,
    }[job_type]
    return await handler(JobContext(0, job_type, payload))


async def test_spreadsheet_import_flow(client, setup, monkeypatch):
    import ledger.jobs.worker as worker

    async def no_progress(self, fraction, message=None):
        return None

    monkeypatch.setattr(worker.JobContext, "progress", no_progress)
    from ledger.ai import imports as ai_imports

    async def fake_adjudicate(pairs):
        return {
            p["id"]: ai_imports.PairVerdict(pair_id=p["id"], probability_same=0.9, reason="Same bill from two feeds")
            for p in pairs
        }

    monkeypatch.setattr(ai_imports, "adjudicate_pairs", fake_adjudicate)
    cats = {c["name"]: c["id"] for c in (await client.get("/api/categories")).json()}

    async def fake_labels(labels, taxonomy):
        assert labels == ["Bills & Utilities", "Entertainment"]
        return [
            ai_imports.LabelMatch(
                label="Entertainment", category_id=cats["Books, Amusement, & Entertainment"], confidence=0.9
            ),
            ai_imports.LabelMatch(label="Bills & Utilities", category_id=cats["Electric"], confidence=0.4),
        ]

    monkeypatch.setattr(ai_imports, "map_category_labels", fake_labels)

    r = await client.post("/api/imports", files={"file": ("chase_sep.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["status"] == "mapping" and b["row_count"] == 4
    assert b["mapping"]["Transaction Date"] == "txn_date" and b["mapping_source"] == "heuristic"

    body = {
        "mapping": b["mapping"],
        "defaults": {"account_id": setup["acct"]["id"], "notes": "Imported"},
        "save_template": True,
        "template_name": "Chase card",
    }
    r = await client.post(f"/api/imports/{b['id']}/prepare", json=body)
    assert r.status_code == 200 and r.json()["status"] == "preparing"
    stats = await _run("prepare_import", {"batch_id": b["id"]})
    assert stats["exact_duplicates"] == 1
    assert stats["possible_duplicates"] == 1
    assert stats["invalid"] == 1
    assert stats["category_aliases_learned"] == 1

    d = (await client.get(f"/api/imports/{b['id']}")).json()
    assert d["status"] == "review"
    assert d["decisions"] == {"skip_duplicate": 1, "pending": 1, "insert": 1, "invalid": 1}

    pairs = (await client.get(f"/api/imports/{b['id']}/duplicates")).json()
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["existing"]["description"] == "Ameren Electric"
    assert pair["ai_reason"] == "Same bill from two feeds"
    assert "Different accounts" in pair["reasons"]

    invalid = (await client.get(f"/api/imports/{b['id']}/rows", params={"decision": "invalid"})).json()
    assert invalid[0]["errors"] == ["Unreadable date 'not a date'"]

    # Keep the Ameren row as a separate transaction
    r = await client.post(f"/api/imports/{b['id']}/rows/{pair['row']['id']}/decision", json={"decision": "keep"})
    assert r.json()["decision"] == "keep"

    d = (await client.post(f"/api/imports/{b['id']}/commit", json={})).json()
    assert d["status"] == "committed"
    assert d["stats"]["inserted"] == 2 and d["stats"]["skipped_duplicates"] == 1 and d["stats"]["kept_separate"] == 1

    page = (await client.get("/api/transactions", params={"import_batch_id": b["id"]})).json()
    assert page["total"] == 2
    assert all(t["account_name"] == "Import Card" and t["notes"] == "Imported" for t in page["items"])
    netflix = next(t for t in page["items"] if t["description"] == "NETFLIX.COM")
    assert netflix["category_name"] == "Books, Amusement, & Entertainment"

    # The kept pair is remembered and not re-flagged by a scan
    await _run("dup_scan", {})
    pending = (await client.get("/api/duplicates")).json()
    assert not any({p["a"]["description"], p["b"]["description"]} == {"Ameren Electric", "AMEREN ILLINOIS"} for p in pending)
    kept = (await client.get("/api/duplicates", params={"status": "confirmed_separate"})).json()
    assert len(kept) == 1

    # Re-importing the same file: template applies, everything already exists
    r = await client.post("/api/imports", files={"file": ("chase_sep_again.csv", CSV.encode(), "text/csv")})
    b2 = r.json()
    assert b2["mapping_source"] == "template" and b2["template_name"] == "Chase card"
    await client.post(f"/api/imports/{b2['id']}/prepare", json={"mapping": b2["mapping"], "defaults": b2["defaults"]})
    stats = await _run("prepare_import", {"batch_id": b2["id"]})
    assert stats["exact_duplicates"] == 3

    # Rollback removes the first batch's transactions
    d = (await client.post(f"/api/imports/{b['id']}/rollback")).json()
    assert d["status"] == "rolled_back"
    page = (await client.get("/api/transactions", params={"import_batch_id": b["id"]})).json()
    assert page["total"] == 0


async def test_document_import_with_mocked_extraction(client, setup, monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import imports as ai_imports

    async def no_progress(self, fraction, message=None):
        return None

    monkeypatch.setattr(worker.JobContext, "progress", no_progress)

    async def fake_extract(data, mime_type, filename):
        assert mime_type == "application/pdf" and data.startswith(b"%PDF")
        return ai_imports.ExtractedStatement(
            document_type="credit_card_statement",
            institution="JPMorgan Chase",
            account_name="Sapphire Preferred",
            account_last4="9911",
            account_type="credit_card",
            period_start="2026-08-20",
            period_end="2026-09-19",
            opening_balance=100.0,
            closing_balance=333.33,
            sign_note="Charges shown positive; flipped",
            summary="Chase card statement",
            transactions=[
                ai_imports.ExtractedTxn(date="2026-08-22", description="SHELL OIL 5744", amount=-45.12, confidence=0.97),
                ai_imports.ExtractedTxn(date="2026-09-02", description="TARGET T-1234", amount=-188.21, confidence=0.62),
            ],
        )

    monkeypatch.setattr(ai_imports, "extract_statement", fake_extract)
    r = await client.post("/api/imports", files={"file": ("stmt.pdf", b"%PDF-1.7 fake", "application/pdf")})
    b = r.json()
    assert b["status"] == "extracting" and b["job_id"]
    await _run("extract_document", {"batch_id": b["id"]})
    d = (await client.get(f"/api/imports/{b['id']}")).json()
    assert d["status"] == "mapping" and d["row_count"] == 2
    assert d["defaults"]["account_id"] == setup["acct"]["id"]  # matched by last4
    assert d["doc_meta"]["low_confidence_rows"] == 1
    assert d["doc_meta"]["reconciliation"]["reconciles"] is True

    await client.post(f"/api/imports/{b['id']}/prepare", json={"mapping": d["mapping"], "defaults": d["defaults"]})
    await _run("prepare_import", {"batch_id": b["id"]})
    rows = (await client.get(f"/api/imports/{b['id']}/rows")).json()
    assert [r["confidence"] for r in rows] == ["0.970", "0.620"]
    d = (await client.post(f"/api/imports/{b['id']}/commit", json={})).json()
    assert d["stats"]["inserted"] == 2
    page = (await client.get("/api/transactions", params={"import_batch_id": b["id"]})).json()
    assert {t["source_type"] for t in page["items"]} == {"document"}

    att = await client.get(f"/api/attachments/{d['attachment_id']}/content")
    assert att.status_code == 200 and att.content.startswith(b"%PDF")


async def test_scan_and_decide_duplicate(client, setup, monkeypatch):
    import ledger.jobs.worker as worker

    async def no_progress(self, fraction, message=None):
        return None

    monkeypatch.setattr(worker.JobContext, "progress", no_progress)
    acct = setup["other"]["id"]
    ids = []
    for desc, day in (("TRADER JOE'S #702", "08"), ("Trader Joe's", "09")):
        r = await client.post(
            "/api/transactions",
            json={"txn_date": f"2026-07-{day}", "description": desc, "amount": "-88.34", "account_id": acct, "notes": desc},
        )
        ids.append(r.json()["id"])
    await _run("dup_scan", {"since": "2026-07-01"})
    pairs = [p for p in (await client.get("/api/duplicates")).json() if {p["a"]["id"], p["b"]["id"]} == set(ids)]
    assert len(pairs) == 1
    p = pairs[0]
    r = await client.post(f"/api/duplicates/{p['id']}/decide", json={"decision": "duplicate", "keep_id": ids[0]})
    assert r.json()["status"] == "confirmed_duplicate"
    assert (await client.get(f"/api/transactions/{ids[1]}")).status_code == 404
    kept = (await client.get(f"/api/transactions/{ids[0]}")).json()
    assert "Trader Joe's" in kept["notes"]


async def test_duplicate_rows_link_document_and_notes(client, setup, monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import imports as ai_imports

    async def no_progress(self, fraction, message=None):
        return None

    monkeypatch.setattr(worker.JobContext, "progress", no_progress)
    acct = setup["acct"]["id"]
    existing = (
        await client.post(
            "/api/transactions",
            json={"txn_date": "2026-06-03", "description": "COSTCO WHSE #0412", "amount": "-61.07", "account_id": acct},
        )
    ).json()

    async def fake_extract(data, mime_type, filename):
        return ai_imports.ExtractedStatement(
            document_type="credit_card_statement",
            account_last4="9911",
            sign_note="as printed",
            summary="June statement",
            transactions=[
                ai_imports.ExtractedTxn(
                    date="2026-06-03", description="COSTCO WHSE #0412", details="Member 1234; 2% reward",
                    amount=-61.07, confidence=0.99,
                ),
                ai_imports.ExtractedTxn(
                    date="2026-06-05", description="ACME HARDWARE", details="Ref 998877", amount=-12.34, confidence=0.99
                ),
            ],
        )

    monkeypatch.setattr(ai_imports, "extract_statement", fake_extract)
    b = (await client.post("/api/imports", files={"file": ("june.pdf", b"%PDF-1.7 june", "application/pdf")})).json()
    await _run("extract_document", {"batch_id": b["id"]})
    d = (await client.get(f"/api/imports/{b['id']}")).json()
    assert d["mapping"]["Details"] == "notes"
    await client.post(f"/api/imports/{b['id']}/prepare", json={"mapping": d["mapping"], "defaults": d["defaults"]})
    stats = await _run("prepare_import", {"batch_id": b["id"]})
    assert stats["exact_duplicates"] == 1
    d = (await client.post(f"/api/imports/{b['id']}/commit", json={})).json()
    assert d["stats"]["inserted"] == 1 and d["stats"]["linked_to_existing"] == 1

    # The duplicate row is linked to the existing transaction, and its statement details become a note.
    sources = (await client.get(f"/api/transactions/{existing['id']}/sources")).json()
    assert [(s["role"], s["filename"], s["origin"]) for s in sources] == [("matched", "june.pdf", "upload")]
    assert sources[0]["match_score"] == "1.000"
    notes = (await client.get(f"/api/transactions/{existing['id']}/notes")).json()
    assert [(n["body"], n["source"], n["filename"]) for n in notes] == [("Member 1234; 2% reward", "import", "june.pdf")]

    acme = (await client.get("/api/transactions", params={"import_batch_id": b["id"]})).json()["items"][0]
    assert acme["notes"] == "Ref 998877"
    assert [s["role"] for s in (await client.get(f"/api/transactions/{acme['id']}/sources")).json()] == ["created"]

    # Hand-written notes stack up alongside imported ones and are searchable.
    r = await client.post(f"/api/transactions/{existing['id']}/notes", json={"body": "  Bulk paper towels  "})
    assert r.status_code == 201 and r.json()["body"] == "Bulk paper towels" and r.json()["source"] == "user"
    note_id = r.json()["id"]
    r = await client.patch(f"/api/transactions/{existing['id']}/notes/{note_id}", json={"body": "Bulk towels + TP"})
    assert r.json()["body"] == "Bulk towels + TP"
    assert len((await client.get(f"/api/transactions/{existing['id']}/notes")).json()) == 2
    found = (await client.get("/api/transactions", params={"q": "towels + TP"})).json()["items"]
    assert [t["id"] for t in found] == [existing["id"]]
    assert (await client.delete(f"/api/transactions/{acme['id']}/notes/{note_id}")).status_code == 404
    assert (await client.delete(f"/api/transactions/{existing['id']}/notes/{note_id}")).status_code == 204

    # Re-importing the same statement adds no second copy of the note.
    b2 = (await client.post("/api/imports", files={"file": ("june2.pdf", b"%PDF-1.7 june again", "application/pdf")})).json()
    await _run("extract_document", {"batch_id": b2["id"]})
    d2 = (await client.get(f"/api/imports/{b2['id']}")).json()
    await client.post(f"/api/imports/{b2['id']}/prepare", json={"mapping": d2["mapping"], "defaults": d2["defaults"]})
    await _run("prepare_import", {"batch_id": b2["id"]})
    await client.post(f"/api/imports/{b2['id']}/commit", json={})
    sources = (await client.get(f"/api/transactions/{existing['id']}/sources")).json()
    assert [s["filename"] for s in sources] == ["june.pdf", "june2.pdf"]
    assert len((await client.get(f"/api/transactions/{existing['id']}/notes")).json()) == 1

    # Rolling back an import drops what it attached to existing transactions.
    await client.post(f"/api/imports/{b['id']}/rollback")
    sources = (await client.get(f"/api/transactions/{existing['id']}/sources")).json()
    assert [s["filename"] for s in sources] == ["june2.pdf"]
    assert (await client.get(f"/api/transactions/{existing['id']}/notes")).json() == []
