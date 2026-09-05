"""
Append-only JSON Lines audit log for MCP tool calls.

Phase 3 will import log_entry() without the MCP server running, so this
module depends only on the stdlib and LOG_PATH from the environment
(or the default logs/sql_mcp.jsonl).

Rules
-----
- Every tool call is logged, including validation failures.
- SELECT payloads are never written — only row counts.
- Mutations log the SQL string and rows_affected.
- The directory is created on first write.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_path() -> Path:
    """Resolve LOG_PATH relative to the current working directory (repo root when Cursor launches uv)."""
    raw = os.environ.get("LOG_PATH", "logs/sql_mcp.jsonl")
    return Path(raw)


def log_entry(
    *,
    tool: str,
    status: str,
    operation_type: str | None = None,
    sql: str | None = None,
    rows_affected: int | None = None,
    execution_time_ms: float | None = None,
    error: str | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Append one JSON object as a single line.

    extra is merged last so callers can add mode=prod without changing
    the positional shape.
    """
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "operation_type": operation_type,
        "sql": sql,
        "status": status,
        "rows_affected": rows_affected,
        "execution_time_ms": execution_time_ms,
        "error": error,
        "reason": reason,
    }
    if extra:
        payload.update(extra)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")


def read_entries(
    last_n: int = 50,
    operation_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return the newest matching lines from the jsonl file.

    The file is read from disk each call (small learning logs). If the
    file does not exist yet, return an empty list.
    """
    path = log_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if operation_type and item.get("operation_type") != operation_type:
                continue
            rows.append(item)
    return rows[-last_n:]


def count_entries_today() -> int:
    """Count log lines whose UTC timestamp starts with today's date."""
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for item in read_entries(last_n=10_000):
        ts = str(item.get("timestamp") or "")
        if ts.startswith(today):
            n += 1
    return n


def last_mutation() -> dict[str, Any] | None:
    """Newest log row whose operation is INSERT, UPDATE, or DELETE."""
    for item in reversed(read_entries(last_n=10_000)):
        if item.get("operation_type") in {"INSERT", "UPDATE", "DELETE"}:
            return {
                "timestamp": item.get("timestamp"),
                "operation": item.get("operation_type"),
                "rows_affected": item.get("rows_affected"),
            }
    return None
