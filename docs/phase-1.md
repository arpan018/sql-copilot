# Phase 1 — Connect, Inspect, Query

## Problem Statement

When learning SQL, the biggest friction point is not knowing what your database
actually looks like at any given moment. You forget table names, misremember
column types, and write queries that reference columns that don't exist. The
feedback loop is: write query → run it → get an error → look up the schema →
fix the query → repeat.

This phase solves that by making Cursor's agent aware of your live PostgreSQL
schema at all times. Instead of switching to pgAdmin or running `\d tablename`
in psql, you ask Cursor in plain English and it gives you a ready-to-run query
built from the actual schema it just read from your database.

**This phase does not execute any queries against your data.** It only reads
schema metadata and generates SQL for you to review. Safety is absolute at
this stage.

---

## Goals

1. Establish a working MCP server in Python that Cursor agent can call.
2. Connect to a running PostgreSQL instance using credentials from `.env`.
3. Read and cache the full database schema on startup.
4. Expose schema information as MCP tools so the agent has live DB context.
5. Generate SELECT queries from natural language without running them.
6. Confirm the entire pipeline works end-to-end in Cursor agent mode.

---

## System Prerequisites

These should already be present on your machine:

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ | Runtime |
| uv | latest | Package and virtual environment manager |
| PostgreSQL | latest | Database server (must be running locally) |
| Cursor | latest | AI agent that calls this MCP server |
| npm | latest | Present but not used in this phase |

To verify before starting:
```bash
python --version
uv --version
psql --version
cursor --version   # or check Cursor is installed
```

---

## Technical Stack (Phase 1)

| Component | Library / Tool | Why |
|-----------|---------------|-----|
| MCP framework | `fastmcp` (standalone package, decorator API) | See note below — this is a deliberate choice, not the bare `mcp` package |
| PostgreSQL driver | `psycopg2-binary` | Synchronous, stable, widely documented — easiest for learning |
| Environment config | `python-dotenv` | Loads `.env` file so credentials never appear in code |
| SQL parser | `sqlglot` | Validates SQL syntax and identifies operation type without running queries. Installed now even though Phase 1 doesn't call it — Phase 2 depends on it heavily |
| Transport | `stdio` | Works with Cursor agent mode and Claude Code CLI with zero config changes |

### Why `fastmcp` and not the bare `mcp` package — read this before installing anything

As of mid-2026 the official `mcp` package on PyPI moved to a v2.x line built
around a newer spec revision. Most MCP clients that are actually shipping
right now — including Cursor — still speak the older, pre-v2 protocol line.
The standalone `fastmcp` package (the decorator-based framework that was
originally folded into the official SDK) pins itself to that older,
currently-interoperable line and gives the lowest-boilerplate way to define
tools: a plain Python function with type hints and a docstring becomes a
fully schema-validated MCP tool with one decorator.

**Install this, not `mcp` directly:**
```bash
uv add fastmcp
```

If a future version of Cursor or Claude Code requires the newer spec line,
the fix is a version bump in `pyproject.toml`, not a rewrite — the decorator
API (`@mcp.tool`, `mcp.run()`) is stable across both lines. This is called
out explicitly so nobody spends an afternoon debugging a "tool not found"
error in Cursor caused by a protocol version mismatch.

---

## Project Structure

```
sql-mcp/
├── .env                        # DB credentials — never commit this
├── .env.example                # Safe template to commit
├── .gitignore                  # Must include .env and __pycache__
├── pyproject.toml              # uv project manifest and dependencies
├── README.md                   # Setup and usage instructions
├── docs/
│   ├── phase-1.md              # This file
│   ├── phase-2.md              # Validation, execution, logging plan
│   └── phase-3.md              # ORM experiment plan (future)
├── src/
│   └── sql_mcp/
│       ├── __init__.py         # Package marker
│       ├── server.py           # MCP server entry point — registers all tools
│       ├── connection.py       # PostgreSQL connection management and health check
│       ├── schema.py           # Schema introspection — reads information_schema
│       └── tools/
│           ├── __init__.py     # Package marker
│           └── phase1.py       # Tool implementations for Phase 1
└── .cursor/
    └── mcp.json                # Cursor reads this to discover and launch the server
```

**Important:** Every `.py` file created in this project must contain detailed
inline comments explaining:
- What each class and function does
- Why a particular approach was chosen
- What the input and output look like
- Any PostgreSQL or MCP-specific behaviour the reader should know

This is a learning project. Comments are not optional.

---

## `pyproject.toml` — Exact Dependencies

Use this content directly. `sqlglot` is included now even though Phase 1
does not call it, so Phase 2 never needs a fresh `uv sync` mid-build.

```toml
[project]
name = "sql-mcp"
version = "0.1.0"
description = "MCP server for safe, schema-aware PostgreSQL access from AI agents"
requires-python = ">=3.11"
dependencies = [
    "fastmcp>=3.0",
    "psycopg2-binary>=2.9",
    "python-dotenv>=1.0",
    "sqlglot>=23.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/sql_mcp"]
```

Run `uv sync` after creating this file to install everything in one pass.

---

## Environment Configuration

### `.env` file (never commit)
```
# PostgreSQL connection string
# Format: postgresql://username:password@host:port/database_name
DB_URL=postgresql://postgres:yourpassword@localhost:5432/yourdbname

# Human-readable label shown in logs and tool responses
DB_NAME=local-learning-db

# dev = relaxed, prod = stricter safeguards (used in Phase 2)
MODE=dev
```

### `.env.example` file (safe to commit)
```
DB_URL=postgresql://username:password@localhost:5432/database_name
DB_NAME=your-db-name
MODE=dev
```

**Note for later:** Phase 2 appends one more variable, `LOG_PATH`, to this
same `.env` file — it does not replace it. Phase 2's document shows the
exact line to add. `MODE` is declared here but Phase 1 does not read it;
Phase 2 is the first phase that branches behaviour on it (blocking
destructive DDL when `MODE=prod`).

---

## `connection.py` Design — Read Carefully, This Prevents Phase 2 Rework

Do not implement this as "open one connection at startup and reuse it
everywhere." That pattern works for Phase 1's read-only calls but breaks
the moment Phase 2 needs per-call transactions (`BEGIN` / `COMMIT` /
`ROLLBACK` for `dry_run` and `run_mutation`) — a single shared connection
cannot safely be mid-transaction for one tool call while another call needs
its own transaction state.

**Build a connection pool from day one instead**, even though Phase 1 only
ever reads from it. This is the one piece of Phase 1 that exists purely to
save Phase 2 from a rewrite.

```python
# connection.py — conceptual shape, write the real version with full comments

from contextlib import contextmanager
from psycopg2 import pool

_connection_pool = None  # module-level, created once

def init_pool(dsn: str, minconn: int = 1, maxconn: int = 5) -> None:
    """Create the pool once, called from server.py at startup."""
    global _connection_pool
    _connection_pool = pool.SimpleConnectionPool(minconn, maxconn, dsn)

@contextmanager
def get_db_connection(autocommit: bool = True):
    """
    Borrow a connection from the pool, guarantee it's returned even on
    error. Phase 1 tools call this with the default autocommit=True since
    they only read. Phase 2's mutation tools call this with
    autocommit=False so they control BEGIN/COMMIT/ROLLBACK explicitly.
    """
    conn = _connection_pool.getconn()
    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        _connection_pool.putconn(conn)
```

Every tool — Phase 1 or Phase 2 — accesses the database through
`with get_db_connection() as conn:` and nothing else. No tool ever holds a
connection outside that `with` block. This one rule is what makes Phase 2's
transaction safety model (see `phase-2.md`) work without touching
`connection.py` again.

---

## MCP Concepts Used in This Phase

Before coding, understand these two MCP primitives:

**Tools** — Functions the AI agent can call. They take arguments and return
a result. Think of them like API endpoints. The agent decides when to call
them based on what the user asks.

**Resources** — Read-only data the agent can access passively, like a
configuration file or a reference document. Schema cache could live here,
but in Phase 1 we expose it via a tool for simplicity. Phase 2 may promote
it to a resource.

**Transport** — How the agent and server communicate. We use `stdio` which
means the server reads from stdin and writes to stdout. Cursor launches the
server as a subprocess and pipes messages through it. No network ports needed.

---

## Tools to Implement

### `ping_connection`

**Purpose:** Verify the PostgreSQL server is reachable and return basic info.

**Arguments:** None

**Returns:**
```json
{
  "status": "connected",
  "db_name": "local-learning-db",
  "postgres_version": "PostgreSQL 16.2",
  "host": "localhost",
  "port": 5432
}
```

**On failure:**
```json
{
  "status": "error",
  "message": "could not connect to server: Connection refused"
}
```

**Why this exists:** First tool to implement and test. Confirms the entire
MCP → Python → PostgreSQL pipeline is working before building anything else.

---

### `get_schema`

**Purpose:** Return the full schema of the connected database — all tables,
columns, data types, nullable flags, primary keys, and foreign keys.

**Arguments:** None

**Returns:** Structured JSON of the entire schema. Example:
```json
{
  "database": "local-learning-db",
  "tables": {
    "users": {
      "columns": [
        {"name": "id", "type": "integer", "nullable": false, "primary_key": true},
        {"name": "email", "type": "varchar", "nullable": false, "primary_key": false},
        {"name": "created_at", "type": "timestamp", "nullable": true, "primary_key": false}
      ],
      "foreign_keys": []
    },
    "orders": {
      "columns": [
        {"name": "id", "type": "integer", "nullable": false, "primary_key": true},
        {"name": "user_id", "type": "integer", "nullable": false, "primary_key": false},
        {"name": "total", "type": "numeric", "nullable": false, "primary_key": false}
      ],
      "foreign_keys": [
        {"column": "user_id", "references_table": "users", "references_column": "id"}
      ]
    }
  },
  "cached_at": "2026-08-23T10:30:00"
}
```

**Data source:** PostgreSQL's `information_schema.columns`,
`information_schema.table_constraints`, and
`information_schema.key_column_usage` views. These are standard SQL — no
Postgres-specific extensions needed.

**Caching:** Schema is fetched once on server startup and stored in memory.
`get_schema` returns the cached version. `refresh_schema` (Phase 2) forces
a re-read. This avoids hammering the DB on every agent message.

---

### `get_table_info`

**Purpose:** Return focused detail on a single table — useful when the agent
needs to reason about one table without the noise of the full schema.

**Arguments:**
```json
{"table_name": "orders"}
```

**Returns:** Same structure as one entry from `get_schema`, plus row count:
```json
{
  "table": "orders",
  "columns": [...],
  "foreign_keys": [...],
  "approximate_row_count": 1042,
  "indexes": ["orders_pkey", "orders_user_id_idx"]
}
```

**Why row count:** Knowing a table has 2 million rows changes how you write
a query. This is a learning-relevant detail.

---

### `prepare_query_context`

**Important — read this before implementing:** an MCP tool is a plain Python
function. It has no way to call back into Cursor's own model and ask it to
"write a SQL query" — that reasoning is not something your Python code can
do internally without your own separate LLM API key, which defeats the
whole "no paid API needed" point of this project. So this tool does **not**
generate SQL. It never has a `sql` field in its response. Writing the actual
SELECT statement is the agent's job, not this tool's — Cursor's own model
does that in its normal chat reply, using the context this tool hands it.

**Purpose:** Given a plain-English intent, narrow the full schema down to
just the tables and columns that are probably relevant, plus any foreign
key paths between them. This is pure Python string matching — no LLM call,
no external API, fully deterministic and instant.

**Arguments:**
```json
{
  "intent": "find all users who placed more than 3 orders"
}
```

**How it works internally:** Lowercase the intent string, split into
keywords, match keywords against table names and column names in the
cached schema (simple substring matching is enough — e.g. "users" and
"orders" appear in the intent and match table names directly). Return the
matched tables' full column lists plus any foreign keys connecting them.

**Returns:**
```json
{
  "intent": "find all users who placed more than 3 orders",
  "likely_relevant_tables": ["users", "orders"],
  "schema_context": {
    "users": {
      "columns": [
        {"name": "id", "type": "integer"},
        {"name": "email", "type": "varchar"}
      ]
    },
    "orders": {
      "columns": [
        {"name": "id", "type": "integer"},
        {"name": "user_id", "type": "integer"},
        {"name": "total", "type": "numeric"}
      ]
    }
  },
  "join_paths": [
    {"from_table": "orders", "from_column": "user_id", "to_table": "users", "to_column": "id"}
  ],
  "note": "No SQL is generated by this tool. Use this context to write the query yourself, in-chat."
}
```

**What actually happens in Cursor:** You ask "give me a query to find users
with more than 3 orders." Cursor's agent calls `prepare_query_context`
(or just calls `get_schema` directly for a small DB — this tool is an
optimisation for larger schemas, not a requirement), gets real column names
and the join path back, and then the agent itself writes the SQL in its
response to you, grounded in real identifiers instead of guessed ones.
That's the whole mechanism — no hallucinated columns because the agent saw
the real schema, not because a tool secretly wrote the query for it.

**Output is read-only:** Nothing is executed. Phase 1 never touches data.

---

## Reference Skeleton — `server.py` and Tool Registration Pattern

This exact pattern must be followed so Phase 2 can add its tools by adding
one import line and one function call — no restructuring. Tool modules
never import `server.py` (that would create a circular import); instead
each tool module exposes a `register(mcp)` function that `server.py` calls.

```python
# src/sql_mcp/tools/phase1.py  (shape only — write the real version with
# full docstrings, error handling, and comments)

def register(mcp):
    """
    Attach every Phase 1 tool to the given FastMCP instance.
    Called once from server.py at startup.
    """

    @mcp.tool
    def ping_connection() -> dict:
        """Check that PostgreSQL is reachable and return basic info."""
        ...  # implementation using connection.get_db_connection()

    @mcp.tool
    def get_schema() -> dict:
        """Return the full cached database schema."""
        ...  # implementation using schema.py's in-memory cache

    @mcp.tool
    def get_table_info(table_name: str) -> dict:
        """Return focused schema detail and row count for one table."""
        ...

    @mcp.tool
    def prepare_query_context(intent: str) -> dict:
        """Narrow the schema to tables/columns relevant to a stated intent."""
        ...
```

```python
# src/sql_mcp/server.py

"""
MCP server entry point.
Creates the FastMCP instance, initialises the DB connection pool,
loads the schema cache, and registers every tool module.
Phase 2 extends this file by adding exactly two lines — see the
comment marked PHASE 2 below.
"""

import os
from dotenv import load_dotenv
from fastmcp import FastMCP

from sql_mcp import connection, schema
from sql_mcp.tools import phase1
# PHASE 2 will add: from sql_mcp.tools import phase2

load_dotenv()

mcp = FastMCP("sql-copilot")

phase1.register(mcp)
# PHASE 2 will add: phase2.register(mcp)


def main() -> None:
    connection.init_pool(os.environ["DB_URL"])
    schema.refresh_schema()  # populate the cache before any tool call
    mcp.run()  # stdio transport by default — this is what Cursor expects


if __name__ == "__main__":
    main()
```

Every `.py` file listed above must be written with the full detailed
comments required by this project — the snippets here are structural
shape only, not the final code.

---

## Cursor Integration

### `.cursor/mcp.json`

```json
{
  "mcpServers": {
    "sql-copilot": {
      "command": "uv",
      "args": ["run", "python", "-m", "sql_mcp.server"],
      "cwd": "/absolute/path/to/sql-mcp",
      "env": {
        "PYTHONPATH": "src"
      }
    }
  }
}
```

**How Cursor uses this:**
1. When Cursor agent mode starts, it reads this file.
2. It launches your Python server as a subprocess.
3. It discovers available tools by sending an `initialize` message.
4. When you ask Cursor a DB question, it decides which tool to call.
5. Results come back as tool outputs that the agent uses to form its response.

**You do not need a paid Anthropic API key.** Cursor's agent uses its own
model budget. Your MCP server is just a local subprocess.

---

## Implementation Order

Build and test in this exact sequence. Do not skip ahead.

1. `pyproject.toml` + `uv sync`
2. `.env` + `.env.example`
3. `connection.py` — build the pool (`init_pool`) and the
   `get_db_connection()` context manager per the design above
4. Test `connection.py` standalone: write a throwaway script that calls
   `init_pool()` then `get_db_connection()` and runs `SELECT 1`
5. `schema.py` — introspect schema using the queries below, store in a
   module-level cache dict, expose `refresh_schema()`
6. Test `schema.py` standalone — print schema JSON to terminal
7. `tools/phase1.py` — implement `register(mcp)` with all four tools,
   using `connection.get_db_connection()` and the `schema.py` cache
8. `server.py` — wire up per the reference skeleton above
9. `.cursor/mcp.json` — point Cursor at the server
10. Test in Cursor — ask "what tables do I have?" and "give me a query to find X"

Each step should work in isolation before the next step depends on it. This
is how you debug — if step 8 breaks, the problem is in server.py, not in
connection.py which you already confirmed works.

---

## Key PostgreSQL Queries Used Internally

These are the queries `schema.py` will run against `information_schema`.
Understanding them is part of learning SQL.

### Get all tables in the current database
```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_type = 'BASE TABLE'
ORDER BY table_name;
```

### Get all columns for a specific table
```sql
SELECT
    column_name,
    data_type,
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'your_table_name'
ORDER BY ordinal_position;
```

### Get primary key columns
```sql
SELECT kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY'
  AND tc.table_schema = 'public'
  AND tc.table_name = 'your_table_name';
```

### Get foreign keys
```sql
SELECT
    kcu.column_name,
    ccu.table_name AS references_table,
    ccu.column_name AS references_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage ccu
    ON tc.constraint_name = ccu.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema = 'public'
  AND tc.table_name = 'your_table_name';
```

Knowing these queries means you understand `information_schema` — a standard
SQL concept that exists in Postgres, MySQL, and SQL Server. This is not
wasted effort.

---

## Definition of Done — Phase 1

Phase 1 is complete when all of the following are true:

- [ ] `uv run python -m sql_mcp.server` starts without errors
- [ ] Cursor agent lists `ping_connection`, `get_schema`, `get_table_info`,
      `prepare_query_context` as available tools
- [ ] `ping_connection` returns DB name and Postgres version
- [ ] `get_schema` returns all tables and columns from the connected DB
- [ ] `get_table_info` returns focused detail and approximate row count
- [ ] `prepare_query_context` returns a relevant table/column subset for
      a given intent, and Cursor's agent writes a correct SQL query in its
      chat reply using that context (not returned by the tool itself)
- [ ] All `.py` files have detailed inline comments throughout
- [ ] `.env` is in `.gitignore` and never committed
- [ ] README documents setup steps from scratch

---

## What Phase 2 Needs From This Foundation

Phase 2 will extend this codebase, not replace it. For it to work, Phase 1
must deliver:

1. **The connection pool and `get_db_connection()` context manager from
   `connection.py`**, unchanged. Phase 2's mutation tools call it with
   `autocommit=False` to control transactions explicitly — the same
   function, a different argument. No changes to `connection.py` itself
   should be needed.

2. **A schema cache that is accessible as a Python dict** — not just
   returned from a tool. Phase 2's validator needs to check column names
   against the cache programmatically without making a DB round-trip.

3. **The `register(mcp)` pattern in every tools module** — Phase 2 adds
   its tools by adding `from sql_mcp.tools import phase2` and
   `phase2.register(mcp)` to `server.py`, exactly two lines, no other
   changes to `server.py`.

4. **`sqlglot` already installed** — Phase 2 depends on it heavily.
   Add it to `pyproject.toml` now even though Phase 1 does not use it.

---

## Common Mistakes to Avoid

**Do not hardcode the DB URL in any Python file.** Always read from `.env`.
If the URL appears in code, it will end up in Git history.

**Do not open raw `psycopg2.connect()` calls anywhere outside `connection.py`.**
Every tool borrows from the shared pool via `get_db_connection()` and lets
the `with` block return it automatically. This keeps Postgres's connection
limit respected and is what makes Phase 2's transactions safe.

**Do not return raw psycopg2 exceptions to the agent.** Catch them, log the
full error internally, and return a clean JSON error object. Raw stack traces
in MCP responses confuse the agent.

**Do not skip the standalone tests.** Testing `connection.py` and `schema.py`
directly before wiring them into the MCP server saves hours of debugging.
