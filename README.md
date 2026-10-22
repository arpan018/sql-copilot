# sql-copilot

MCP server for schema-aware PostgreSQL access from Cursor. The agent inspects the live schema and can run a short SELECT preview. DDL and DML are returned as SQL for a person to run locally. They are not executed by the server.

Cursor launches it over stdio (`uv run python -m sql_mcp.server`). There is no HTTP port.

## Tools

**Inspect** (`src/sql_mcp/tools/inspect.py`)

- `ping_connection` — check that PostgreSQL is reachable
- `get_schema` — cached public schema
- `get_table_info` — one table, row estimate, and indexes
- `prepare_query_context` — tables and join paths for an English intent. This tool does not emit SQL.

**Execute** (`src/sql_mcp/tools/execute.py`)

- `run_select` — SELECT only. Returns at most 10 rows (a preview). Also returns `sql_to_run_locally` for pgAdmin. If you omitted `LIMIT`, that copy includes `LIMIT 200`.
- `validate_query` — classify a statement without running it
- `dry_run`, `run_mutation`, `alter_table_safe` — do not run the SQL. They return the statement and say auto-execute is disabled.
- `refresh_schema`, `get_logs`, `get_status`

INSERT, UPDATE, DELETE, ALTER, DROP, CREATE, and TRUNCATE are not executed.

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
