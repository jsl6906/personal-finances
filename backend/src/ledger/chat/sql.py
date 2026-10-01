"""Guarded, read-only SQL execution for the chat assistant.

Defense in depth: queries are parsed and restricted to SELECTs over the v_* views, then run in a
READ ONLY transaction as the pf_readonly role (which can only SELECT those views) with a timeout.
"""

import datetime as dt
from decimal import Decimal

import sqlglot
from sqlalchemy import text
from sqlglot import exp

from ledger.config import get_settings
from ledger.db.engine import get_engine

ALLOWED_VIEWS = {
    "v_transactions",
    "v_categories",
    "v_accounts",
    "v_statements",
    "v_statement_usage",
    "v_budgets",
    "v_monthly_category",
    "v_balances",
    "v_holdings",
    "v_transaction_notes",
}
BLOCKED_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "lo_import",
    "lo_export",
    "dblink",
    "set_config",
    "current_setting",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "query_to_xml",
    "pg_stat_file",
}
MAX_ROWS = 500


class UnsafeQuery(ValueError):
    pass


def validate(sql: str) -> str:
    sql = sql.strip().rstrip(";")
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise UnsafeQuery(f"Could not parse SQL: {exc}") from None
    if len(statements) != 1 or statements[0] is None:
        raise UnsafeQuery("Exactly one SELECT statement is allowed")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise UnsafeQuery("Only SELECT queries are allowed")
    for node in tree.walk():
        if isinstance(
            node,
            (
                exp.Insert,
                exp.Update,
                exp.Delete,
                exp.Create,
                exp.Drop,
                exp.Alter,
                exp.Command,
                exp.Merge,
                exp.TruncateTable,
                exp.Into,
                exp.Lock,
            ),
        ):
            raise UnsafeQuery("Only read-only SELECT queries are allowed")
        if isinstance(node, exp.Func):
            name = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if name in BLOCKED_FUNCTIONS:
                raise UnsafeQuery(f"Function {name} is not allowed")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if name in ctes:
            continue
        if table.db and table.db.lower() not in ("", get_settings().db_schema):
            raise UnsafeQuery(f"Schema {table.db} is not available")
        if name not in ALLOWED_VIEWS:
            raise UnsafeQuery(f"Table {table.name} is not available; use one of: {', '.join(sorted(ALLOWED_VIEWS))}")
    return sql


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


async def run_readonly(sql: str, max_rows: int = MAX_ROWS) -> dict:
    """Validate and execute; returns {columns, rows, row_count, truncated}."""
    safe = validate(sql)
    schema = get_settings().db_schema
    async with get_engine().connect() as conn:
        async with conn.begin() as tx:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text("SET LOCAL ROLE pf_readonly"))
            await conn.execute(text("SET LOCAL statement_timeout = '8s'"))
            await conn.execute(text(f'SET LOCAL search_path = "{schema}", public'))
            result = await conn.execute(text(f"SELECT * FROM ({safe}) AS q LIMIT {max_rows + 1}"))
            columns = list(result.keys())
            rows = [[_jsonable(v) for v in r] for r in result.fetchall()]
            await tx.rollback()
    truncated = len(rows) > max_rows
    return {"columns": columns, "rows": rows[:max_rows], "row_count": min(len(rows), max_rows), "truncated": truncated}
