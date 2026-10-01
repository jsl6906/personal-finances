"""'Ask the ledger': Gemini answers questions by calling a read-only SQL tool over reporting views."""

import asyncio
import csv
import io
import logging
import time
from datetime import date

from google.genai import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.ai.client import _log_call, _retryable, get_client, model_for
from ledger.chat.sql import UnsafeQuery, run_readonly
from ledger.imports.parsing import detect_kind, read_table
from ledger.models import Account, Attachment, Category, CategoryGroup, ChatMessage, ChatSession, StatementSeries

log = logging.getLogger(__name__)
MAX_TOOL_CALLS = 6
HISTORY_MESSAGES = 16
SHEET_ROWS = 400

SCHEMA_DOC = """Views (PostgreSQL; query only these):
v_transactions(id, date, posted_date, description, merchant, amount, category, category_group, category_type,
  hidden_from_reports, account, account_type, institution, member, tags text[], notes, source,
  budget_spread_months, date_added, has_statement)
  - amount is signed: negative = money out, positive = money in.
  - category_type is 'expense' | 'income' | 'transfer' | NULL (uncategorized).
v_monthly_category(month, category, category_group, category_type, hidden_from_reports, net_amount, transactions)
v_categories(category, category_group, type, hidden_from_reports, is_active)
v_accounts(account, account_type, institution, mask, is_closed, is_hidden)
v_statements(id, series, vendor, document_type, status, statement_date, period_start, period_end, due_date,
  amount_due, summary, usage_metric, usage_value, usage_unit, linked_transaction_ids bigint[])
v_statement_usage(statement_id, series, period_start, period_end, statement_date, metric, value, unit, is_primary)
v_budgets(target, target_type, period_type, amount, notes)
v_balances(account, account_type, institution, as_of, balance, available, source)
  - daily balance history from connected sources; latest per account = max(as_of). Credit cards/loans are debts.
v_holdings(account, as_of, symbol, description, shares, market_value, cost_basis, currency, source)
v_transaction_notes(transaction_id, date, description, amount, account, note, source, document, created_at)
  - extra notes on a transaction (typed by hand, or details captured from imported statements); join to
    v_transactions on transaction_id = id."""

SYSTEM = """You are the household's finance analyst for their personal ledger. Answer questions using the run_sql
tool; never guess numbers. Today is {today}.

{schema}

Conventions:
- Spending = -sum(amount) over rows with category_type = 'expense' (refunds net out), plus uncategorized rows
  (category_type IS NULL) with negative amounts when the question is about all spending.
- Exclude category_type = 'transfer' and hidden_from_reports = true unless the user asks for them.
- Income = sum(amount) where category_type = 'income'.
- Match merchants/categories case-insensitively (ILIKE) and check v_categories for exact names when unsure.
- Utility usage (kWh, gallons, therms) lives in v_statement_usage / v_statements, linked to payments.
- Use date_trunc('month', date) for monthly breakdowns; state the period you used.

Categories (group > category): {categories}
Accounts: {accounts}
Bill series: {series}

Answer style: concise, lead with the answer, use $ with 2 decimals, short tables in Markdown when helpful,
mention assumptions (e.g. excluded transfers). If a document is attached, read it and relate it to the ledger
(e.g. find the matching payment, compare with prior periods). If data is missing, say so plainly."""

RUN_SQL = types.FunctionDeclaration(
    name="run_sql",
    description="Run one read-only PostgreSQL SELECT over the v_* views. Returns columns and up to 500 rows.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "sql": types.Schema(type=types.Type.STRING, description="A single SELECT statement"),
            "purpose": types.Schema(type=types.Type.STRING, description="What this query answers, in a few words"),
        },
        required=["sql"],
    ),
)


async def _context(session: AsyncSession) -> dict:
    cats = (
        await session.execute(
            select(CategoryGroup.name, Category.name)
            .join(Category.group)
            .where(Category.is_active)
            .order_by(CategoryGroup.sort_order, Category.name)
        )
    ).all()
    grouped: dict[str, list[str]] = {}
    for g, c in cats:
        grouped.setdefault(g, []).append(c)
    accounts = (await session.scalars(select(Account.name).where(Account.is_closed.is_(False)))).all()
    series = (await session.scalars(select(StatementSeries.name))).all()
    return {
        "today": date.today().isoformat(),
        "schema": SCHEMA_DOC,
        "categories": "; ".join(f"{g} > {', '.join(cs)}" for g, cs in grouped.items()),
        "accounts": ", ".join(accounts) or "(none)",
        "series": ", ".join(series) or "(none)",
    }


async def _attachment_part(session: AsyncSession, attachment_id: int) -> types.Part | None:
    row = (
        await session.execute(
            select(Attachment.mime_type, Attachment.content, Attachment.filename).where(Attachment.id == attachment_id)
        )
    ).first()
    if row is None:
        return None
    if detect_kind(row.filename) == "spreadsheet":
        table = read_table(row.content, row.filename)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=table.headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(table.rows[:SHEET_ROWS])
        more = f"\n(... {len(table.rows) - SHEET_ROWS} more rows not shown)" if len(table.rows) > SHEET_ROWS else ""
        return types.Part.from_text(text=f"Attached spreadsheet {row.filename}:\n{buf.getvalue()}{more}")
    return types.Part.from_bytes(data=row.content, mime_type=row.mime_type)


async def _generate(contents: list[types.Content], config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    model = model_for("main")
    delay = 2.0
    for attempt in range(1, 5):
        started = time.monotonic()
        try:
            resp = await get_client().aio.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:
            await _log_call("chat", model, started, None, repr(exc)[:2000])
            if attempt < 4 and _retryable(exc):
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
        await _log_call("chat", model, started, resp, None)
        return resp
    raise AssertionError("unreachable")


async def answer(session: AsyncSession, chat: ChatSession, user_msg: ChatMessage) -> ChatMessage:
    history = (
        await session.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == chat.id,
                ChatMessage.id != user_msg.id,
                ChatMessage.role.in_(("user", "assistant")),
            )
            .order_by(ChatMessage.id.desc())
            .limit(HISTORY_MESSAGES)
        )
    ).all()[::-1]
    recent_docs = {m.id for m in [*history, user_msg] if m.attachment_id}
    recent_docs = set(sorted(recent_docs)[-2:])

    contents: list[types.Content] = []
    for m in [*history, user_msg]:
        parts: list[types.Part] = []
        if m.attachment_id and m.id in recent_docs:
            part = await _attachment_part(session, m.attachment_id)
            if part:
                parts.append(part)
        parts.append(types.Part.from_text(text=m.content or "(see attached document)"))
        contents.append(types.Content(role="user" if m.role == "user" else "model", parts=parts))

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM.format(**await _context(session)),
        tools=[types.Tool(function_declarations=[RUN_SQL])],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.2,
    )
    # Once the query budget is spent, tools are disabled so the model must answer with what it has.
    final_config = config.model_copy(
        update={"tool_config": types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))}
    )
    queries: list[dict] = []
    text_out = ""
    for _ in range(MAX_TOOL_CALLS):
        resp = await _generate(contents, config)
        calls = resp.function_calls or []
        if not calls:
            text_out = (resp.text or "").strip()
            break
        contents.append(resp.candidates[0].content)
        responses = []
        for call in calls:
            args = dict(call.args or {})
            record = {"sql": args.get("sql", ""), "purpose": args.get("purpose")}
            try:
                result = await run_readonly(record["sql"])
                record.update(result)
                payload = {
                    "columns": result["columns"],
                    "rows": result["rows"][:200],
                    "row_count": result["row_count"],
                    "truncated": result["truncated"],
                }
            except UnsafeQuery as exc:
                record["error"] = str(exc)
                payload = {"error": str(exc)}
            except Exception as exc:  # SQL errors go back to the model so it can correct itself
                record["error"] = str(getattr(exc, "orig", exc))[:500]
                payload = {"error": record["error"]}
            queries.append({**record, "rows": record.get("rows", [])[:50]})
            responses.append(types.Part.from_function_response(name=call.name, response=payload))
        contents.append(types.Content(role="user", parts=responses))

    if not text_out:
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Answer the question now using the query results above; no more queries.")],
            )
        )
        resp = await _generate(contents, final_config)
        text_out = (
            resp.text or ""
        ).strip() or "I ran out of query steps before reaching an answer; try a narrower question."

    reply = ChatMessage(session_id=chat.id, role="assistant", content=text_out, queries=queries)
    session.add(reply)
    await session.flush()
    log.info("chat %s: %d queries", chat.id, len(queries))
    return reply
