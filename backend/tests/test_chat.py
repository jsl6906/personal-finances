import pytest
from google.genai import types

from ledger.chat import service as chat_service
from ledger.chat.sql import UnsafeQuery, run_readonly, validate


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM v_transactions",
        "SELECT * FROM transaction",
        "SELECT * FROM personal_finances.app_setting",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT pg_sleep(10)",
        "SELECT * FROM v_transactions; DROP TABLE transaction",
        "WITH x AS (DELETE FROM transaction RETURNING *) SELECT * FROM x",
        "SELECT set_config('role', 'ledger', false)",
        "SELECT * INTO t2 FROM v_transactions",
        "COPY (SELECT 1) TO '/tmp/x'",
        "SELECT * FROM v_transactions t JOIN attachment a ON true",
    ],
)
def test_validator_rejects(sql):
    with pytest.raises(UnsafeQuery):
        validate(sql)


def test_validator_allows_views_and_ctes():
    validate(
        "WITH m AS (SELECT date_trunc('month', date) AS mo, sum(amount) s FROM v_transactions GROUP BY 1) "
        "SELECT * FROM m ORDER BY mo;"
    )
    validate("SELECT category, sum(net_amount) FROM personal_finances.v_monthly_category GROUP BY 1 UNION ALL SELECT 'x', 1")


@pytest.fixture(scope="module")
async def chat_data(client):
    g = (await client.post("/api/category-groups", json={"name": "TC Group"})).json()
    cat = (await client.post("/api/categories", json={"name": "TC Coffee", "group_id": g["id"]})).json()
    for d, amt in (("2024-03-02", "-4.50"), ("2024-03-09", "-5.25"), ("2024-04-01", "-6.00")):
        r = await client.post(
            "/api/transactions",
            json={"txn_date": d, "description": "TC BEANERY", "amount": amt, "category_id": cat["id"]},
        )
        assert r.status_code == 201
    return cat


async def test_readonly_executor(chat_data):
    res = await run_readonly("SELECT sum(amount) AS total, count(*) AS n FROM v_transactions WHERE category = 'TC Coffee'")
    assert res["columns"] == ["total", "n"]
    assert res["rows"] == [[-15.75, 3]]
    # Runs as pf_readonly: the base tables are off limits even if the validator were bypassed
    from sqlalchemy import text

    from ledger.db.engine import get_engine

    async with get_engine().connect() as conn:
        await conn.execute(text("SET ROLE pf_readonly"))
        with pytest.raises(Exception, match="permission denied"):
            await conn.execute(text("SELECT * FROM personal_finances.transaction LIMIT 1"))
        await conn.rollback()


def _resp(parts: list[types.Part]) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=parts))])


async def test_chat_tool_loop(client, chat_data, monkeypatch):
    seen: list[list[types.Content]] = []

    async def fake_generate(contents, config):
        seen.append(list(contents))
        if len(seen) == 1:
            return _resp([types.Part.from_function_call(name="run_sql", args={"sql": "SELECT * FROM transaction"})])
        if len(seen) == 2:
            assert "not available" in contents[-1].parts[0].function_response.response["error"]
            return _resp(
                [
                    types.Part.from_function_call(
                        name="run_sql",
                        args={
                            "sql": "SELECT -sum(amount) AS spent FROM v_transactions WHERE category = 'TC Coffee' "
                            "AND date >= '2024-03-01' AND date < '2024-04-01'",
                            "purpose": "March coffee",
                        },
                    )
                ]
            )
        assert contents[-1].parts[0].function_response.response["rows"] == [[9.75]]
        return _resp([types.Part.from_text(text="You spent $9.75 on coffee in March 2024.")])

    monkeypatch.setattr(chat_service, "_generate", fake_generate)
    chat = (await client.post("/api/chat/sessions", json={})).json()
    r = await client.post(f"/api/chat/sessions/{chat['id']}/messages", data={"content": "Coffee spend in March 2024?"})
    assert r.status_code == 200, r.text
    user, bot = r.json()
    assert user["role"] == "user" and bot["role"] == "assistant"
    assert bot["content"] == "You spent $9.75 on coffee in March 2024."
    assert len(bot["queries"]) == 2
    assert bot["queries"][0]["error"] and bot["queries"][1]["rows"] == [[9.75]]

    sessions = (await client.get("/api/chat/sessions")).json()
    assert any(s["id"] == chat["id"] and s["title"].startswith("Coffee spend") for s in sessions)

    # Follow-up turn carries history; an attached CSV is sent as text
    async def fake_followup(contents, config):
        assert [c.role for c in contents] == ["user", "model", "user"]
        assert "Attached spreadsheet" in contents[-1].parts[0].text
        return _resp([types.Part.from_text(text="Looks fine.")])

    monkeypatch.setattr(chat_service, "_generate", fake_followup)
    r = await client.post(
        f"/api/chat/sessions/{chat['id']}/messages",
        data={"content": "Check this"},
        files={"file": ("tc.csv", b"Date,Amount\n2024-03-02,-4.50\n", "text/csv")},
    )
    user, bot = r.json()
    assert user["attachment_name"] == "tc.csv" and user["attachment_kind"] == "spreadsheet"
    assert bot["content"] == "Looks fine."

    r = await client.post(f"/api/chat/attachments/{user['attachment_id']}/import")
    assert r.status_code == 200 and r.json()["batch_id"]

    assert len((await client.get(f"/api/chat/sessions/{chat['id']}/messages")).json()) == 4
    assert (await client.delete(f"/api/chat/sessions/{chat['id']}")).status_code == 204


async def test_chat_answers_when_query_budget_runs_out(client, chat_data, monkeypatch):
    async def always_query(contents, config):
        if config.tool_config is not None:
            assert "no more queries" in contents[-1].parts[0].text
            return _resp([types.Part.from_text(text="Best answer from what I found.")])
        return _resp([types.Part.from_function_call(name="run_sql", args={"sql": "SELECT count(*) FROM v_accounts"})])

    monkeypatch.setattr(chat_service, "_generate", always_query)
    chat = (await client.post("/api/chat/sessions", json={})).json()
    r = await client.post(f"/api/chat/sessions/{chat['id']}/messages", data={"content": "loop forever"})
    bot = r.json()[1]
    assert bot["content"] == "Best answer from what I found."
    assert len(bot["queries"]) == chat_service.MAX_TOOL_CALLS


async def test_chat_model_failure_is_recorded(client, monkeypatch):
    async def boom(contents, config):
        raise RuntimeError("quota exhausted")

    monkeypatch.setattr(chat_service, "_generate", boom)
    chat = (await client.post("/api/chat/sessions", json={})).json()
    r = await client.post(f"/api/chat/sessions/{chat['id']}/messages", data={"content": "hi"})
    assert [m["role"] for m in r.json()] == ["user", "error"]
    assert "quota exhausted" in r.json()[1]["content"]
