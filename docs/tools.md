# Tool spec

This page is the contract for **sql-copilot**. A tool call is a named function with arguments. The server returns one JSON object. Cursor starts the process over **stdio** (standard input and output). There is no HTTP port.

Source of the names: `src/sql_mcp/tools/inspect.py` and `src/sql_mcp/tools/execute.py`.

## Clone and test

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and a local PostgreSQL database.

The query examples use the open-source [DVD Rental](https://neon.com/postgresql/getting-started/sample-database) sample (15 public tables such as `film`, `actor`, and `rental`). Download the dump:

https://neon.com/postgresqltutorial/dvdrental.zip

Unzip it to get `dvdrental.tar`, create an empty database, and restore it:

```text
psql -U postgres -c "CREATE DATABASE dvdrental"
pg_restore -U postgres -d dvdrental dvdrental.tar
```

Then point this server at that database. The password below is yours, not a secret from this repo:

```text
DB_URL=postgresql://postgres:password@localhost:5432/dvdrental
DB_NAME=dvdrental
```

Any database name is fine if `DB_URL` and `DB_NAME` use the same one. A smaller local copy of this schema works too. The full sample has 1000 rows in `film`.

```text
uv sync
copy .env.example .env
```

Set `DB_URL` in `.env` as above. Copy `.cursor/mcp.example.json` to `.cursor/mcp.json`, then set `cwd` to the folder where you cloned this repo. `PYTHONPATH` must stay `src`.

Reload the MCP server in Cursor, then check three calls:

1. `ping_connection` returns `"status": "connected"`.
2. `run_select` with `SELECT film_id, title FROM film` returns at most 10 rows and a `sql_to_run_locally` string.
3. `run_mutation` with `DELETE FROM some_table` returns `"sql_not_executed": true`. The row is still there.

## Shared rules

- One SQL statement per call. A second statement in the same string is rejected.
- Names are checked against the in-memory public schema before a `SELECT` preview runs.
- `OFFSET` is removed from SQL the server builds. The preview does not skip rows.
- `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `DROP`, `CREATE`, and `TRUNCATE` are not executed. The message is: "DDL and DML auto-execute is disabled."
- A `SELECT INTO` and a `SELECT` that hides `INSERT`, `UPDATE`, or `DELETE` inside `WITH` are treated as writes and are not executed.
- `UPDATE` or `DELETE` with no `WHERE`, or with `WHERE true` or `WHERE 1=1`, is warned about. It is still not executed.
- Postgres errors on a real `SELECT` include the raw message and a short explanation.

## Current limits

- Write commands are refused, including common writes hidden inside a `SELECT`. Some PostgreSQL functions can still have side effects. Use a database account with read-only access.
- Schema checks use cached names and may miss mistakes involving table aliases or unclear column names.
- Automated tests are a possible next step. The clone guide currently has manual checks instead.

## Inspect

These tools read the catalog or the cache. They do not run your SQL against table data.

### `ping_connection`

No arguments. Runs `SELECT version()`.

```json
{
  "status": "connected",
  "db_name": "dvd_rental_mcp_exp",
  "postgres_version": "PostgreSQL 18...",
  "host": "localhost",
  "port": 5432
}
```

Failure: `{"status": "error", "message": "..."}`.

### `get_schema`

No arguments. Returns the cache loaded at startup. It does not query the catalog again.

Top-level fields: `database`, `tables`, `cached_at`. Each table has `columns` (`name`, `type`, `nullable`, `primary_key`) and `foreign_keys` (`column`, `references_table`, `references_column`). Only `public` base tables are included.

### `get_table_info`

| Argument | Required | Meaning |
| --- | --- | --- |
| `table_name` | yes | Exact public table name |

Returns `table`, `columns`, `foreign_keys`, `approximate_row_count` (planner estimate from `pg_class.reltuples`, not `COUNT(*)`), and `indexes`. An unknown name returns `status: error` and `available_tables`.

### `prepare_query_context`

| Argument | Required | Meaning |
| --- | --- | --- |
| `intent` | yes | English description of the question |

Keyword match against cached table and column names. No language model inside the tool. The result has `likely_relevant_tables`, `schema_context`, `join_paths`, and a `note` that this tool does not write SQL. If nothing matches, `schema_context` is the full public schema and `likely_relevant_tables` is empty.

## Execute

### `run_select`

| Argument | Required | Meaning |
| --- | --- | --- |
| `sql` | yes | One `SELECT` statement |

The only tool that runs your SQL. It returns at most **10** rows (`PREVIEW_ROW_LIMIT` in `validator.py`). A larger `LIMIT`, or no `LIMIT`, is lowered to 10 before execution. A smaller `LIMIT` is kept.

```json
{
  "status": "success",
  "rows_returned": 10,
  "preview_row_limit": 10,
  "preview_capped": true,
  "columns": ["film_id", "title"],
  "rows": [{"film_id": 1, "title": "ACADEMY DINOSAUR"}],
  "sql_to_run_locally": "SELECT film_id, title FROM film LIMIT 200",
  "local_limit_added": true,
  "execution_time_ms": 12.0,
  "message": "Showing at most 10 rows so the agent context stays small. Run sql_to_run_locally in pgAdmin for the larger result."
}
```

`sql_to_run_locally` is the statement to paste into pgAdmin. If you wrote no `LIMIT`, that copy gets `LIMIT 200`. If you wrote a `LIMIT`, that copy keeps it. `OFFSET` is not included.

A non-`SELECT`, `SELECT INTO`, or a `SELECT` with a hidden write returns `status: auto_execute_disabled` and `sql_not_executed: true`. Unknown tables or columns return `status: validation_error` and do not run.

### `validate_query`

| Argument | Required | Meaning |
| --- | --- | --- |
| `sql` | yes | Any one statement |

Does not run the SQL. Returns `operation_type`, `syntax_valid`, `schema_valid`, `tables_referenced`, `columns_referenced`, `warnings`, `safe_to_auto_execute`, `explanation`, and `expected_tool`. `safe_to_auto_execute` is true only for a normal `SELECT` whose names match the cache.

### `dry_run`

| Argument | Required | Meaning |
| --- | --- | --- |
| `sql` | yes | A write you might run later |

Does not open a transaction and does not roll anything back. Same refusal shape as `run_mutation`.

### `run_mutation`

| Argument | Required | Meaning |
| --- | --- | --- |
| `sql` | yes | `INSERT`, `UPDATE`, or `DELETE` |
| `reason` | no | Stored in the audit log only. It does not run the SQL |

```json
{
  "status": "auto_execute_disabled",
  "operation_type": "DELETE",
  "sql": "DELETE FROM film",
  "sql_not_executed": true,
  "message": "DDL and DML auto-execute is disabled. Copy the SQL into pgAdmin or your own SQL client and run it there.",
  "warnings": ["DDL and DML auto-execute is disabled. Copy the SQL into pgAdmin or your own SQL client and run it there."]
}
```

A missing `WHERE`, or `WHERE true` / `WHERE 1=1`, is added to `warnings`.

### `alter_table_safe`

| Argument | Required | Meaning |
| --- | --- | --- |
| `ddl` | yes | An `ALTER` statement |

Does not run the DDL. For an `ALTER` it also returns `change_description` and `data_at_risk` from a text preview. The schema cache is not changed.

### `refresh_schema`

No arguments. Re-reads `information_schema` into the cache. Use this after you change tables outside this server. Returns `status: refreshed`, `tables_found`, `cached_at`, and the new `schema`.

### `get_logs`

| Argument | Required | Meaning |
| --- | --- | --- |
| `last_n` | no | How many newest lines to return. Default 50 |
| `operation_type` | no | Keep only that type, such as `SELECT` |

Reads `LOG_PATH` (default `logs/sql_mcp.jsonl`). `SELECT` log lines store the row count, not the row data.

### `get_status`

No arguments. Returns `connection` (`healthy` or `unhealthy`), `database`, `mode`, `schema_cache_age_seconds`, `tables_in_cache`, `log_entries_today`, `last_mutation`, and `warnings`.
