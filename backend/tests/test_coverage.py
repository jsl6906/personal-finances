"""Statement coverage checks: statement rows vs. the ledger for the statement period, with proposed fixes."""

import pytest

from ledger.jobs.worker import JobContext


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    import ledger.jobs.worker as worker
    from ledger.ai import imports as ai_imports

    async def no_progress(self, fraction, message=None):
        return None

    async def no_verdicts(pairs):
        return {}

    monkeypatch.setattr(worker.JobContext, "progress", no_progress)
    monkeypatch.setattr(ai_imports, "adjudicate_pairs", no_verdicts)


async def _run(job_type: str, payload: dict):
    import ledger.imports.jobs as jobs

    handler = {
        "prepare_import": jobs.prepare_import_job,
        "extract_document": jobs.extract_document_job,
        "statement_checks": jobs.statement_checks_job,
    }[job_type]
    return await handler(JobContext(0, job_type, payload))


async def _statement(client, monkeypatch, name: str, last4: str, period: tuple[str, str], closing: float, txns):
    from ledger.ai import imports as ai_imports

    async def fake_extract(data, mime_type, filename):
        return ai_imports.ExtractedStatement(
            document_type="bank_statement",
            institution="Coverage Bank",
            account_name="Everyday",
            account_last4=last4,
            period_start=period[0],
            period_end=period[1],
            opening_balance=0,
            closing_balance=closing,
            sign_note="as printed",
            summary="Coverage test statement",
            transactions=[
                ai_imports.ExtractedTxn(date=d, description=desc, amount=amt, confidence=0.99) for d, desc, amt in txns
            ],
        )

    monkeypatch.setattr(ai_imports, "extract_statement", fake_extract)
    b = (await client.post("/api/imports", files={"file": (name, f"%PDF-1.7 {name}".encode(), "application/pdf")})).json()
    await _run("extract_document", {"batch_id": b["id"]})
    d = (await client.get(f"/api/imports/{b['id']}")).json()
    await client.post(f"/api/imports/{b['id']}/prepare", json={"mapping": d["mapping"], "defaults": d["defaults"]})
    await _run("prepare_import", {"batch_id": b["id"]})
    return b["id"]


async def _txn(client, acct: int, day: str, desc: str, amount: str) -> int:
    r = await client.post(
        "/api/transactions", json={"txn_date": day, "description": desc, "amount": amount, "account_id": acct}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _by_kind(check: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i in check["detail"]["issues"]:
        out.setdefault(i["kind"], []).append(i)
    return out


async def test_review_proposes_fixes_that_trust_the_statement(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Card", "mask": "6602"})).json()["id"]
    coffee = await _txn(client, acct, "2021-04-05", "BLUE BOTTLE COFFEE", "-4.75")
    olive = await _txn(client, acct, "2021-04-10", "OLIVE GARDEN 123", "-45.00")
    phantom = await _txn(client, acct, "2021-04-15", "PHANTOM CHARGE XYZ", "-19.99")
    await _txn(client, acct, "2021-04-20", "PAYROLL ACME", "1000.00")
    edge = await _txn(client, acct, "2021-04-30", "EDGE THING", "-3.33")
    hardware = await _txn(client, acct, "2021-04-19", "HARDWARE DEPOT", "-61.17")

    rows = [
        ("2021-04-05", "BLUE BOTTLE COFFEE", -4.75),
        ("2021-04-05", "BLUE BOTTLE COFFEE", -4.75),
        ("2021-04-11", "OLIVE GARDEN 123", -54.00),
        ("2021-04-20", "PAYROLL ACME", 1000.00),
        ("2021-04-22", "NEW STORE", -12.34),
        ("2021-04-14", "HARDWARE DEPOT", -61.17),
    ]
    bid = await _statement(client, monkeypatch, "cov_apr.pdf", "6602", ("2021-04-01", "2021-04-30"), 862.99, rows)

    [c] = (await client.get(f"/api/imports/{bid}/checks")).json()
    assert c["account_id"] == acct and c["status"] == "mismatch" and c["trusted"] is True
    assert (c["statement_total"], c["ledger_total"], c["difference"]) == ("862.99", "738.25", "124.74")
    kinds = _by_kind(c)
    assert {k: len(v) for k, v in kinds.items()} == {"missing": 1, "link": 1, "amount": 1, "extra": 1, "edge": 1}
    missing = kinds["missing"][0]
    assert missing["row"]["row_index"] == 1 and "statement row 1" in missing["hint"]
    assert kinds["link"][0]["transaction_id"] == hardware
    assert kinds["amount"][0]["transaction_id"] == olive and kinds["amount"][0]["effect"] == "45.00"
    assert kinds["extra"][0]["transaction_id"] == phantom and kinds["extra"][0]["suggested"]
    assert kinds["edge"][0]["transaction_id"] == edge and not kinds["edge"][0]["suggested"]
    assert sum(float(i["effect"]) for i in c["detail"]["issues"]) == pytest.approx(124.74)

    fixes = [
        {"fix": i["fix"], "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
        for i in c["detail"]["issues"]
        if i["suggested"]
    ]
    stale = {"fix": "remove", "row_id": None, "transaction_id": coffee}
    r = (await client.post(f"/api/imports/{bid}/checks/fix", json={"fixes": [*fixes, stale]})).json()
    assert (r["applied"], r["skipped"]) == (4, 1)
    [c] = r["checks"]
    assert c["status"] == "explained" and c["difference"] == "3.33"

    assert (await client.get(f"/api/transactions/{phantom}")).status_code == 404
    olive_txn = (await client.get(f"/api/transactions/{olive}")).json()
    assert olive_txn["amount"] == "-54.00"
    notes = (await client.get(f"/api/transactions/{olive}/notes")).json()
    assert notes[0]["body"] == "Amount corrected from -45.00 to -54.00 per cov_apr.pdf"

    d = (await client.post(f"/api/imports/{bid}/commit", json={})).json()
    assert d["stats"]["inserted"] == 2  # the second coffee and the new store
    [c] = (await client.get(f"/api/imports/{bid}/checks")).json()
    assert c["status"] == "explained"
    r = (
        await client.post(
            f"/api/imports/{bid}/checks/fix",
            json={"fixes": [{"fix": "remove", "transaction_id": edge}]},
        )
    ).json()
    assert r["applied"] == 1 and r["checks"][0]["status"] == "ok" and r["checks"][0]["difference"] == "0.00"


async def test_historic_check_and_post_commit_add(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Checking", "mask": "6603"})).json()["id"]
    await _txn(client, acct, "2021-06-03", "CORNER DELI", "-8.80")
    rows = [
        ("2021-06-03", "CORNER DELI", -8.80),
        ("2021-06-03", "CORNER DELI", -8.80),
        ("2021-06-15", "RENT PAYMENT", -1500.00),
    ]
    bid = await _statement(client, monkeypatch, "cov_jun.pdf", "6603", ("2021-06-01", "2021-06-30"), 1517.60, rows)
    d = (await client.post(f"/api/imports/{bid}/commit", json={})).json()
    assert d["stats"]["inserted"] == 1

    result = await _run("statement_checks", {})
    assert result["statements"] >= 1 and result["mismatch"] >= 1
    listing = (await client.get("/api/statement-checks", params={"status": "mismatch"})).json()
    item = next(i for i in listing["items"] if i["import_batch_id"] == bid)
    assert item["issue_counts"] == {"missing": 1} and item["fixes"] == 1 and item["account_name"] == "Coverage Checking"
    assert listing["unchecked"] == 0

    [c] = (await client.get(f"/api/imports/{bid}/checks")).json()
    [issue] = c["detail"]["issues"]
    r = (
        await client.post(f"/api/imports/{bid}/checks/fix", json={"fixes": [{"fix": "add", "row_id": issue["row_id"]}]})
    ).json()
    assert r["applied"] == 1 and r["checks"][0]["status"] == "ok"
    page = (await client.get("/api/transactions", params={"import_batch_id": bid})).json()
    assert sorted(t["description"] for t in page["items"]) == ["CORNER DELI", "CORNER DELI", "RENT PAYMENT"]

    # Rolling the statement back removes its check from the list.
    await client.post(f"/api/imports/{bid}/rollback")
    listing = (await client.get("/api/statement-checks")).json()
    assert all(i["import_batch_id"] != bid for i in listing["items"])
