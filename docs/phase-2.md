# Phase 2 — Validate, Execute, Log, Debug

## Problem Statement

Phase 1 gave the agent eyes — it can see the schema and generate queries.
But generated queries still have to be copy-pasted into psql or pgAdmin,
run manually, and any errors decoded by the developer alone. For someone
learning SQL, this is still a slow and opaque loop.

The problems Phase 2 solves:

**Problem 1 — No safety net on execution.**
Nothing stops a badly formed query from running. A missing WHERE clause on
a DELETE wipes every row. An ALTER TABLE drops a column with data in it.
There is no confirmation step, no preview, no undo awareness.

**Problem 2 — Errors are raw and unhelpful.**
`ERROR: insert or update on table "orders" violates foreign key constraint
"orders_user_id_fkey"` is technically correct but tells a learning developer
nothing actionable. What does it mean? Which row? What should they do?

**Problem 3 — No record of what happened.**
After a session of experimenting with SQL, there is no log of what ran,
what failed, what changed, or when. Reproducing a bug or reviewing what
you did yesterday is impossible.

**Problem 4 — Schema gets stale.**
After running an ALTER TABLE, the Phase 1 schema cache is wrong. The agent
will generate queries referencing old column names. There is no mechanism
to refresh without restarting the server.

Phase 2 fixes all four problems.

---

## Goals

1. Execute SELECT queries safely with row limits and result formatting.
2. Validate any SQL before execution using `sqlglot` — catch errors before
   they hit the database.
3. Block destructive queries at the tool level — SELECTs only through the
   read tool, no exceptions.
4. Provide dry-run mode for mutations — show what would change, then rollback.
5. Execute INSERT, UPDATE, DELETE through a separate tool with explicit
   confirmation required.
6. Guard ALTER TABLE with a diff preview and a two-step confirm flow.
7. Auto-refresh schema cache after any successful DDL operation.
8. Log every tool call — SQL, timestamp, result summary, error — to a JSON
   file on disk.
9. Return plain-English error explanations alongside raw Postgres errors.
10. Add a status tool that summarises recent activity and current DB state.

---

## What Phase 2 Inherits From Phase 1

Phase 2 does not rewrite Phase 1. It imports from it and extends it.

- `connection.py` — reused directly, unchanged. Every Phase 2 tool accesses
  the database through `connection.get_db_connection(autocommit=...)`, the
  same context manager Phase 1 built around the shared pool. Read-only
  tools (`run_select`) borrow with `autocommit=True`. Mutation tools
  (`run_mutation`, `dry_run`, `alter_table_safe`) borrow with
  `autocommit=False` and control `COMMIT` / `ROLLBACK` explicitly inside
  the `with` block, exactly as `connection.py`'s design in `phase-1.md`
  anticipated. **No changes to `connection.py` are needed for Phase 2.**
- `schema.py` — the in-memory cache dict is imported directly into the
  validator. `refresh_schema()` already exists from Phase 1; Phase 2 just
  calls it after successful DDL.
- `server.py` — Phase 2 is wired in by adding exactly two lines, per the
  reference skeleton in `phase-1.md`:
  ```python
  from sql_mcp.tools import phase2
  ...
  phase2.register(mcp)
  ```
  No other change to `server.py`.
- `tools/phase1.py` — untouched. All Phase 1 tools remain available.
  `tools/phase2.py` follows the same `register(mcp)` pattern as
  `tools/phase1.py` — a single function that attaches every Phase 2 tool
  to the passed-in `mcp` instance.

If Phase 1's connection pool is unstable or leaks connections, Phase 2 will
amplify that problem because it executes real queries. Fix Phase 1 fully —
specifically, confirm the pool + context manager pattern is solid — before
starting Phase 2.

---

## `.env` Additions for Phase 2

This extends the existing `.env` from Phase 1 — it does not replace it.
Add this one line to the same file:

```
# Path to the audit log file, created automatically if it doesn't exist
LOG_PATH=logs/sql_mcp.jsonl
```

`MODE` already exists in `.env` from Phase 1 (`dev` or `prod`) but Phase 1
never reads it. Phase 2 is where it starts mattering — see the next
section.

---

## What `MODE` Actually Gates

Phase 1 declared `MODE=dev|prod` in `.env` as a placeholder for future use.
Phase 2 is that future use. This is what makes the difference between a
local learning database and a database you'd never want an AI agent
mutating without extreme caution.

**When `MODE=dev` (the default for a local learning database):**
- All Phase 2 tools behave exactly as documented below — `dry_run`,
  `run_mutation` with `confirmed: true`, `alter_table_safe` with its
  two-step confirm flow, all work normally.

**When `MODE=prod`:**
- `alter_table_safe` is rejected outright, at the validation step, before
  any DB interaction. Returns:
  ```json
  {
    "status": "blocked",
    "reason": "MODE=prod blocks all ALTER TABLE operations through this MCP server. Run schema migrations through your normal migration tooling instead."
  }
  ```
- `run_mutation` on an UPDATE or DELETE with no WHERE clause is rejected
  even if `confirmed: true` and `override_no_where: true` are both set —
  the prod override does not exist. This specific combination is always
  blocked in `MODE=prod`, no exceptions.
- Every other `run_mutation` call is allowed but logs an extra
  `"mode": "prod"` field so the audit trail makes prod mutations easy to
  filter and review later.

**Where this check lives in code:** `validator.py` reads `MODE` from the
environment once at import time and exposes a `is_prod() -> bool` helper.
Every tool in `phase2.py` that touches the database calls this helper as
its first check, before `sqlglot` parsing even runs.

**Why this matters for the portfolio story:** this is the "prod vs dev
connection guard" — a real differentiator over a plain ORM, and it costs
almost nothing to implement since the gating logic is a handful of `if`
statements layered on top of validation you're already building.

---

## New Dependency: `sqlglot`

`sqlglot` is a pure Python SQL parser and transpiler. In Phase 2 it is used
to parse SQL strings before they reach the database.

What it gives us:
- Identify the statement type (SELECT, INSERT, UPDATE, DELETE, ALTER, DROP)
  without running the query
- Check syntax validity without a DB round-trip
- Extract table and column references to validate against the schema cache
- Detect dangerous patterns (SELECT *, DELETE without WHERE, DROP TABLE)

Install it now if not already in `pyproject.toml`:
```
sqlglot>=23.0.0
```

`sqlglot` does not execute SQL. It only parses the string. It is safe to
call on any input.

---

## Technical Stack Additions (Phase 2)

| Component | Library | Why |
|-----------|---------|-----|
| SQL parser | `sqlglot` | Syntax check and operation type detection without DB round-trip |
| Structured logging | Python `logging` + `json` stdlib | No extra dependency — log to rotating JSON file |
| Transaction control | psycopg2 connection API | `autocommit=False` + explicit commit/rollback for mutation tools |

No new major dependencies beyond `sqlglot`. Everything else is stdlib or
already installed from Phase 1.

---

## Project Structure Changes

```
sql-mcp/
├── logs/                       # NEW — created automatically on first run
│   └── sql_mcp.jsonl           # Append-only JSON Lines log file
├── src/
│   └── sql_mcp/
│       ├── server.py           # UPDATED — registers phase2 tools alongside phase1
│       ├── connection.py       # UNCHANGED from Phase 1
│       ├── schema.py           # UPDATED — add refresh_schema() function
│       ├── validator.py        # NEW — sqlglot-based query validation logic
│       ├── logger.py           # NEW — JSON Lines audit log writer
│       └── tools/
│           ├── phase1.py       # UNCHANGED
│           └── phase2.py       # NEW — all Phase 2 tool implementations
```

**Important:** Every new `.py` file must have detailed inline comments.
`validator.py` and `logger.py` especially — they contain logic that is not
obvious and will be read again during debugging.

---

## Core Concept: Operation Classification

Before any query reaches the DB, it must be classified. The classifier
lives in `validator.py` and uses `sqlglot` to parse the statement.

```
SQL string
    ↓
sqlglot.parse_one(sql)
    ↓
Identify statement type:
    SELECT      → read-only path (run_select tool)
    INSERT      → mutation path (run_mutation tool, confirmation required)
    UPDATE      → mutation path (run_mutation tool, confirmation required)
    DELETE      → mutation path (run_mutation tool, confirmation required)
    ALTER TABLE → DDL path (alter_table_safe tool, diff + confirmation)
    DROP        → always blocked, no tool accepts this
    CREATE      → blocked in Phase 2, Phase 3 may handle it
    TRUNCATE    → always blocked
    anything else → blocked with explanation
```

This classification runs first on every tool call, before any DB interaction.
A tool that receives the wrong operation type for its purpose returns an
error immediately — it never reaches `connection.get_db_connection()`.

---

## Tools to Implement

### `run_select`

**Purpose:** Execute a SELECT query and return results with metadata.

**Arguments:**
```json
{
  "sql": "SELECT id, email FROM users WHERE created_at > '2026-01-01' LIMIT 50;"
}
```

**Validation steps (in order, stop on first failure):**
1. Parse with `sqlglot` — syntax valid?
2. Operation type check — is it a SELECT? If not, reject with message
   explaining which tool to use instead.
3. Schema validation — do all referenced tables and columns exist in the
   cache? If not, list the unknown identifiers.
4. Row limit check — does the query have a LIMIT clause? If not, inject
   `LIMIT 200` automatically and warn the user that a limit was added.
5. Execute against DB.

**Returns on success:**
```json
{
  "status": "success",
  "row_count": 42,
  "rows_returned": 42,
  "limit_applied": false,
  "columns": ["id", "email"],
  "rows": [
    {"id": 1, "email": "user@example.com"},
    {"id": 2, "email": "other@example.com"}
  ],
  "execution_time_ms": 12
}
```

**Returns on limit injection:**
```json
{
  "status": "success",
  "warning": "No LIMIT clause found. LIMIT 200 was applied automatically to prevent large result sets.",
  "row_count": 200,
  ...
}
```

**Returns on schema validation failure:**
```json
{
  "status": "validation_error",
  "error": "Unknown column references: users.nonexistent_column",
  "suggestion": "Available columns on 'users': id, email, created_at",
  "sql_not_executed": true
}
```

**Why LIMIT injection matters:** A SELECT without LIMIT on a table with
100,000 rows returns all 100,000 rows through your MCP server into the
agent's context window. That will crash the response. Always cap it.

---

### `validate_query`

**Purpose:** Analyse any SQL string and report what it would do without
executing it. Safe to call on any input including destructive SQL.

**Arguments:**
```json
{
  "sql": "DELETE FROM users;"
}
```

**Returns:**
```json
{
  "operation_type": "DELETE",
  "syntax_valid": true,
  "schema_valid": true,
  "tables_referenced": ["users"],
  "columns_referenced": [],
  "warnings": [
    "DELETE without WHERE clause — this will delete ALL rows in 'users' (approximately 1,042 rows)",
    "This is a destructive operation. Use run_mutation with confirmed=true to execute."
  ],
  "safe_to_auto_execute": false,
  "explanation": "This DELETE has no WHERE filter. Every row in the users table would be removed permanently."
}
```

**Key behaviour:** This tool never executes SQL. It only analyses. It can
safely receive DROP TABLE, TRUNCATE, or any other dangerous statement and
will describe what it would do without doing it. This is useful for the
agent when a user asks "what does this query do?" without wanting to run it.

---

### `dry_run`

**Purpose:** Execute a mutation (INSERT, UPDATE, DELETE) inside a transaction,
capture what changed, then roll back. Shows the real effect without
persisting it.

**Arguments:**
```json
{
  "sql": "UPDATE orders SET status = 'cancelled' WHERE created_at < '2025-01-01';"
}
```

**How it works internally:**
1. Validate operation type — must be INSERT, UPDATE, or DELETE.
2. Begin a transaction (`autocommit=False` on psycopg2 connection).
3. Execute the SQL.
4. Capture `cursor.rowcount` — how many rows were affected.
5. For UPDATE/DELETE: run a SELECT to show a sample of the affected rows
   before the rollback.
6. Always `ROLLBACK` — nothing is persisted.
7. Return the preview.

**Returns:**
```json
{
  "status": "dry_run_complete",
  "operation": "UPDATE",
  "rows_that_would_be_affected": 156,
  "sample_affected_rows": [
    {"id": 3, "status": "pending", "created_at": "2024-11-15"},
    {"id": 7, "status": "pending", "created_at": "2024-08-03"}
  ],
  "sample_size": 5,
  "rolled_back": true,
  "to_execute_for_real": "Call run_mutation with this SQL and confirmed=true"
}
```

**Why this is important for learning:** Seeing "156 rows would be affected"
before committing is one of the most useful safeguards in SQL. This makes
that experience native to every mutation.

---

### `run_mutation`

**Purpose:** Execute an INSERT, UPDATE, or DELETE for real. Requires an
explicit confirmed flag to prevent accidental execution.

**Arguments:**
```json
{
  "sql": "UPDATE orders SET status = 'cancelled' WHERE created_at < '2025-01-01';",
  "confirmed": true,
  "reason": "Cancelling all pre-2025 pending orders as discussed"
}
```

**Validation steps (in order):**
1. `confirmed` must be `true` — if missing or false, reject with a message
   telling the user to call `dry_run` first and then set confirmed.
2. Parse with `sqlglot` — syntax valid?
3. Operation type — must be INSERT, UPDATE, or DELETE. Not ALTER, not DROP.
4. Schema validation — tables and columns exist in cache?
5. Warning check — DELETE/UPDATE without WHERE clause requires `confirmed`
   AND a separate `override_no_where: true` flag. Two explicit flags for
   the most dangerous pattern.
6. Execute inside a transaction.
7. Commit on success, rollback on any error.
8. Log the operation.

**Returns on success:**
```json
{
  "status": "executed",
  "operation": "UPDATE",
  "rows_affected": 156,
  "committed": true,
  "logged": true,
  "timestamp": "2026-08-23T14:22:05"
}
```

**Returns when confirmed is missing:**
```json
{
  "status": "confirmation_required",
  "message": "This is a mutation operation. Run dry_run first to preview the effect, then call run_mutation with confirmed=true.",
  "sql_not_executed": true
}
```

**The `reason` field** is optional but logged. When reviewing the audit log
later, knowing *why* a mutation happened is as useful as knowing what it did.

---

### `alter_table_safe`

**Purpose:** Run an ALTER TABLE statement with a mandatory diff-and-confirm
flow. This is the highest-risk DDL operation because it changes the schema
and may be irreversible (e.g. dropping a column).

**Arguments (step 1 — preview):**
```json
{
  "ddl": "ALTER TABLE users DROP COLUMN phone_number;",
  "confirmed": false
}
```

**Returns (step 1 — diff preview):**
```json
{
  "status": "preview",
  "operation": "DROP COLUMN",
  "table": "users",
  "change_description": "Column 'phone_number' (varchar, nullable) will be permanently removed from table 'users'",
  "current_column_exists": true,
  "data_at_risk": "Any data stored in phone_number will be lost permanently. This cannot be undone.",
  "current_schema_snapshot": {
    "columns_before": ["id", "email", "phone_number", "created_at"]
  },
  "predicted_schema_after": {
    "columns_after": ["id", "email", "created_at"]
  },
  "to_proceed": "Call alter_table_safe again with the same DDL and confirmed=true"
}
```

**Arguments (step 2 — execution):**
```json
{
  "ddl": "ALTER TABLE users DROP COLUMN phone_number;",
  "confirmed": true
}
```

**Returns (step 2 — executed):**
```json
{
  "status": "executed",
  "operation": "DROP COLUMN",
  "table": "users",
  "ddl_run": "ALTER TABLE users DROP COLUMN phone_number;",
  "schema_refreshed": true,
  "new_column_list": ["id", "email", "created_at"],
  "logged": true
}
```

**Auto-refresh on success:** After any successful ALTER TABLE, `schema.py`'s
`refresh_schema()` is called automatically. The agent immediately has the
correct updated schema in its context for the next query it generates.

---

### `refresh_schema`

**Purpose:** Force a re-read of the entire schema from PostgreSQL and update
the in-memory cache. Returns the new schema.

**Arguments:** None

**When to call it:**
- After an ALTER TABLE (called automatically by `alter_table_safe`)
- If you ran DDL outside this MCP server (e.g. directly in psql)
- If you suspect the cache is stale

**Returns:**
```json
{
  "status": "refreshed",
  "previous_cache_age_seconds": 3420,
  "tables_found": 8,
  "cached_at": "2026-08-23T14:30:00",
  "schema": { ... }
}
```

---

### `get_logs`

**Purpose:** Return recent entries from the audit log so the agent can answer
questions like "what queries did I run today?" or "what changed in the last
hour?"

**Arguments:**
```json
{
  "last_n": 20,
  "operation_type": "DELETE"
}
```

Both arguments are optional. Default is last 50 entries, all operation types.

**Returns:**
```json
{
  "log_file": "logs/sql_mcp.jsonl",
  "entries_returned": 3,
  "entries": [
    {
      "timestamp": "2026-08-23T14:22:05",
      "tool": "run_mutation",
      "operation": "DELETE",
      "sql": "DELETE FROM sessions WHERE expires_at < NOW();",
      "rows_affected": 88,
      "status": "success",
      "reason": "cleanup expired sessions"
    }
  ]
}
```

---

### `get_status`

**Purpose:** Single-call health dashboard. Returns DB connection status,
schema cache age, recent activity summary, and any warnings.

**Arguments:** None

**Returns:**
```json
{
  "connection": "healthy",
  "database": "local-learning-db",
  "schema_cache_age_seconds": 142,
  "tables_in_cache": 8,
  "log_entries_today": 34,
  "last_mutation": {
    "timestamp": "2026-08-23T14:22:05",
    "operation": "UPDATE",
    "rows_affected": 156
  },
  "warnings": []
}
```

---

## The Logger Module (`logger.py`)

Every tool call is logged. The logger writes to a `.jsonl` file (JSON Lines
— one JSON object per line). This format is easy to read, easy to parse,
and appends safely without loading the whole file.

Each log entry contains:
```json
{
  "timestamp": "2026-08-23T14:22:05.123456",
  "tool": "run_mutation",
  "operation_type": "UPDATE",
  "sql": "UPDATE orders SET status = 'cancelled' WHERE ...",
  "status": "success",
  "rows_affected": 156,
  "execution_time_ms": 44,
  "error": null,
  "reason": "Cancelling old pending orders"
}
```

**Logger rules (enforced in `logger.py`):**
- Log file path comes from `.env` → `LOG_PATH=logs/sql_mcp.jsonl`
- Log directory is created automatically if it does not exist
- Every tool call is logged — including validation failures and rejections
- `SELECT` results are logged with row count only — never log the actual
  row data (could contain sensitive information)
- Mutations log the full SQL and rows affected
- Errors log the full exception message

---

## Error Translation Map

When `psycopg2` raises an exception, `validator.py` catches the error code
and appends a plain-English explanation. These are the most common ones a
learner will hit:

| Postgres Error Code | Raw Message (example) | Plain English Added |
|--------------------|-----------------------|--------------------|
| `23503` | foreign key constraint violation | "You're trying to modify or delete a row that other rows depend on. Check which rows in other tables reference this one first." |
| `23502` | null value in column violates not-null constraint | "This column requires a value — you cannot leave it empty (NULL). Provide a value for it." |
| `23505` | duplicate key value violates unique constraint | "A row with this value already exists. The column requires unique values across all rows." |
| `42703` | column does not exist | "The column name you used doesn't exist in this table. Call get_table_info to see available columns." |
| `42P01` | relation does not exist | "The table name you used doesn't exist. Call get_schema to see all available tables." |
| `42601` | syntax error | "The SQL has a syntax error. Check for missing commas, unmatched parentheses, or misspelled keywords." |
| `40001` | serialization failure | "Two operations conflicted. The transaction was rolled back. Try again." |
| `53300` | too many connections | "PostgreSQL has too many open connections. Restart the MCP server to reset the connection." |

**How it works in code:** `psycopg2` exceptions have a `.pgcode` attribute
containing the 5-character error code. Match against this dict in a
`translate_error()` function in `validator.py`. Always return both the
original error and the translation.

---

## Transaction Safety Model

Phase 2 uses explicit transaction control on all mutations, built directly
on the connection pool and `get_db_connection()` context manager from
`connection.py` (see `phase-1.md`) — no new connection machinery here.

**How a mutation tool borrows its connection:**
```python
with connection.get_db_connection(autocommit=False) as conn:
    # psycopg2 autocommit=False means every statement is automatically
    # wrapped in a transaction that must be explicitly committed or
    # rolled back — nothing persists until COMMIT runs.
    ...
```

Read-only tools (`run_select`, everything in Phase 1) borrow with the
default `autocommit=True` instead — no explicit transaction needed for a
SELECT.

**Phase 2 mutation flow:**
```
Tool call received
    ↓
Validate (sqlglot, schema check, MODE check) — no connection borrowed yet
    ↓
with connection.get_db_connection(autocommit=False) as conn:
    BEGIN (implicit — this is what autocommit=False means)
        ↓
    Execute SQL
        ↓
    Success? → conn.commit() → log success
    Failure? → conn.rollback() → log error → return translated error
    ↓
connection returned to the pool automatically when the `with` block exits
```

**Important for `dry_run`:** the rollback is unconditional — it happens
every time, success or failure, inside a `try/finally` (or simply by never
calling `conn.commit()` before the `with` block exits and the connection
goes back to the pool). Even if something unexpected happens during the
preview SELECT, nothing is persisted. The DB is never left in a partial
state, and the connection is never leaked back to the pool mid-transaction.

---

## Implementation Order

Build and test in this order. Phase 1 must be fully working before starting.

1. Add `sqlglot` to `pyproject.toml`, run `uv sync`
2. `logger.py` — implement `log_entry()`, test by writing a dummy entry
   and reading the `.jsonl` file manually
3. `validator.py` — implement `classify_operation()` using sqlglot,
   test with various SQL strings in a standalone script
4. `validator.py` — add `validate_against_schema()` using the Phase 1
   schema cache, test with known-valid and known-invalid column names
5. `validator.py` — add `translate_error()` with the error code map
6. `schema.py` — add `refresh_schema()` function
7. `tools/phase2.py` — implement `run_select` first, test in Cursor
8. `tools/phase2.py` — implement `validate_query`
9. `tools/phase2.py` — implement `dry_run`
10. `tools/phase2.py` — implement `run_mutation`
11. `tools/phase2.py` — implement `alter_table_safe`
12. `tools/phase2.py` — implement `refresh_schema` tool (wraps schema.py)
13. `tools/phase2.py` — implement `get_logs` and `get_status`
14. `server.py` — import and register all phase2 tools
15. End-to-end test in Cursor: SELECT → dry_run → mutation → ALTER TABLE
    → verify log has all entries → verify schema refreshed after ALTER

---

## Testing Checklist (Run In Cursor Agent)

These are the scenarios to walk through to confirm Phase 2 is working:

**SELECT safety:**
- [ ] Ask for a SELECT with no LIMIT — confirm LIMIT is injected and warned
- [ ] Ask for a SELECT referencing a column that doesn't exist — confirm
      schema validation catches it before execution
- [ ] Ask for a DELETE through `run_select` — confirm it is rejected

**Dry run:**
- [ ] Run `dry_run` on an UPDATE — confirm row count preview and rollback
- [ ] Check the DB after dry_run — confirm nothing changed

**Mutation confirmation:**
- [ ] Call `run_mutation` without `confirmed: true` — confirm rejection
- [ ] Call `run_mutation` with `confirmed: true` — confirm execution and log

**ALTER TABLE guard:**
- [ ] Call `alter_table_safe` with `confirmed: false` — confirm diff preview
- [ ] Call `alter_table_safe` with `confirmed: true` — confirm execution
- [ ] Call `get_schema` immediately after — confirm new schema is reflected

**Logging:**
- [ ] Run a mix of operations — confirm all appear in `logs/sql_mcp.jsonl`
- [ ] Call `get_logs` from Cursor — confirm entries match what was run

**Error translation:**
- [ ] Trigger a foreign key violation — confirm plain English appears
- [ ] Trigger a null constraint violation — confirm plain English appears

---

## Definition of Done — Phase 2

Phase 2 is complete when:

- [ ] All 8 tools are registered and callable from Cursor agent
- [ ] `run_select` enforces read-only, injects LIMIT, validates schema
- [ ] `validate_query` analyses without executing including dangerous SQL
- [ ] `dry_run` shows real row counts and rolls back every time
- [ ] `run_mutation` requires `confirmed: true` before executing
- [ ] `alter_table_safe` requires two-step confirmation and auto-refreshes schema
- [ ] `refresh_schema` updates the in-memory cache from live DB
- [ ] `get_logs` returns queryable audit history
- [ ] `get_status` returns a health summary in one call
- [ ] All errors return both raw Postgres message and plain English translation
- [ ] `logs/sql_mcp.jsonl` contains an entry for every tool call
- [ ] All `.py` files have detailed inline comments throughout
- [ ] Phase 1 tools still work unchanged

---

## What Phase 3 Needs From This Foundation

Phase 3 attempts to intercept SQLAlchemy's query execution and route it
through this MCP server. For that experiment to work, Phase 2 must provide:

1. **`run_select` and `run_mutation` must be callable as Python functions**
   — not just as MCP tools. Phase 3's SQLAlchemy session subclass will
   import and call them directly (bypassing the MCP protocol) when running
   in the same process. Design `phase2.py` so the tool implementations are
   regular functions that the MCP tool handlers wrap, not logic embedded
   inside the handler itself.

2. **The validator must accept a compiled SQL string** — SQLAlchemy outputs
   compiled SQL as a string. `validator.py` already works on strings, but
   make sure it handles parameterised query syntax (`$1`, `%s`) that
   SQLAlchemy may produce.

3. **The logger must be importable independently** — Phase 3 will call
   `log_entry()` from the SQLAlchemy session layer. It must not depend on
   the MCP server being running.

4. **Connection management must support two modes** — Phase 3 may need to
   share a connection with SQLAlchemy's connection pool rather than
   managing its own. Design `connection.py` to accept an optional external
   connection parameter for this scenario.

These are design decisions to make now, in Phase 2, so Phase 3 is not
blocked by architectural rework.
