from datetime import date

import pytest
from sqlalchemy import select

from ledger.ai import backfill as ai_backfill
from ledger.ai import imports as ai_imports
from ledger.ai import statements as ai_st
from ledger.config import get_settings
from ledger.db.engine import get_sessionmaker
from ledger.models import Account, BackfillFile, Statement, Transaction, statement_transaction
from ledger.sources import archive, backfill

STATEMENT = b"%PDF-1.4 tb bank january 2019"


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={"inbox_dir": tmp_path})
    monkeypatch.setattr(archive, "get_settings", lambda: settings)
    (tmp_path / "2019").mkdir()
    (tmp_path / "2019" / "tb_2019-01.pdf").write_bytes(STATEMENT)
    (tmp_path / "2019" / "tb_2019-01 (1).pdf").write_bytes(STATEMENT)
    (tmp_path / "2019" / "water_2019-01.pdf").write_bytes(b"%PDF-1.4 water bill")
    (tmp_path / "2019" / "receipt.jpg").write_bytes(b"\xff\xd8\xff receipt")
    (tmp_path / "2019" / "blurry.png").write_bytes(b"\x89PNG blurry")
    (tmp_path / "2019" / "tb_export.csv").write_text(
        "Date,Description,Amount\n2019-02-02,TB FEBRUARY SHOP,-17.71\n", encoding="utf-8"
    )
    (tmp_path / "notes.docx").write_bytes(b"PK not supported")
    (tmp_path / "sheets").mkdir()
    (tmp_path / "sheets" / "old_export.csv").write_text(
        "Date,Description,Amount\n2018-12-03,TB OLD SHOP,-12.34\n2018-12-09,TB OLD DINER,-23.45\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def fake_ai(monkeypatch):
    classes = {
        "tb_2019-01.pdf": ("bank_statement", "TB Bank", "5291", "2019-01-01", 0.95),
        "water_2019-01.pdf": ("utility_bill", "City Water Util", None, "2018-12-15", 0.9),
        "receipt.jpg": ("receipt", "Corner Store", None, None, 0.9),
        "blurry.png": ("other", None, None, None, 0.3),
    }

    async def classify(data, mime, filename, folder):
        kind, inst, last4, start, conf = classes[filename.replace(" (1)", "")]
        return ai_backfill.DocClass(
            document_type=kind, institution=inst, account_last4=last4, period_start=start, confidence=conf, reason="test"
        )

    async def extract(data, mime, filename):
        txns = [
            ("2019-01-05", "TB PAYROLL", 2000.0),
            ("2019-01-25", "CITY WATER UTIL", -45.17),
            ("2019-01-28", "TB GROCER", -61.02),
        ]
        return ai_imports.ExtractedStatement(
            document_type="bank_statement",
            institution="TB Bank",
            account_last4="5291",
            account_type="checking",
            period_start="2019-01-01",
            period_end="2019-01-31",
            opening_balance=1000.0,
            closing_balance=1000.0 + sum(t[2] for t in txns),
            sign_note="test",
            summary="TB Bank January 2019",
            transactions=[ai_imports.ExtractedTxn(date=d, description=s, amount=a, confidence=0.99) for d, s, a in txns],
        )

    async def bill(data, mime, filename):
        return ai_st.BillExtraction(
            document_type="utility_bill",
            vendor="CITY WATER UTIL",
            service_type="TB Water",
            statement_date="2019-01-18",
            period_start="2018-12-15",
            period_end="2019-01-14",
            due_date="2019-02-05",
            amount_due=45.17,
            usage=[ai_st.UsageMetric(metric="water_usage", value=4100, unit="gal", is_primary=True)],
            summary="Water bill",
        )

    async def no_pairs(pairs):
        return {}

    async def no_labels(labels, taxonomy):
        return []

    monkeypatch.setattr(ai_backfill, "classify_document", classify)
    monkeypatch.setattr(ai_imports, "extract_statement", extract)
    monkeypatch.setattr(ai_st, "extract_bill", bill)
    monkeypatch.setattr(ai_imports, "adjudicate_pairs", no_pairs)
    monkeypatch.setattr(ai_imports, "map_category_labels", no_labels)


async def test_local_backfill_end_to_end(client, inbox, fake_ai):
    assert (
        await client.put("/api/backfill/settings", json={"provider": "local", "folder": "../outside"})
    ).status_code == 422
    r = await client.put("/api/backfill/settings", json={"provider": "local", "folder": ""})
    assert r.status_code == 200, r.text

    async with get_sessionmaker()() as s:
        scan = await backfill.scan(s)
        assert scan == {**scan, "found": 8, "new": 8, "unsupported": 1}
        assert (await backfill.scan(s))["new"] == 0
        order = []
        while (step := await backfill.process_next(s)) is not None:
            order.append(step)
        assert len(order) == 7 + 4  # seven classifications, then four imports

    async with get_sessionmaker()() as s:
        files = {f.name: f for f in (await s.scalars(select(BackfillFile))).all()}
    assert files["notes.docx"].status == "skipped"
    copies = sorted([files["tb_2019-01 (1).pdf"], files["tb_2019-01.pdf"]], key=lambda f: f.status)
    statement, copy = copies  # "done" sorts before "skipped"
    assert copy.status == "skipped" and "Same content" in copy.message
    assert files["receipt.jpg"].status == "skipped"
    assert files["blurry.png"].status == "review"
    assert statement.status == "done" and statement.detail["inserted"] == 3, statement.message
    old = files["old_export.csv"]
    assert old.status == "done" and old.detail["inserted"] == 2, old.error
    bill = files["water_2019-01.pdf"]
    assert bill.status == "done" and "linked to its payment" in bill.message, bill.message

    # The bill was filed after the statement, so it found and linked the payment
    async with get_sessionmaker()() as s:
        acct = await s.scalar(select(Account).where(Account.name == "TB Bank ···5291"))
        assert acct is not None and acct.mask == "5291"
        water = await s.scalar(select(Transaction).where(Transaction.description == "CITY WATER UTIL"))
        assert water.account_id == acct.id and water.source_type == "backfill" and water.txn_date == date(2019, 1, 25)
        linked = await s.scalar(
            select(statement_transaction.c.transaction_id).where(statement_transaction.c.statement_id == bill.statement_id)
        )
        assert linked == water.id
        assert (await s.get(Statement, bill.statement_id)).status == "approved"
        # An account-less export in the statements' folder lands on the statements' account
        feb = await s.scalar(select(Transaction).where(Transaction.description == "TB FEBRUARY SHOP"))
        assert feb.account_id == acct.id

    ov = (await client.get("/api/backfill")).json()
    assert ov["summary"]["transactions_added"] >= 5 and ov["summary"]["by_status"]["review"] == 1
    flagged = (await client.get("/api/backfill/files", params={"status": "review"})).json()
    assert [f["name"] for f in flagged] == ["blurry.png"]

    # A flagged file can be re-routed by hand
    r = await client.post(f"/api/backfill/files/{flagged[0]['id']}", json={"action": "set_kind", "kind": "nonsense"})
    assert r.status_code == 422
    r = await client.post(f"/api/backfill/files/{flagged[0]['id']}", json={"action": "skip"})
    assert r.json()["status"] == "skipped"


async def test_watchdog_revives_broken_chain():
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete

    from ledger.models import Job
    from ledger.sources.jobs import backfill_watchdog

    async with get_sessionmaker()() as s:
        await backfill.save_settings(s, paused=True)
        f = BackfillFile(provider="local", external_id="watchdog/pending.pdf", name="pending.pdf", status="pending")
        stale = Job(type="backfill_run", status="running", started_at=datetime.now(UTC) - timedelta(hours=3))
        s.add_all([f, stale])
        await s.commit()
        assert await backfill_watchdog(s) is None  # paused

        await backfill.save_settings(s, paused=False)
        await s.commit()
        new_id = await backfill_watchdog(s)
        assert new_id is not None
        assert (await s.get(Job, stale.id, populate_existing=True)).status == "cancelled"
        assert await backfill_watchdog(s) is None  # the queued job counts as active

        await backfill.save_settings(s, paused=True)
        await s.execute(delete(Job).where(Job.id.in_([stale.id, new_id])))
        await s.execute(delete(BackfillFile).where(BackfillFile.id == f.id))
        await s.commit()


def test_drive_folder_ids():
    assert (
        archive.parse_folder_id("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp?usp=sharing")
        == "1AbCdEfGhIjKlMnOp"
    )
    assert archive.parse_folder_id("1AbCdEfGhIjKlMnOp") == "1AbCdEfGhIjKlMnOp"
    with pytest.raises(archive.ArchiveError):
        archive.parse_folder_id("not a folder")
