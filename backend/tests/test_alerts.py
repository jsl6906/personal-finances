from datetime import date

import pytest
from sqlalchemy import select

from ledger.alerts import service as alerts
from ledger.db.engine import get_sessionmaker
from ledger.models import AlertEvent, Anomaly


async def test_mailer_builds_safe_message(monkeypatch):
    from ledger.alerts import mailer
    from ledger.config import Settings

    captured = {}

    async def fake_send(msg, **kwargs):
        captured.update(msg=msg, **kwargs)

    settings = Settings(smtp_host="smtp.example.com", smtp_from="Ledger <ledger@example.com>", smtp_username="u")
    monkeypatch.setattr(mailer, "get_settings", lambda: settings)
    monkeypatch.setattr(mailer.aiosmtplib, "send", fake_send)
    await mailer.send_email(["a@example.com", "b@example.com"], "Budget\r\nBcc: evil@example.com", "body", "<p>body</p>")
    msg = captured["msg"]
    assert msg["Subject"] == "Budget Bcc: evil@example.com" and msg["Bcc"] is None
    assert msg["To"] == "a@example.com, b@example.com"
    assert captured["hostname"] == "smtp.example.com" and captured["start_tls"] is True and captured["use_tls"] is False
    assert msg.get_body(("html",)).get_content().strip() == "<p>body</p>"


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []

    async def fake_send(to, subject, text, html=None):
        sent.append({"to": to, "subject": subject, "text": text, "html": html})

    monkeypatch.setattr(alerts, "send_email", fake_send)
    monkeypatch.setattr(alerts, "smtp_configured", lambda: True)

    async def no_ai(d):
        return "A quiet week."

    monkeypatch.setattr(alerts, "_ai_summary", no_ai)
    return sent


async def _events(like: str) -> list[AlertEvent]:
    async with get_sessionmaker()() as s:
        return list((await s.scalars(select(AlertEvent).where(AlertEvent.title.like(f"%{like}%")))).all())


async def test_recipients_and_rules_api(client):
    assert (await client.post("/api/alerts/recipients", json={"email": "not-an-email"})).status_code == 422
    r = await client.post("/api/alerts/recipients", json={"email": "Alerts@Example.com", "name": "Home"})
    assert r.status_code == 201 and r.json()["email"] == "alerts@example.com"
    assert (await client.post("/api/alerts/recipients", json={"email": "alerts@example.com"})).status_code == 409

    rules = {r["kind"]: r for r in (await client.get("/api/alerts/rules")).json()}
    assert set(rules) == {"budget_overspend", "out_of_norm", "large_transaction", "weekly_digest"}
    assert rules["large_transaction"]["params"]["threshold"] == 500
    r = await client.put("/api/alerts/rules/large_transaction", json={"threshold": 750, "pace": False})
    assert r.json()["params"] == {"threshold": 750.0}
    await client.put("/api/alerts/rules/large_transaction", json={"threshold": 500})

    status = (await client.get("/api/alerts/status")).json()
    assert status["recipients"] >= 1
    assert (await client.post("/api/alerts/test")).status_code == 409  # SMTP not configured in tests


async def test_budget_overspend_alerts_once(client, outbox):
    g = (await client.post("/api/category-groups", json={"name": "TAL Group"})).json()
    cat = (await client.post("/api/categories", json={"name": "TAL Hobbies", "group_id": g["id"]})).json()
    r = await client.post("/api/budgets", json={"category_id": cat["id"], "period_type": "month", "amount": "100"})
    assert r.status_code == 201, r.text
    today = date.today()
    await client.post(
        "/api/transactions",
        json={"txn_date": today.isoformat(), "description": "TAL KITS", "amount": "-160.00", "category_id": cat["id"]},
    )
    await client.post("/api/alerts/recipients", json={"email": "budget@example.com"})

    async with get_sessionmaker()() as s:
        res = await alerts.evaluate(s, today)
    assert res["new"] >= 1
    events = await _events("TAL Hobbies")
    assert len(events) == 1 and events[0].kind == "budget_overspend" and events[0].status == "sent"
    assert "budget@example.com" in events[0].recipients
    assert any("TAL Hobbies" in m["text"] for m in outbox)

    async with get_sessionmaker()() as s:
        await alerts.evaluate(s, today)
    assert len(await _events("TAL Hobbies")) == 1


async def test_anomaly_alerts_respect_rules(client, outbox):
    async with get_sessionmaker()() as s:
        s.add(
            Anomaly(
                kind="category_spike",
                subject_key="test:tal-spike",
                period=date.today().replace(day=1),
                amount=420,
                baseline=100,
                score=5,
                title="TAL Spike",
                detail="TAL Spike is 4.2x its norm.",
            )
        )
        await s.commit()

    await client.put("/api/alerts/rules/out_of_norm", json={"enabled": False})
    async with get_sessionmaker()() as s:
        await alerts.evaluate(s)
    assert await _events("TAL Spike") == []

    await client.put("/api/alerts/rules/out_of_norm", json={"enabled": True})
    async with get_sessionmaker()() as s:
        await alerts.evaluate(s)
    [event] = await _events("TAL Spike")
    assert event.kind == "out_of_norm" and event.status == "sent"
    assert "Out-of-norm" in event.title

    listed = (await client.get("/api/alerts/events")).json()
    assert any(e["id"] == event.id for e in listed)


async def test_undelivered_without_smtp(client):
    async with get_sessionmaker()() as s:
        s.add(
            Anomaly(
                kind="new_merchant",
                subject_key="test:tal-new",
                period=date.today().replace(day=1),
                amount=300,
                score=1.5,
                title="TAL Newshop",
                detail="First purchase.",
            )
        )
        await s.commit()
        await alerts.evaluate(s)
    [event] = await _events("TAL Newshop")
    assert event.status == "skipped" and "SMTP" in event.error


async def test_weekly_digest(client, outbox):
    preview = (await client.get("/api/alerts/digest/preview")).json()
    assert preview["subject"].startswith("Ledger weekly digest")
    assert "The week" in preview["text"] and "<h3" in preview["html"]

    async with get_sessionmaker()() as s:
        first = await alerts.send_digest(s, date(2031, 3, 3))
        again = await alerts.send_digest(s, date(2031, 3, 5))
    assert first["status"] == "sent" and again["status"] == "already_sent"
    assert outbox[-1]["subject"].startswith("Ledger weekly digest")
