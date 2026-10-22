# sql-copilot

MCP server for schema-aware PostgreSQL access from Cursor. The agent inspects the live schema, then runs SQL through tools that parse, limit, confirm, and log each call.

Cursor launches it over stdio (`uv run python -m sql_mcp.server`). There is no HTTP port.

## Tools

**Inspect** (`src/sql_mcp/tools/inspect.py`)

- `ping_connection` — check that PostgreSQL is reachable
- `get_schema` — cached public schema
- `get_table_info` — one table, row estimate, and indexes
- `prepare_query_context` — tables and join paths for an English intent. This tool does not emit SQL.

**Execute** (`src/sql_mcp/tools/execute.py`)

- `run_select` — SELECT only. Injects `LIMIT 200` when the statement has no LIMIT.
- `validate_query` — classify a statement without running it
- `dry_run` — INSERT, UPDATE, or DELETE, then roll back
- `run_mutation` — write only when `confirmed` is true
- `alter_table_safe` — preview an ALTER, then run it and refresh the schema cache
- `refresh_schema`, `get_logs`, `get_status`

DROP, CREATE, and TRUNCATE are blocked. `MODE=prod` blocks ALTER, and blocks UPDATE or DELETE that has no WHERE clause.

## Setup

```text
uv sync
copy .env.example .env
```

Set `DB_URL` in `.env`. Cursor picks up the server from `.cursor/mcp.json` (`PYTHONPATH=src`).

## How it was built

Phase 1 was inspection only: a connection pool, an in-memory public-schema cache, and the four inspect tools. That code lives in `connection.py`, `schema.py`, and `tools/inspect.py`.

Phase 2 added execution with a safety net: sqlglot checks, dry-run rollback, explicit confirmation, a JSONL audit log, and plain-English Postgres errors. That code lives in `validator.py`, `logger.py`, and `tools/execute.py`.

The server registers both modules at startup. They share one pool so a mutation cannot leave a connection mid-transaction for the next call.
