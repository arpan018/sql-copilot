"""
Execute tools: validate, run SQL with safeguards, log, refresh schema.

Implementations are ordinary functions so other Python code can import
them without the MCP protocol. register(mcp) only wraps them with @mcp.tool.

This module never imports sql_mcp.server.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor
from sqlglot import exp

from sql_mcp import connection, schema as schema_mod
from sql_mcp import logger
from sql_mcp import validator as val


def register(mcp) -> None:
    """Attach every execute tool. Called once from server.py."""

    @mcp.tool
    def run_select(sql: str) -> dict:
        """Run a SELECT preview of at most 10 rows, plus SQL to paste into pgAdmin."""
        return run_select_impl(sql)

    @mcp.tool
    def validate_query(sql: str) -> dict:
        """Analyse any SQL string without executing it, including destructive statements."""
        return validate_query_impl(sql)

    @mcp.tool
    def dry_run(sql: str) -> dict:
        """Do not run INSERT/UPDATE/DELETE. Return the SQL and say auto-execute is disabled."""
        return dry_run_impl(sql)

    @mcp.tool
    def run_mutation(sql: str, reason: str | None = None) -> dict:
        """Do not run INSERT/UPDATE/DELETE. Return the SQL."""
        return run_mutation_impl(sql, reason)

    @mcp.tool
    def alter_table_safe(ddl: str) -> dict:
        """Do not run ALTER. Return the statement and say auto-execute is disabled."""
        return alter_table_safe_impl(ddl)

    @mcp.tool
    def refresh_schema() -> dict:
        """Re-read information_schema into the in-memory cache."""
        return refresh_schema_impl()

    @mcp.tool
    def get_logs(last_n: int = 50, operation_type: str | None = None) -> dict:
        """Return recent audit-log entries from the JSONL file."""
        return get_logs_impl(last_n, operation_type)

    @mcp.tool
    def get_status() -> dict:
        """Connection, cache age, and recent mutation summary in one call."""
        return get_status_impl()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return value


def _rows_as_dicts(rows: list) -> list[dict]:
    out = []
    for row in rows:
        item = dict(row)
        out.append({k: _jsonable(v) for k, v in item.items()})
    return out


def _approx_rows(table: str) -> int | None:
    detail = schema_mod.get_table_detail(table)
    if detail.get("status") == "error":
        return None
    return detail.get("approximate_row_count")


def _parse_or_error(sql: str) -> dict[str, Any]:
    parsed = val.parse_one(sql)
    if not parsed.get("ok"):
        return parsed
    return parsed


def run_select_impl(sql: str) -> dict:
    started = time.perf_counter()
    parsed = val.parse_one(sql)
    if not parsed.get("ok"):
        logger.log_entry(tool="run_select", status="validation_error", sql=sql, error=parsed.get("error"))
        return parsed
    expr = parsed["expression"]
    op = val.classify_operation(expr)
    hidden_write = val.select_hides_a_write(expr)
    if op != "SELECT" or hidden_write:
        result = {
            "status": "auto_execute_disabled",
            "operation_type": op,
            "sql": sql,
            "sql_not_executed": True,
            "message": hidden_write or val.AUTO_EXECUTE_DISABLED,
        }
        logger.log_entry(tool="run_select", status="auto_execute_disabled", operation_type=op, sql=sql)
        return result
    schema_err = val.validate_against_schema(expr)
    if schema_err:
        logger.log_entry(tool="run_select", status="validation_error", operation_type="SELECT", sql=sql, error=schema_err.get("error"))
        return schema_err
    plan = val.preview_and_local_sql(expr, sql)
    exec_sql = plan["preview_sql"]
    try:
        with connection.get_db_connection(autocommit=True) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(exec_sql)
                rows = cur.fetchmany(val.PREVIEW_ROW_LIMIT)
                columns = [d.name for d in cur.description] if cur.description else []
        ms = round((time.perf_counter() - started) * 1000, 2)
        payload = {
            "status": "success",
            "rows_returned": len(rows),
            "preview_row_limit": val.PREVIEW_ROW_LIMIT,
            "preview_capped": plan["preview_capped"],
            "columns": columns,
            "rows": _rows_as_dicts(rows),
            "sql_to_run_locally": plan["local_sql"],
            "local_limit_added": plan["local_limit_added"],
            "execution_time_ms": ms,
            "message": (
                f"Showing at most {val.PREVIEW_ROW_LIMIT} rows so the agent context stays small. "
                "Run sql_to_run_locally in pgAdmin for the larger result."
            ),
        }
        logger.log_entry(
            tool="run_select",
            status="success",
            operation_type="SELECT",
            sql=exec_sql,
            rows_affected=len(rows),
            execution_time_ms=ms,
        )
        return payload
    except psycopg2.Error as exc:
        translated = val.translate_error(exc)
        logger.log_entry(tool="run_select", status="error", operation_type="SELECT", sql=exec_sql, error=translated["raw_error"])
        return {"status": "error", "sql_not_executed": False, **translated}


def validate_query_impl(sql: str) -> dict:
    parsed = val.parse_one(sql)
    if not parsed.get("ok"):
        logger.log_entry(tool="validate_query", status="validation_error", sql=sql, error=parsed.get("error"))
        return {
            "operation_type": "UNKNOWN",
            "syntax_valid": False,
            "schema_valid": False,
            "tables_referenced": [],
            "columns_referenced": [],
            "warnings": [parsed.get("error")],
            "safe_to_auto_execute": False,
            "explanation": parsed.get("error"),
        }
    expr = parsed["expression"]
    op = val.classify_operation(expr)
    schema_err = None
    if op not in {"DROP", "CREATE", "TRUNCATE", "OTHER"}:
        schema_err = val.validate_against_schema(expr)
    schema_valid = schema_err is None
    tables = val.tables_referenced(expr)
    warnings: list[str] = []
    if val.has_star(expr) and op == "SELECT":
        warnings.append("SELECT * will return every column. Prefer an explicit column list.")
    if op in {"UPDATE", "DELETE"} and not val.has_where(expr):
        approx = _approx_rows(tables[0]) if tables else None
        extra = f" (approximately {approx} rows)" if approx is not None else ""
        warnings.append(
            f"{op} without WHERE clause — this will affect ALL rows in '{tables[0] if tables else '?'}'{extra}"
        )
    if op in {"UPDATE", "DELETE"} and val.where_matches_every_row(expr):
        warnings.append(
            f"{op} WHERE true or WHERE 1=1 matches every row. Running it in a SQL client would affect the whole table."
        )
    hidden_write = val.select_hides_a_write(expr)
    if hidden_write:
        warnings.append(hidden_write)
    if op in {"INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE", "TRUNCATE"} or hidden_write:
        warnings.append(val.AUTO_EXECUTE_DISABLED)
    if op == "SELECT" and not hidden_write:
        warnings.append(
            f"Use run_select for a preview of at most {val.PREVIEW_ROW_LIMIT} rows. This tool never runs SQL."
        )
    explanation = f"This is a {op} statement."
    if op in {"UPDATE", "DELETE"} and not val.has_where(expr):
        explanation = (
            f"This {op} has no WHERE filter. Every matching row in the target table "
            "would be changed or removed permanently."
        )
    elif op in {"UPDATE", "DELETE"} and val.where_matches_every_row(expr):
        explanation = (
            f"This {op} uses WHERE true or WHERE 1=1, so every row would be changed or removed."
        )
    result = {
        "operation_type": op,
        "syntax_valid": True,
        "schema_valid": schema_valid,
        "tables_referenced": tables,
        "columns_referenced": val.columns_referenced(expr),
        "warnings": warnings,
        "safe_to_auto_execute": op == "SELECT" and schema_valid and not hidden_write,
        "explanation": explanation,
        "expected_tool": val.expected_tool_for(op),
    }
    if schema_err:
        result["schema_error"] = schema_err.get("error")
        result["suggestion"] = schema_err.get("suggestion")
    logger.log_entry(tool="validate_query", status="analysed", operation_type=op, sql=sql)
    return result


def _refuse_write(sql: str, tool: str, reason: str | None = None) -> dict:
    """Parse a write statement, keep schema warnings, and do not open a transaction."""
    parsed = val.parse_one(sql)
    if not parsed.get("ok"):
        logger.log_entry(tool=tool, status="validation_error", sql=sql, error=parsed.get("error"))
        return parsed
    expr = parsed["expression"]
    op = val.classify_operation(expr)
    hidden_write = val.select_hides_a_write(expr)
    if op == "SELECT" and not hidden_write:
        err = {
            "status": "validation_error",
            "error": f"{tool} does not run SELECT. Use run_select for a {val.PREVIEW_ROW_LIMIT}-row preview.",
            "sql": sql,
            "sql_not_executed": True,
        }
        logger.log_entry(tool=tool, status="validation_error", operation_type=op, sql=sql, error=err["error"])
        return err
    if hidden_write:
        op = "SELECT"

    warnings: list[str] = [val.AUTO_EXECUTE_DISABLED]
    schema_err = None
    if op not in {"DROP", "CREATE", "TRUNCATE", "OTHER"}:
        schema_err = val.validate_against_schema(expr)
        if schema_err and schema_err.get("error"):
            warnings.append(str(schema_err["error"]))
    if op in {"UPDATE", "DELETE"} and not val.has_where(expr):
        warnings.append(f"{op} has no WHERE clause. Running it in a SQL client would affect every row.")
    if op in {"UPDATE", "DELETE"} and val.where_matches_every_row(expr):
        warnings.append(f"{op} WHERE true or WHERE 1=1 matches every row.")
    if hidden_write:
        warnings.append(hidden_write)

    result: dict[str, Any] = {
        "status": "auto_execute_disabled",
        "operation_type": op,
        "sql": sql.strip(),
        "sql_not_executed": True,
        "message": hidden_write or val.AUTO_EXECUTE_DISABLED,
        "warnings": warnings,
    }
    if schema_err:
        result["schema_error"] = schema_err.get("error")
        result["suggestion"] = schema_err.get("suggestion")
    if op == "ALTER":
        preview = _alter_preview(sql, expr)
        result["change_description"] = preview.get("change_description")
        result["data_at_risk"] = preview.get("data_at_risk")
    logger.log_entry(
        tool=tool,
        status="auto_execute_disabled",
        operation_type=op,
        sql=sql,
        reason=reason,
    )
    return result


def dry_run_impl(sql: str) -> dict:
    return _refuse_write(sql, "dry_run")


def run_mutation_impl(sql: str, reason: str | None = None) -> dict:
    return _refuse_write(sql, "run_mutation", reason=reason)


def _alter_preview(ddl: str, expr: exp.Expression) -> dict:
    tables = val.tables_referenced(expr)
    table = tables[0] if tables else None
    cache = schema_mod.get_cached_schema() or {}
    table_info = (cache.get("tables") or {}).get(table or "")
    columns_before = [c["name"] for c in table_info["columns"]] if table_info else []
    ddl_u = ddl.lower()
    operation = "ALTER TABLE"
    change = ddl.strip()
    data_at_risk = "Review this DDL carefully. Some ALTER operations cannot be undone."
    predicted = list(columns_before)
    current_exists = True
    if "drop column" in ddl_u:
        operation = "DROP COLUMN"
        # crude name grab after DROP COLUMN
        after = ddl_u.split("drop column", 1)[1]
        col = after.replace("if exists", "").split(",")[0].split(";")[0].strip().strip('"')
        col = col.split()[0] if col else ""
        current_exists = col in columns_before
        predicted = [c for c in columns_before if c != col]
        change = f"Column '{col}' will be permanently removed from table '{table}'"
        data_at_risk = (
            f"Any data stored in {col} will be lost permanently. This cannot be undone."
        )
    elif "add column" in ddl_u:
        operation = "ADD COLUMN"
        after = ddl_u.split("add column", 1)[1]
        col = after.replace("if not exists", "").split()[0].strip().strip('"')
        predicted = columns_before + ([col] if col and col not in columns_before else [])
        change = f"Column '{col}' will be added to table '{table}'"
        data_at_risk = "Existing rows will get NULL or the specified default for the new column."
        current_exists = table_info is not None
    elif "rename" in ddl_u:
        operation = "RENAME"
        change = f"Rename in table '{table}': {ddl.strip()}"
    return {
        "status": "preview",
        "operation": operation,
        "table": table,
        "change_description": change,
        "current_column_exists": current_exists,
        "data_at_risk": data_at_risk,
        "current_schema_snapshot": {"columns_before": columns_before},
        "predicted_schema_after": {"columns_after": predicted},
        "to_proceed": val.AUTO_EXECUTE_DISABLED,
    }


def alter_table_safe_impl(ddl: str) -> dict:
    return _refuse_write(ddl, "alter_table_safe")


def refresh_schema_impl() -> dict:
    age = schema_mod.cache_age_seconds()
    payload = schema_mod.refresh_schema()
    result = {
        "status": "refreshed",
        "previous_cache_age_seconds": round(age, 1) if age is not None else None,
        "tables_found": len(payload.get("tables") or {}),
        "cached_at": payload.get("cached_at"),
        "schema": payload,
    }
    logger.log_entry(tool="refresh_schema", status="refreshed", operation_type="DDL")
    return result


def get_logs_impl(last_n: int, operation_type: str | None) -> dict:
    n = last_n if last_n and last_n > 0 else 50
    entries = logger.read_entries(last_n=n, operation_type=operation_type)
    return {
        "log_file": str(logger.log_path()),
        "entries_returned": len(entries),
        "entries": entries,
    }


def get_status_impl() -> dict:
    warnings: list[str] = []
    connection_state = "unhealthy"
    try:
        with connection.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        connection_state = "healthy"
    except Exception as exc:
        warnings.append(str(exc).split("\n")[0])
    cache = schema_mod.get_cached_schema()
    age = schema_mod.cache_age_seconds()
    return {
        "connection": connection_state,
        "database": os.environ.get("DB_NAME", "unknown"),
        "mode": os.environ.get("MODE", "dev"),
        "schema_cache_age_seconds": round(age, 1) if age is not None else None,
        "tables_in_cache": len((cache or {}).get("tables") or {}),
        "log_entries_today": logger.count_entries_today(),
        "last_mutation": logger.last_mutation(),
        "warnings": warnings,
    }
