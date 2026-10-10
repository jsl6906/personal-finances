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


def test_balance_fixes_correct_a_misread_amount():
    from decimal import Decimal as D

    from ledger.imports.service import balance_fixes

    amounts = [D("1000"), D("1000"), D("1000"), D("800"), D("-2000"), D("172"), D("6.96")]
    balances = [D(b) for b in ("8400.34", "8500.34", "9500.34", "10300.34", "8300.34", "8472.34", "8479.30")]
    assert balance_fixes(amounts, balances, D("7400.34"), D("8479.30")) == {1: D("100.00")}
    # Newest-first listing.
    assert balance_fixes(amounts[::-1], balances[::-1], D("7400.34"), D("8479.30")) == {5: D("100.00")}
    # Credit card: balance owed rises with purchases (negative amounts).
    assert balance_fixes([D("-50"), D("-200"), D("-30")], [D("150"), D("170"), D("200")], D("100"), D("200")) == {
        1: D("-20")
    }
    # Already reconciles, or the row's own balance isn't confirmed by the next row: nothing to fix.
    assert balance_fixes([D("50"), D("20")], [D("150"), D("170")], D("100"), D("170")) == {}
    assert balance_fixes([D("50"), D("200"), D("30")], [D("150"), D("999"), D("200")], D("100"), D("200")) == {}


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


async def test_confident_fixes_apply_at_commit_and_in_bulk(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Checking", "mask": "6603"})).json()["id"]
    await _txn(client, acct, "2021-06-03", "CORNER DELI", "-8.80")
    rows = [
        ("2021-06-03", "CORNER DELI", -8.80),
        ("2021-06-03", "CORNER DELI", -8.80),
        ("2021-06-15", "RENT PAYMENT", -1500.00),
    ]
    bid = await _statement(client, monkeypatch, "cov_jun.pdf", "6603", ("2021-06-01", "2021-06-30"), 1517.60, rows)
    # The ledger had one deli charge for two statement rows; the missing one is added at commit without review.
    d = (await client.post(f"/api/imports/{bid}/commit", json={})).json()
    assert d["stats"]["inserted"] == 1 and d["stats"]["auto_fixed"] == 1
    page = (await client.get("/api/transactions", params={"import_batch_id": bid})).json()
    assert sorted(t["description"] for t in page["items"]) == ["CORNER DELI", "CORNER DELI", "RENT PAYMENT"]

    # A feed later records the rent twice; the copy is a confident removal.
    copy = await _txn(client, acct, "2021-06-15", "RENT PAYMENT", "-1500.00")
    result = await _run("statement_checks", {})
    assert result["statements"] >= 1 and result["mismatch"] >= 1 and "fixed" not in result
    listing = (await client.get("/api/statement-checks", params={"status": "mismatch"})).json()
    item = next(i for i in listing["items"] if i["import_batch_id"] == bid)
    assert item["issue_counts"] == {"extra": 1} and item["confident"] == 1 and item["account_name"] == "Coverage Checking"
    assert listing["unchecked"] == 0 and listing["confident"] >= 1

    result = await _run("statement_checks", {"apply": True})
    assert result["fixed"] >= 1
    [c] = (await client.get(f"/api/imports/{bid}/checks")).json()
    assert c["status"] == "ok"
    assert (await client.get(f"/api/transactions/{copy}")).status_code == 404

    # Rolling the statement back removes its check from the list.
    await client.post(f"/api/imports/{bid}/rollback")
    listing = (await client.get("/api/statement-checks")).json()
    assert all(i["import_batch_id"] != bid for i in listing["items"])


async def test_duplicate_at_period_edge_is_not_explained_away(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Savings", "mask": "6604"})).json()["id"]
    first = await _txn(client, acct, "2021-08-31", "INTEREST PAID", "2.95")
    second = await _txn(client, acct, "2021-08-31", "INTEREST PAID", "2.95")
    lone = await _txn(client, acct, "2021-08-01", "EDGE ONLY", "-1.11")
    rows = [("2021-08-31", "INTEREST PAID", 2.95)]
    bid = await _statement(client, monkeypatch, "cov_aug.pdf", "6604", ("2021-08-01", "2021-08-31"), 2.95, rows)

    [c] = (await client.get(f"/api/imports/{bid}/checks")).json()
    kinds = {i["kind"]: i for i in c["detail"]["issues"]}
    assert set(kinds) == {"extra", "edge"} and c["status"] == "mismatch"
    assert kinds["extra"]["transaction_id"] in (first, second) and kinds["extra"]["suggested"]
    assert kinds["edge"]["transaction_id"] == lone and not kinds["edge"]["suggested"]


async def test_edge_transaction_on_another_statement_takes_its_date(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Rewards", "mask": "6605"})).json()["id"]
    late = await _txn(client, acct, "2021-10-01", "LATE CAFE", "-7.77")
    sep_rows = [
        ("2021-09-29", "LATE CAFE", -7.77),
        ("2021-09-30", "MIDNIGHT DINER", -8.88),
        ("2021-09-15", "SEP GROCER", -20.00),
    ]
    sep = await _statement(client, monkeypatch, "cov_sep.pdf", "6605", ("2021-09-01", "2021-09-30"), -36.65, sep_rows)
    await client.post(f"/api/imports/{sep}/commit", json={})
    assert any(s["import_batch_id"] == sep for s in (await client.get(f"/api/transactions/{late}/sources")).json())
    # The diner row's transaction is lost; a feed later records the purchase two days on.
    page = (await client.get("/api/transactions", params={"import_batch_id": sep, "q": "MIDNIGHT DINER"})).json()
    await client.delete(f"/api/transactions/{page['items'][0]['id']}")
    diner = await _txn(client, acct, "2021-10-02", "MIDNIGHT DINER", "-8.88")

    oct_rows = [("2021-10-15", "OCT RENT CO", -900.00)]
    bid = await _statement(client, monkeypatch, "cov_oct.pdf", "6605", ("2021-10-01", "2021-10-31"), -900.00, oct_rows)
    [c] = (await client.get(f"/api/imports/{bid}/checks")).json()
    assert c["status"] == "explained" and c["difference"] == "8.88"
    fixes = {i["transaction_id"]: i for i in c["detail"]["issues"]}
    assert set(fixes) == {late, diner}
    assert all(i["kind"] == "listed" and i["fix"] == "date" and i["suggested"] for i in fixes.values())
    assert fixes[late]["row"]["date"] == "2021-09-29" and fixes[late]["row"]["statement"] == "cov_sep.pdf"
    # The late cafe is already left out of the total (cov_sep.pdf lists it); the diner moves out when redated.
    assert fixes[late]["effect"] == "0.00" and c["detail"]["listed_elsewhere"] == {"count": 1, "total": "-7.77"}
    assert fixes[diner]["row"]["date"] == "2021-09-30" and fixes[diner]["effect"] == "8.88"

    body = {"fixes": [{"fix": "date", "row_id": i["row_id"], "transaction_id": t} for t, i in fixes.items()]}
    r = (await client.post(f"/api/imports/{bid}/checks/fix", json=body)).json()
    assert r["applied"] == 2 and r["checks"][0]["status"] == "ok"
    assert (await client.get(f"/api/transactions/{late}")).json()["txn_date"] == "2021-09-29"
    notes = (await client.get(f"/api/transactions/{late}/notes")).json()
    assert notes[0]["body"] == "Date corrected from 2021-10-01 to 2021-09-29 per cov_sep.pdf"
    assert (await client.get(f"/api/transactions/{diner}")).json()["txn_date"] == "2021-09-30"
    [sep_check] = (await client.get(f"/api/imports/{sep}/checks")).json()
    assert sep_check["status"] == "ok"


async def test_feed_holds_reversals_and_pending_amounts_are_explained(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Holds", "mask": "6610"})).json()["id"]
    await _txn(client, acct, "2022-03-15", "HOLDS GROCER", "-40.00")
    await _txn(client, acct, "2022-04-12", "TIPPY CAFE", "-23.00")
    hold = await _txn(client, acct, "2022-03-30", "HOLDS MARKETPLACE", "-9.99")
    release = await _txn(client, acct, "2022-04-01", "HOLDS MARKETPLACE REFUND", "9.99")
    tip = await _txn(client, acct, "2022-04-10", "TIPPY CAFE", "-20.00")
    mar = await _statement(
        client,
        monkeypatch,
        "cov_holds_mar.pdf",
        "6610",
        ("2022-03-01", "2022-03-31"),
        -40.00,
        [("2022-03-15", "HOLDS GROCER", -40.00)],
    )
    await client.post(f"/api/imports/{mar}/commit", json={})
    apr = await _statement(
        client,
        monkeypatch,
        "cov_holds_apr.pdf",
        "6610",
        ("2022-04-01", "2022-04-30"),
        -23.00,
        [("2022-04-12", "TIPPY CAFE", -23.00)],
    )
    [c] = (await client.get(f"/api/imports/{apr}/checks")).json()
    issues = {i["transaction_id"]: i for i in c["detail"]["issues"]}
    # The March statement is imported, so a release on 1 April isn't waiting to appear there.
    assert issues[release]["kind"] == "extra" and issues[release]["suggested"]
    assert issues[release]["hint"].startswith("Cancels out -9.99")
    assert issues[tip]["kind"] == "extra"
    assert issues[tip]["hint"].startswith("Probably the pending amount of TIPPY CAFE")
    # Together the two removals close the difference exactly, which lifts both (85 -> 95) past the auto threshold.
    assert c["detail"]["fixes_reconcile"] and issues[release]["confidence"] == issues[tip]["confidence"] == 95
    d = (await client.post(f"/api/imports/{apr}/commit", json={})).json()
    assert d["stats"]["auto_fixed"] == 2
    assert (await client.get(f"/api/transactions/{tip}")).status_code == 404
    [c] = (await client.get(f"/api/imports/{apr}/checks")).json()
    assert c["status"] == "ok"
    [c] = (await client.get(f"/api/imports/{mar}/checks")).json()
    assert [(i["transaction_id"], i["kind"]) for i in c["detail"]["issues"]] == [(hold, "extra")]


async def test_matched_rows_dated_before_the_period_count_toward_the_ledger(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Boundary", "mask": "6609"})).json()["id"]
    await _txn(client, acct, "2022-02-28", "BOUNDARY SHOP", "-26.92")
    await _txn(client, acct, "2022-03-12", "BOUNDARY CAFE", "-3.50")
    rows = [("2022-02-28", "BOUNDARY SHOP", -26.92), ("2022-03-12", "BOUNDARY CAFE", -3.50)]
    bid = await _statement(client, monkeypatch, "cov_mar.pdf", "6609", ("2022-03-01", "2022-03-31"), -30.42, rows)
    await client.post(f"/api/imports/{bid}/commit", json={})
    [c] = (await client.get(f"/api/imports/{bid}/checks")).json()
    assert c["status"] == "ok" and c["ledger_total"] == "-30.42" and c["ledger_rows"] == 2
    assert len(c["detail"]["shifted"]) == 1

    # February's own check: the shop falls in its dates but March lists it with the same date, so nothing to explain.
    rows = [("2022-02-14", "BOUNDARY FLORIST", -40.00)]
    feb = await _statement(client, monkeypatch, "cov_feb.pdf", "6609", ("2022-02-01", "2022-02-28"), -40.00, rows)
    await client.post(f"/api/imports/{feb}/commit", json={})
    [c] = (await client.get(f"/api/imports/{feb}/checks")).json()
    assert c["status"] == "ok" and c["detail"]["issues"] == []
    assert c["detail"]["listed_elsewhere"] == {"count": 1, "total": "-26.92"}


async def test_account_timeline_shows_gaps_and_imports_in_progress(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Timeline", "mask": "6606"})).json()["id"]
    months = [("01", "31", -11.01), ("02", "28", -12.02), ("03", "31", -13.03), ("06", "30", -16.06)]
    for m, last, amt in months:
        period = (f"2022-{m}-01", f"2022-{m}-{last}")
        rows = [(f"2022-{m}-10", f"TIMELINE SHOP {m}", amt)]
        bid = await _statement(client, monkeypatch, f"cov_tl_{m}.pdf", "6606", period, amt, rows)
        await client.post(f"/api/imports/{bid}/commit", json={})
    rows = [("2022-07-10", "TIMELINE SHOP 07", -17.07)]
    review = await _statement(client, monkeypatch, "cov_tl_07.pdf", "6606", ("2022-07-01", "2022-07-31"), -17.07, rows)

    items = (await client.get("/api/statement-checks/timeline", params={"account_id": acct})).json()
    assert [(i["kind"], i["period_end"]) for i in items] == [
        ("review", "2022-07-31"),
        ("statement", "2022-06-30"),
        ("missing", "2022-05-31"),
        ("statement", "2022-03-31"),
        ("statement", "2022-02-28"),
        ("statement", "2022-01-31"),
    ]
    assert items[0]["import_batch_id"] == review
    gap = items[2]
    assert gap["period_start"] == "2022-04-01" and gap["estimated"] == 2
    assert float(items[1]["statement_total"]) == float(items[1]["ledger_total"]) == -16.06


async def test_removed_feed_copy_lends_its_name_to_the_statement_copy(client, monkeypatch):
    from ledger.services.dedupe import more_descriptive

    assert more_descriptive("Invest529 Payment", "ACH Withdrawal")
    assert not more_descriptive("Home Depot", "THE HOME DEPOT SPRINGFIELD VA")
    assert not more_descriptive("True United", "EXXONMOBIL 47833538 SUITLAND MD")

    acct = (await client.post("/api/accounts", json={"name": "Coverage Names", "mask": "6607"})).json()["id"]
    feed = await _txn(client, acct, "2021-11-17", "INVEST529 PAYMENT", "-75.00")
    rows = [("2021-11-17", "ACH WITHDRAWAL", -75.00), ("2021-11-20", "NAMES GROCER", -10.00)]
    bid = await _statement(client, monkeypatch, "cov_nov.pdf", "6607", ("2021-11-01", "2021-11-30"), -85.00, rows)
    # The statement row is kept as its own transaction, leaving the feed's copy as a duplicate. Removing it closes the
    # difference exactly, so the removal is confident enough to happen at commit.
    d = (await client.post(f"/api/imports/{bid}/commit", json={"pending_as": "keep"})).json()
    assert d["stats"]["auto_fixed"] == 1
    assert (await client.get(f"/api/transactions/{feed}")).status_code == 404
    [c] = (await client.get(f"/api/imports/{bid}/checks")).json()
    assert c["status"] == "ok"
    page = (await client.get("/api/transactions", params={"import_batch_id": bid, "q": "INVEST529"})).json()
    [kept] = page["items"]
    assert kept["description"] == "INVEST529 PAYMENT"
    notes = [n["body"] for n in (await client.get(f"/api/transactions/{kept['id']}/notes")).json()]
    assert "Also described as: ACH WITHDRAWAL" in notes


async def test_same_file_imported_twice_is_flagged_not_explained(client, monkeypatch):
    acct = (await client.post("/api/accounts", json={"name": "Coverage Twice", "mask": "6608"})).json()["id"]
    rows = [("2021-12-10", "TWICE SHOP", -30.00), ("2021-12-11", "TWICE CAFE", -5.00)]
    period = ("2021-12-01", "2021-12-31")
    first = await _statement(client, monkeypatch, "cov_dec.pdf", "6608", period, -35.00, rows)
    await client.post(f"/api/imports/{first}/commit", json={})
    second = await _statement(client, monkeypatch, "cov_dec.pdf", "6608", period, -35.00, rows)
    assert second != first

    [c] = (await client.get(f"/api/imports/{second}/checks")).json()
    assert c["account_id"] == acct and c["detail"]["same_file"] == [first]
    assert not any(i["suggested"] for i in c["detail"]["issues"])
    r = await client.post(f"/api/imports/{second}/commit", json={"pending_as": "keep"})
    assert r.status_code == 409 and f"#{first}" in r.json()["detail"]


async def test_cards_sharing_an_account_are_checked_together(client, monkeypatch):
    from ledger.ai import imports as ai_imports

    acct = (await client.post("/api/accounts", json={"name": "Coverage Shared Card", "mask": "6611"})).json()["id"]
    await _txn(client, acct, "2022-01-03", "SHARED GROCER", "-40.00")
    await _txn(client, acct, "2022-01-15", "SHARED PAYMENT THANK YOU", "100.00")
    await _txn(client, acct, "2022-01-10", "AUTHORIZED USER CAFE", "-12.50")

    async def fake_extract(data, mime_type, filename):
        txn = ai_imports.ExtractedTxn
        return ai_imports.ExtractedStatement(
            document_type="credit_card_statement",
            institution="Coverage Bank",
            period_start="2022-01-01",
            period_end="2022-01-31",
            accounts=[
                # Only the primary card prints balances, and they cover both cards.
                ai_imports.StatementAccount(last4="6611", name="Primary", opening_balance=0, closing_balance=47.5),
                ai_imports.StatementAccount(last4="6612", name="Authorized User"),
            ],
            sign_note="as printed",
            summary="Shared card statement",
            transactions=[
                txn(date="2022-01-03", description="SHARED GROCER", amount=-40, confidence=0.99, account="6611"),
                txn(date="2022-01-15", description="SHARED PAYMENT THANK YOU", amount=100, confidence=0.99, account="6611"),
                txn(date="2022-01-10", description="AUTHORIZED USER CAFE", amount=-12.5, confidence=0.99, account="6612"),
            ],
        )

    monkeypatch.setattr(ai_imports, "extract_statement", fake_extract)
    b = (await client.post("/api/imports", files={"file": ("shared.pdf", b"%PDF-1.7 shared", "application/pdf")})).json()
    await _run("extract_document", {"batch_id": b["id"]})
    d = (await client.get(f"/api/imports/{b['id']}")).json()
    defaults = {**d["defaults"], "account_map": {"6611": acct, "6612": acct}}
    await client.post(f"/api/imports/{b['id']}/prepare", json={"mapping": d["mapping"], "defaults": defaults})
    await _run("prepare_import", {"batch_id": b["id"]})

    [c] = (await client.get(f"/api/imports/{b['id']}/checks")).json()
    assert (c["account_ref"], c["account_id"], c["statement_rows"]) == ("6611+6612", acct, 3)
    assert c["trusted"] is True and c["status"] == "ok", c["detail"]["issues"]
    await client.post(f"/api/imports/{b['id']}/commit", json={})
    [c] = (await client.get(f"/api/imports/{b['id']}/checks")).json()
    assert c["status"] == "ok" and c["difference"] == "0.00"
