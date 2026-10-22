"""
In-memory PostgreSQL schema cache built from information_schema.

Why cache
---------
get_schema is called often by the agent. Re-querying catalogs on every
chat turn is slow and noisy. We load once at server startup
(refresh_schema) and serve the dict. Successful DDL calls
refresh_schema() again so the cache matches the new columns.

The cache is a module-level dict, not only an MCP tool return value.
validator.py imports get_cached_schema() without a DB round-trip.

Scope
-----
Only table_schema = 'public'. The learning database keeps application
tables there.

Row counts / indexes for get_table_info live in this module too:
- approximate_row_count uses pg_class.reltuples (planner estimate, not COUNT(*))
- indexes come from pg_indexes for schema public

Inputs / outputs
----------------
refresh_schema() -> dict: full schema JSON-serialisable structure,
    also stored in _schema_cache.
get_cached_schema() -> dict | None
get_table_detail(table_name) -> dict for one table or a not-found error dict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sql_mcp.connection import get_db_connection

# Full schema as returned by get_schema. None before the first refresh.
_schema_cache: dict[str, Any] | None = None
_cached_at: datetime | None = None


def get_cached_schema() -> dict[str, Any] | None:
    """Return the in-memory schema dict, or None if not yet loaded."""
    return _schema_cache


def get_cached_at() -> datetime | None:
    """UTC timestamp of the last successful refresh_schema() call."""
    return _cached_at


def cache_age_seconds() -> float | None:
    """Seconds since last refresh, or None if the cache is empty."""
    if _cached_at is None:
        return None
    return (datetime.now(timezone.utc) - _cached_at).total_seconds()


def refresh_schema() -> dict[str, Any]:
    """
    Read public tables, columns, PKs, and FKs and replace the cache.

    Returns the same structure get_schema serves to the agent.
    """
    global _schema_cache, _cached_at

    with get_db_connection() as conn:
        tables = _list_public_tables(conn)
        table_map: dict[str, Any] = {}
        for table_name in tables:
            table_map[table_name] = {
                "columns": _list_columns(conn, table_name),
                "foreign_keys": _list_foreign_keys(conn, table_name),
            }

    cached_at = datetime.now(timezone.utc)
    from os import environ

    payload = {
        "database": environ.get("DB_NAME", "unknown"),
        "tables": table_map,
        "cached_at": cached_at.replace(microsecond=0).isoformat(),
    }
    _schema_cache = payload
    _cached_at = cached_at
    return payload


def get_table_detail(table_name: str) -> dict[str, Any]:
    """
    Focused view of one table: columns, FKs, approx row count, index names.

    table_name is matched case-sensitively against information_schema
    (dvd_rental uses lowercase unquoted names).
    """
    cache = _schema_cache
    if cache is None:
        return {
            "status": "error",
            "message": "Schema cache is empty. The server has not finished startup.",
        }

    tables = cache.get("tables") or {}
    if table_name not in tables:
        return {
            "status": "error",
            "message": f"Unknown table '{table_name}'. Call get_schema to list tables.",
            "available_tables": sorted(tables.keys()),
        }

    entry = tables[table_name]
    with get_db_connection() as conn:
        row_count = _approximate_row_count(conn, table_name)
        indexes = _list_index_names(conn, table_name)

    return {
        "table": table_name,
        "columns": entry["columns"],
        "foreign_keys": entry["foreign_keys"],
        "approximate_row_count": row_count,
        "indexes": indexes,
    }


def _list_public_tables(conn) -> list[str]:
    """BASE TABLEs in public, excluding views."""
    sql = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return [row[0] for row in cur.fetchall()]


def _list_columns(conn, table_name: str) -> list[dict[str, Any]]:
    """
    Columns plus primary-key flags.

    data_type is the information_schema type name (character varying,
    integer, ...). is_nullable is YES/NO in the catalog; we expose bool.
    """
    pk_columns = set(_primary_key_columns(conn, table_name))
    sql = """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table_name,))
        rows = cur.fetchall()

    result = []
    for name, data_type, is_nullable in rows:
        result.append(
            {
                "name": name,
                "type": data_type,
                "nullable": is_nullable == "YES",
                "primary_key": name in pk_columns,
            }
        )
    return result


def _primary_key_columns(conn, table_name: str) -> list[str]:
    sql = """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
         AND tc.table_name = kcu.table_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = 'public'
          AND tc.table_name = %s
        ORDER BY kcu.ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table_name,))
        return [row[0] for row in cur.fetchall()]


def _list_foreign_keys(conn, table_name: str) -> list[dict[str, str]]:
    """
    Foreign keys using referential_constraints + key_column_usage.

    constraint_column_usage alone is easy to get wrong on composite FKs.
    This join is still standard information_schema SQL.
    """
    sql = """
        SELECT
            kcu.column_name,
            ccu.table_name AS references_table,
            ccu.column_name AS references_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_schema = kcu.constraint_schema
         AND tc.constraint_name = kcu.constraint_name
        JOIN information_schema.referential_constraints rc
          ON tc.constraint_schema = rc.constraint_schema
         AND tc.constraint_name = rc.constraint_name
        JOIN information_schema.key_column_usage ccu
          ON rc.unique_constraint_schema = ccu.constraint_schema
         AND rc.unique_constraint_name = ccu.constraint_name
         AND kcu.ordinal_position = ccu.ordinal_position
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND tc.table_name = %s
        ORDER BY tc.constraint_name, kcu.ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table_name,))
        rows = cur.fetchall()

    return [
        {
            "column": col,
            "references_table": ref_table,
            "references_column": ref_col,
        }
        for col, ref_table, ref_col in rows
    ]


def _approximate_row_count(conn, table_name: str) -> int:
    """
    Planner estimate from pg_class.reltuples.

    This is catalog metadata, not a table scan. ANALYZE-stale values are
    still useful so the agent knows a table is large vs tiny.
    """
    sql = """
        SELECT COALESCE(c.reltuples, 0)::bigint
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = %s
          AND c.relkind = 'r'
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table_name,))
        row = cur.fetchone()
    if not row:
        return 0
    return int(row[0])


def _list_index_names(conn, table_name: str) -> list[str]:
    sql = """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = %s
        ORDER BY indexname
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table_name,))
        return [row[0] for row in cur.fetchall()]
