"""
Inspect tools: connect, read schema, narrow context for SQL writing.

These functions are registered onto a FastMCP instance via register(mcp).
This module never imports sql_mcp.server (that would be a circular import).

The agent — not these tools — writes SELECT SQL. prepare_query_context
only returns matching tables, columns, and join paths from the cache.
Nothing here executes user SQL against table data.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import psycopg2

from sql_mcp.connection import get_db_connection
from sql_mcp import schema as schema_mod

# Words that appear in English intents but are not table/column names.
_STOP_WORDS = {
    "a",
    "an",
    "the",
    "find",
    "all",
    "who",
    "which",
    "that",
    "with",
    "from",
    "for",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "by",
    "more",
    "than",
    "less",
    "give",
    "me",
    "query",
    "select",
    "show",
    "list",
    "get",
    "how",
    "many",
    "where",
    "their",
    "they",
    "have",
    "has",
    "was",
    "were",
    "been",
    "being",
    "is",
    "are",
    "be",
}


def register(mcp) -> None:
    """
    Attach every inspect tool to the given FastMCP instance.

    Called once from server.py at startup. execute.register(mcp) is
    separate so this file stays limited to schema inspection.
    """

    @mcp.tool
    def ping_connection() -> dict:
        """Check that PostgreSQL is reachable and return basic info."""
        return _ping_connection()

    @mcp.tool
    def get_schema() -> dict:
        """Return the full cached database schema."""
        return _get_schema()

    @mcp.tool
    def get_table_info(table_name: str) -> dict:
        """Return focused schema detail and row count for one table."""
        return _get_table_info(table_name)

    @mcp.tool
    def prepare_query_context(intent: str) -> dict:
        """Narrow the schema to tables/columns relevant to a stated intent."""
        return _prepare_query_context(intent)


def _clean_error(exc: BaseException) -> str:
    """One-line message for the agent; no Python traceback."""
    return str(exc).strip().split("\n")[0]


def _parse_db_url() -> tuple[str, int]:
    """Host and port from DB_URL for ping_connection metadata."""
    parsed = urlparse(os.environ.get("DB_URL", ""))
    host = parsed.hostname or "unknown"
    port = parsed.port or 5432
    return host, port


def _ping_connection() -> dict:
    """
    SELECT version() on a pooled connection.

    Success JSON is a short connectivity summary. Failures return status=error
    so the agent can tell the user the database is down.
    """
    host, port = _parse_db_url()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version_row = cur.fetchone()
        return {
            "status": "connected",
            "db_name": os.environ.get("DB_NAME", "unknown"),
            "postgres_version": version_row[0] if version_row else "unknown",
            "host": host,
            "port": port,
        }
    except psycopg2.Error as exc:
        return {"status": "error", "message": _clean_error(exc)}
    except Exception as exc:
        return {"status": "error", "message": _clean_error(exc)}


def _get_schema() -> dict:
    """Serve the startup cache; do not hit the database again."""
    cached = schema_mod.get_cached_schema()
    if cached is None:
        return {
            "status": "error",
            "message": "Schema cache is empty. Restart the MCP server.",
        }
    return cached


def _get_table_info(table_name: str) -> dict:
    """One table from the cache plus live catalog row-count and indexes."""
    try:
        return schema_mod.get_table_detail(table_name)
    except psycopg2.Error as exc:
        return {"status": "error", "message": _clean_error(exc)}
    except Exception as exc:
        return {"status": "error", "message": _clean_error(exc)}


def _prepare_query_context(intent: str) -> dict:
    """
    Deterministic keyword match against cached table and column names.

    No LLM call. Lowercase the intent, split on non-letters, drop stop
    words, then substring-match remaining tokens against identifiers.
    Foreign keys whose both ends are in the matched table set become
    join_paths.
    """
    cached = schema_mod.get_cached_schema()
    if cached is None:
        return {
            "status": "error",
            "message": "Schema cache is empty. Restart the MCP server.",
        }

    tables: dict[str, Any] = cached.get("tables") or {}
    tokens = [
        t
        for t in re.findall(r"[a-z0-9_]+", intent.lower())
        if t not in _STOP_WORDS and len(t) > 1
    ]

    matched: set[str] = set()
    for table_name, info in tables.items():
        t_lower = table_name.lower()
        col_names = [c["name"].lower() for c in info.get("columns") or []]
        for token in tokens:
            if any(
                v in t_lower or t_lower in v
                for v in _token_variants(token)
            ):
                matched.add(table_name)
                break
            if any(
                v in col or col in v
                for v in _token_variants(token)
                for col in col_names
            ):
                matched.add(table_name)
                break

    # If nothing matched, hand back the full table list so the agent can
    # still write SQL instead of inventing names.
    if not matched:
        schema_context = {
            name: {"columns": [{"name": c["name"], "type": c["type"]} for c in info["columns"]]}
            for name, info in tables.items()
        }
        return {
            "intent": intent,
            "likely_relevant_tables": [],
            "schema_context": schema_context,
            "join_paths": [],
            "note": (
                "No tables matched the intent keywords. Full public schema "
                "included. No SQL is generated by this tool. Use this context "
                "to write the query yourself, in-chat."
            ),
        }

    schema_context = {}
    join_paths = []
    for name in sorted(matched):
        info = tables[name]
        schema_context[name] = {
            "columns": [{"name": c["name"], "type": c["type"]} for c in info["columns"]]
        }
        for fk in info.get("foreign_keys") or []:
            ref = fk["references_table"]
            if ref in matched:
                join_paths.append(
                    {
                        "from_table": name,
                        "from_column": fk["column"],
                        "to_table": ref,
                        "to_column": fk["references_column"],
                    }
                )

    return {
        "intent": intent,
        "likely_relevant_tables": sorted(matched),
        "schema_context": schema_context,
        "join_paths": join_paths,
        "note": (
            "No SQL is generated by this tool. Use this context to write "
            "the query yourself, in-chat."
        ),
    }


def _token_variants(token: str) -> list[str]:
    """
    Tiny English stems so 'rented' still hits table 'rental'.

    Still deterministic string matching — not an NLP library.
    """
    variants = {token}
    if len(token) > 4 and token.endswith("ed"):
        variants.add(token[:-2])
        variants.add(token[:-1])
    if len(token) > 5 and token.endswith("ing"):
        variants.add(token[:-3])
    if len(token) > 3 and token.endswith("s"):
        variants.add(token[:-1])
    return list(variants)
