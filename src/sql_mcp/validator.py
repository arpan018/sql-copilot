"""
sqlglot-based classification and schema checks. Never executes SQL.

Compiled SQL may contain %s or $1 placeholders. Those are rewritten
to NULL before parse so the tree still builds.

The schema cache from schema.py is the source of truth for identifier
checks — no extra catalog round-trip.
"""

from __future__ import annotations

import os
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from sql_mcp import schema as schema_mod

DEFAULT_SELECT_LIMIT = 200

# Postgres SQLSTATE → learner-facing sentence. Always return both raw and this.
ERROR_TRANSLATIONS = {
    "23503": (
        "You're trying to modify or delete a row that other rows depend on. "
        "Check which rows in other tables reference this one first."
    ),
    "23001": (
        "You're trying to modify or delete a row that other rows depend on "
        "(restrict rule). Check which rows in other tables reference this one first."
    ),
    "23502": (
        "This column requires a value — you cannot leave it empty (NULL). "
        "Provide a value for it."
    ),
    "23505": (
        "A row with this value already exists. The column requires unique "
        "values across all rows."
    ),
    "42703": (
        "The column name you used doesn't exist in this table. "
        "Call get_table_info to see available columns."
    ),
    "42P01": (
        "The table name you used doesn't exist. Call get_schema to see all "
        "available tables."
    ),
    "42601": (
        "The SQL has a syntax error. Check for missing commas, unmatched "
        "parentheses, or misspelled keywords."
    ),
    "40001": (
        "Two operations conflicted. The transaction was rolled back. Try again."
    ),
    "53300": (
        "PostgreSQL has too many open connections. Restart the MCP server "
        "to reset the connection."
    ),
}


def is_prod() -> bool:
    """
    Read MODE at call time, not import time.

    server.py loads .env after some imports; a cached import-time read
    would miss MODE=dev. Call this before executing mutations/DDL.
    """
    return os.environ.get("MODE", "dev").strip().lower() == "prod"


def translate_error(exc: BaseException) -> dict[str, str]:
    """Map psycopg2.pgcode to the spec's plain-English table."""
    raw = str(exc).strip().split("\n")[0]
    code = getattr(exc, "pgcode", None)
    translation = ERROR_TRANSLATIONS.get(code) if code else None
    if translation is None:
        translation = (
            "PostgreSQL rejected this statement. Read the raw message and "
            "compare identifiers with get_table_info."
        )
    return {
        "raw_error": raw,
        "pgcode": code or "unknown",
        "explanation": translation,
    }


def _strip_placeholders(sql: str) -> str:
    """Turn SQLAlchemy-style parameters into NULL so sqlglot can parse."""
    sql = re.sub(r"%s", "NULL", sql)
    sql = re.sub(r"%\([a-zA-Z_][a-zA-Z0-9_]*\)s", "NULL", sql)
    sql = re.sub(r"\$\d+", "NULL", sql)
    return sql


def parse_one(sql: str) -> dict[str, Any]:
    """
    Parse exactly one statement.

    Returns {ok, expression} or {ok: False, error, status}.
    """
    cleaned = _strip_placeholders(sql).strip().rstrip(";")
    if not cleaned:
        return {
            "ok": False,
            "status": "validation_error",
            "error": "Empty SQL string.",
            "sql_not_executed": True,
        }
    try:
        trees = sqlglot.parse(cleaned, dialect="postgres")
    except ParseError as exc:
        return {
            "ok": False,
            "status": "validation_error",
            "error": f"SQL syntax is invalid: {exc}",
            "sql_not_executed": True,
        }
    trees = [t for t in trees if t is not None]
    if len(trees) == 0:
        return {
            "ok": False,
            "status": "validation_error",
            "error": "No SQL statement found.",
            "sql_not_executed": True,
        }
    if len(trees) > 1:
        return {
            "ok": False,
            "status": "validation_error",
            "error": "Multiple SQL statements are not allowed. Send one statement per tool call.",
            "sql_not_executed": True,
        }
    return {"ok": True, "expression": trees[0]}


def classify_operation(expression: exp.Expression) -> str:
    """Return SELECT / INSERT / UPDATE / DELETE / ALTER / DROP / CREATE / TRUNCATE / OTHER."""
    if isinstance(expression, exp.Select) or isinstance(expression, exp.Union):
        return "SELECT"
    if isinstance(expression, exp.Insert):
        return "INSERT"
    if isinstance(expression, exp.Update):
        return "UPDATE"
    if isinstance(expression, exp.Delete):
        return "DELETE"
    if isinstance(expression, exp.Alter):
        return "ALTER"
    if isinstance(expression, exp.Drop):
        return "DROP"
    if isinstance(expression, exp.Create):
        return "CREATE"
    if isinstance(expression, exp.TruncateTable):
        return "TRUNCATE"
    key = expression.key.upper() if expression.key else "OTHER"
    if "TRUNCATE" in key:
        return "TRUNCATE"
    return key if key in {"SELECT", "INSERT", "UPDATE", "DELETE", "ALTER"} else "OTHER"


def tables_referenced(expression: exp.Expression) -> list[str]:
    names: list[str] = []
    for table in expression.find_all(exp.Table):
        name = table.name
        if name:
            names.append(name)
    # Preserve order, unique.
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def columns_referenced(expression: exp.Expression) -> list[str]:
    cols: list[str] = []
    seen: set[str] = set()
    for col in expression.find_all(exp.Column):
        label = col.name
        table = col.table
        key = f"{table}.{label}" if table else label
        if label and key not in seen and label != "*":
            seen.add(key)
            cols.append(key)
    return cols


def has_where(expression: exp.Expression) -> bool:
    return expression.find(exp.Where) is not None


def has_star(expression: exp.Expression) -> bool:
    return expression.find(exp.Star) is not None


def has_limit(expression: exp.Expression) -> bool:
    return expression.args.get("limit") is not None or expression.find(exp.Limit) is not None


def inject_select_limit(expression: exp.Expression) -> tuple[str, bool]:
    """
    Return (sql, limit_was_injected).

    Only SELECT trees get a LIMIT 200 when missing. The rewritten SQL is
    what run_select executes so the agent context window cannot explode.
    """
    if not isinstance(expression, (exp.Select, exp.Union)):
        return expression.sql(dialect="postgres"), False
    if has_limit(expression):
        return expression.sql(dialect="postgres"), False
    limited = expression.copy()
    limited.set("limit", exp.Limit(expression=exp.Literal.number(DEFAULT_SELECT_LIMIT)))
    return limited.sql(dialect="postgres"), True


def validate_against_schema(expression: exp.Expression) -> dict[str, Any] | None:
    """
    None means identifiers look fine. Otherwise a validation_error dict.

    Unqualified columns are accepted if they exist on any referenced table
    (dvd_rental queries often omit aliases).
    """
    cache = schema_mod.get_cached_schema()
    if cache is None:
        return {
            "status": "validation_error",
            "error": "Schema cache is empty. Call refresh_schema or restart the server.",
            "sql_not_executed": True,
        }
    tables = cache.get("tables") or {}
    unknown_tables: list[str] = []
    for name in tables_referenced(expression):
        if name not in tables:
            unknown_tables.append(name)
    if unknown_tables:
        return {
            "status": "validation_error",
            "error": f"Unknown table references: {', '.join(unknown_tables)}",
            "suggestion": f"Available tables: {', '.join(sorted(tables))}",
            "sql_not_executed": True,
        }

    unknown_cols: list[str] = []
    referenced = tables_referenced(expression)
    for col in expression.find_all(exp.Column):
        col_name = col.name
        if not col_name or col_name == "*":
            continue
        table_name = col.table
        if table_name:
            info = tables.get(table_name)
            if info is None:
                continue
            names = {c["name"] for c in info["columns"]}
            if col_name not in names:
                unknown_cols.append(f"{table_name}.{col_name}")
        else:
            found = False
            for t in referenced:
                info = tables.get(t)
                if not info:
                    continue
                if col_name in {c["name"] for c in info["columns"]}:
                    found = True
                    break
            if referenced and not found:
                unknown_cols.append(col_name)
    if unknown_cols:
        hint = ""
        if referenced:
            first = referenced[0]
            cols = [c["name"] for c in tables[first]["columns"]]
            hint = f"Available columns on '{first}': {', '.join(cols)}"
        return {
            "status": "validation_error",
            "error": f"Unknown column references: {', '.join(unknown_cols)}",
            "suggestion": hint,
            "sql_not_executed": True,
        }
    return None


def blocked_operation_message(op: str) -> dict[str, Any]:
    """DROP / CREATE / TRUNCATE / OTHER never reach a connection."""
    return {
        "status": "blocked",
        "operation_type": op,
        "sql_not_executed": True,
        "error": (
            f"{op} is not allowed through this MCP server. "
            "DROP, CREATE, and TRUNCATE are blocked. "
            "Use run_select, run_mutation, or alter_table_safe as appropriate."
        ),
    }


def expected_tool_for(op: str) -> str:
    if op == "SELECT":
        return "run_select"
    if op in {"INSERT", "UPDATE", "DELETE"}:
        return "run_mutation or dry_run"
    if op == "ALTER":
        return "alter_table_safe"
    return "validate_query (this operation cannot be executed)"
