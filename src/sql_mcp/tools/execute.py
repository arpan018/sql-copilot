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
        """Execute a SELECT with schema checks and an automatic LIMIT 200 if missing."""
        return run_select_impl(sql)

    @mcp.tool
    def validate_query(sql: str) -> dict:
        """Analyse any SQL string without executing it, including destructive statements."""
        return validate_query_impl(sql)

    @mcp.tool
    def dry_run(sql: str) -> dict:
        """Run INSERT/UPDATE/DELETE inside a transaction and always roll back."""
        return dry_run_impl(sql)

    @mcp.tool
    def run_mutation(
        sql: str,
        confirmed: bool = False,
        reason: str | None = None,
        override_no_where: bool = False,
    ) -> dict:
        """Execute INSERT/UPDATE/DELETE for real. Requires confirmed=true."""
        return run_mutation_impl(sql, confirmed, reason, override_no_where)

    @mcp.tool
    def alter_table_safe(ddl: str, confirmed: bool = False) -> dict:
        """Preview or run ALTER TABLE, then refresh the schema cache on success."""
        return alter_table_safe_impl(ddl, confirmed)

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
    if op != "SELECT":
        result = {
            "status": "validation_error",
            "error": f"run_select only accepts SELECT. This is {op}. Use {val.expected_tool_for(op)} instead.",
            "sql_not_executed": True,
        }
        logger.log_entry(tool="run_select", status="validation_error", operation_type=op, sql=sql, error=result["error"])
        return result
    schema_err = val.validate_against_schema(expr)
    if schema_err:
        logger.log_entry(tool="run_select", status="validation_error", operation_type="SELECT", sql=sql, error=schema_err.get("error"))
        return schema_err
    exec_sql, injected = val.inject_select_limit(expr)
    try:
        with connection.get_db_connection(autocommit=True) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(exec_sql)
                rows = cur.fetchall()
                columns = [d.name for d in cur.description] if cur.description else []
        ms = round((time.perf_counter() - started) * 1000, 2)
        payload = {
            "status": "success",
            "row_count": len(rows),
            "rows_returned": len(rows),
            "limit_applied": injected,
            "columns": columns,
            "rows": _rows_as_dicts(rows),
            "execution_time_ms": ms,
        }
        if injected:
            payload["warning"] = (
                f"No LIMIT clause found. LIMIT {val.DEFAULT_SELECT_LIMIT} was applied "
                "automatically to prevent large result sets."
            )
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
        warnings.append("This is a destructive operation. Use run_mutation with confirmed=true to execute.")
    if op in {"DROP", "TRUNCATE"}:
        warnings.append(f"{op} is always blocked by this server.")
    if op == "CREATE":
        warnings.append("CREATE is not supported by this server.")
    if op == "ALTER":
        warnings.append("Use alter_table_safe for a diff preview before running this DDL.")
    if op == "SELECT":
        warnings.append("Use run_select to execute. This tool never runs SQL.")
    explanation = f"This is a {op} statement."
    if op in {"UPDATE", "DELETE"} and not val.has_where(expr):
        explanation = (
            f"This {op} has no WHERE filter. Every matching row in the target table "
            "would be changed or removed permanently."
        )
    result = {
        "operation_type": op,
        "syntax_valid": True,
        "schema_valid": schema_valid,
        "tables_referenced": tables,
        "columns_referenced": val.columns_referenced(expr),
        "warnings": warnings,
        "safe_to_auto_execute": op == "SELECT" and schema_valid,
        "explanation": explanation,
        "expected_tool": val.expected_tool_for(op),
    }
    if schema_err:
        result["schema_error"] = schema_err.get("error")
        result["suggestion"] = schema_err.get("suggestion")
    logger.log_entry(tool="validate_query", status="analysed", operation_type=op, sql=sql)
    return result


def _mutation_gate(sql: str, tool: str) -> dict[str, Any]:
    parsed = val.parse_one(sql)
    if not parsed.get("ok"):
        return parsed
    expr = parsed["expression"]
    op = val.classify_operation(expr)
    if op in {"DROP", "CREATE", "TRUNCATE", "OTHER"}:
        blocked = val.blocked_operation_message(op)
        logger.log_entry(tool=tool, status="blocked", operation_type=op, sql=sql, error=blocked["error"])
        return blocked
    if op not in {"INSERT", "UPDATE", "DELETE"}:
        err = {
            "status": "validation_error",
            "error": f"{tool} only accepts INSERT, UPDATE, or DELETE. This is {op}. Use {val.expected_tool_for(op)}.",
            "sql_not_executed": True,
        }
        logger.log_entry(tool=tool, status="validation_error", operation_type=op, sql=sql, error=err["error"])
        return err
    schema_err = val.validate_against_schema(expr)
    if schema_err:
        logger.log_entry(tool=tool, status="validation_error", operation_type=op, sql=sql, error=schema_err.get("error"))
        return schema_err
    return {"ok": True, "expression": expr, "operation": op}


def _target_table(expr: exp.Expression) -> str | None:
    table = expr.find(exp.Table)
    return table.name if table else None


def _sample_before(conn, expr: exp.Expression, op: str) -> list[dict]:
    """SELECT the rows that the WHERE clause would hit, before the mutation."""
    if op == "INSERT":
        return []
    table = _target_table(expr)
    if not table:
        return []
    where = expr.find(exp.Where)
    sample_sql = f'SELECT * FROM "{table}"'
    if where:
        sample_sql += " " + where.sql(dialect="postgres")
    sample_sql += " LIMIT 5"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sample_sql)
        return _rows_as_dicts(cur.fetchall())


def dry_run_impl(sql: str) -> dict:
    started = time.perf_counter()
    gated = _mutation_gate(sql, "dry_run")
    if not gated.get("ok"):
        return gated
    expr = gated["expression"]
    op = gated["operation"]
    try:
        with connection.get_db_connection(autocommit=False) as conn:
            sample = _sample_before(conn, expr, op)
            with conn.cursor() as cur:
                if op == "INSERT":
                    insert_sql = sql.rstrip().rstrip(";")
                    if "returning" not in insert_sql.lower():
                        insert_sql = insert_sql + " RETURNING *"
                    cur.execute(insert_sql)
                    returning = cur.fetchmany(5)
                    colnames = [d[0] for d in cur.description] if cur.description else []
                    sample = [
                        {colnames[i]: _jsonable(row[i]) for i in range(len(colnames))}
                        for row in returning
                    ]
                    affected = cur.rowcount
                else:
                    cur.execute(sql)
                    affected = cur.rowcount
            conn.rollback()
        ms = round((time.perf_counter() - started) * 1000, 2)
        result = {
            "status": "dry_run_complete",
            "operation": op,
            "rows_that_would_be_affected": affected,
            "sample_affected_rows": sample,
            "sample_size": len(sample),
            "rolled_back": True,
            "to_execute_for_real": "Call run_mutation with this SQL and confirmed=true",
            "execution_time_ms": ms,
        }
        logger.log_entry(
            tool="dry_run",
            status="dry_run_complete",
            operation_type=op,
            sql=sql,
            rows_affected=affected,
            execution_time_ms=ms,
        )
        return result
    except psycopg2.Error as exc:
        translated = val.translate_error(exc)
        logger.log_entry(tool="dry_run", status="error", operation_type=op, sql=sql, error=translated["raw_error"])
        return {"status": "error", "rolled_back": True, **translated}


def run_mutation_impl(
    sql: str,
    confirmed: bool,
    reason: str | None,
    override_no_where: bool,
) -> dict:
    if not confirmed:
        logger.log_entry(tool="run_mutation", status="confirmation_required", sql=sql)
        return {
            "status": "confirmation_required",
            "message": (
                "This is a mutation operation. Run dry_run first to preview the effect, "
                "then call run_mutation with confirmed=true."
            ),
            "sql_not_executed": True,
        }
    gated = _mutation_gate(sql, "run_mutation")
    if not gated.get("ok"):
        return gated
    expr = gated["expression"]
    op = gated["operation"]
    extra = {"mode": "prod"} if val.is_prod() else {}
    if op in {"UPDATE", "DELETE"} and not val.has_where(expr):
        if val.is_prod():
            msg = {
                "status": "blocked",
                "reason": (
                    "MODE=prod blocks UPDATE/DELETE without a WHERE clause even when "
                    "confirmed and override_no_where are both set."
                ),
                "sql_not_executed": True,
            }
            logger.log_entry(
                tool="run_mutation",
                status="blocked",
                operation_type=op,
                sql=sql,
                extra=extra,
            )
            return msg
        if not override_no_where:
            logger.log_entry(tool="run_mutation", status="confirmation_required", operation_type=op, sql=sql)
            return {
                "status": "confirmation_required",
                "message": (
                    f"{op} without WHERE needs confirmed=true AND override_no_where=true "
                    "in MODE=dev."
                ),
                "sql_not_executed": True,
            }
    started = time.perf_counter()
    try:
        with connection.get_db_connection(autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                affected = cur.rowcount
            conn.commit()
        ms = round((time.perf_counter() - started) * 1000, 2)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        result = {
            "status": "executed",
            "operation": op,
            "rows_affected": affected,
            "committed": True,
            "logged": True,
            "timestamp": ts,
        }
        logger.log_entry(
            tool="run_mutation",
            status="success",
            operation_type=op,
            sql=sql,
            rows_affected=affected,
            execution_time_ms=ms,
            reason=reason,
            extra=extra or None,
        )
        return result
    except psycopg2.Error as exc:
        translated = val.translate_error(exc)
        logger.log_entry(
            tool="run_mutation",
            status="error",
            operation_type=op,
            sql=sql,
            error=translated["raw_error"],
            reason=reason,
            extra=extra or None,
        )
        return {"status": "error", "committed": False, **translated}


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
        "to_proceed": "Call alter_table_safe again with the same DDL and confirmed=true",
    }


def alter_table_safe_impl(ddl: str, confirmed: bool) -> dict:
    if val.is_prod():
        msg = {
            "status": "blocked",
            "reason": (
                "MODE=prod blocks all ALTER TABLE operations through this MCP server. "
                "Run schema migrations through your normal migration tooling instead."
            ),
        }
        logger.log_entry(tool="alter_table_safe", status="blocked", operation_type="ALTER", sql=ddl)
        return msg
    parsed = val.parse_one(ddl)
    if not parsed.get("ok"):
        logger.log_entry(tool="alter_table_safe", status="validation_error", sql=ddl, error=parsed.get("error"))
        return parsed
    expr = parsed["expression"]
    op = val.classify_operation(expr)
    if op != "ALTER":
        err = {
            "status": "validation_error",
            "error": f"alter_table_safe only accepts ALTER TABLE. This is {op}.",
            "sql_not_executed": True,
        }
        logger.log_entry(tool="alter_table_safe", status="validation_error", operation_type=op, sql=ddl, error=err["error"])
        return err
    if not confirmed:
        preview = _alter_preview(ddl, expr)
        logger.log_entry(tool="alter_table_safe", status="preview", operation_type="ALTER", sql=ddl)
        return preview
    started = time.perf_counter()
    try:
        with connection.get_db_connection(autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
        schema_mod.refresh_schema()
        cache = schema_mod.get_cached_schema() or {}
        tables = val.tables_referenced(expr)
        table = tables[0] if tables else None
        cols = []
        if table and table in (cache.get("tables") or {}):
            cols = [c["name"] for c in cache["tables"][table]["columns"]]
        ms = round((time.perf_counter() - started) * 1000, 2)
        preview = _alter_preview(ddl, expr)
        result = {
            "status": "executed",
            "operation": preview["operation"],
            "table": table,
            "ddl_run": ddl,
            "schema_refreshed": True,
            "new_column_list": cols,
            "logged": True,
            "execution_time_ms": ms,
        }
        logger.log_entry(
            tool="alter_table_safe",
            status="success",
            operation_type="ALTER",
            sql=ddl,
            execution_time_ms=ms,
        )
        return result
    except psycopg2.Error as exc:
        translated = val.translate_error(exc)
        logger.log_entry(tool="alter_table_safe", status="error", operation_type="ALTER", sql=ddl, error=translated["raw_error"])
        return {"status": "error", **translated}


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
