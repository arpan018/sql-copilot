# Phase 1 — what we built

Written after the code worked against `dvd_rental_db`. Spec files `phase-1.md` and `phase-2.md` are unchanged.

## Decisions

- **Target database:** existing `dvd_rental_db` on PostgreSQL 18 (`localhost:5432`, user `postgres`). Application tables live in `public` only.
- **Credentials:** `DB_URL` lives in `.env` (gitignored). `.env.example` has placeholders only.
- **Pool, not one connection:** `SimpleConnectionPool` + `get_db_connection()` so Phase 2 can use `autocommit=False` per call without rewriting connection code.
- **FastMCP 4.x** (`uv add` resolved 4.0.2) with `@mcp.tool` and stdio `mcp.run()`. Cursor still lists the four tools.
- **sqlglot** is installed and unused until Phase 2.
- **`MODE=dev`** is in `.env` and unread until Phase 2.
- **Row counts** use `pg_class.reltuples` (estimate), not `COUNT(*)`.
- **Foreign keys** use `referential_constraints` + `key_column_usage` (safer for composite FKs than the spec’s shorter join).
- **`prepare_query_context` does not generate SQL.** Cursor’s agent writes the SELECT in chat. Matching is lowercase tokens + tiny stems (`rented` → `rent` hits `rental`).
- **Python 3.14.3:** `psycopg2-binary` 2.9.12 installed a wheel; no fallback was needed.
- **README** stays a stub until a later pass.

## Modules and functions

| Location | Function | Usage |
|----------|----------|--------|
| `connection.init_pool(dsn)` | Create the process-wide pool | `server.main()` once at startup |
| `connection.get_db_connection(autocommit=True)` | Borrow/return a connection | All DB access; `with` block only |
| `connection.close_pool()` | Close all pooled connections | Standalone tests |
| `schema.refresh_schema()` | Read catalogs into memory | Startup; later after DDL |
| `schema.get_cached_schema()` | Return the dict (or None) | Tools and a future validator |
| `schema.get_table_detail(name)` | One table + indexes + approx rows | `get_table_info` tool |
| `tools.phase1.register(mcp)` | Decorate and attach four tools | `server.py` |
| `ping_connection` | Live `SELECT version()` | First connectivity check |
| `get_schema` | Cached full schema JSON | Agent lists tables/columns |
| `get_table_info` | One table focus | Agent reasons about one relation |
| `prepare_query_context` | Keyword subset + join paths | Agent writes SQL in chat |
| `server.main()` | Pool → cache → `mcp.run()` | `uv run python -m sql_mcp.server` |

## How to use in Cursor

1. Keep PostgreSQL running and `.env` present (not committed).
2. **Cursor Settings → MCP** (or project `.cursor/mcp.json`): server `sql-copilot` should appear. Toggle it on / reload if tools are missing.
3. In Agent chat, ask e.g. “what tables do I have?” or “give me a query for customers who rented films.”
4. Confirm the agent calls `get_schema` / `prepare_query_context` and writes SQL itself (the tool JSON has no `sql` field).

## Verified locally (not via Cursor UI)

- `SELECT 1` and `current_database() = dvd_rental_db`
- Schema cache: 15 public tables; `film` ~1000 estimated rows
- FastMCP `list_tools`: `ping_connection`, `get_schema`, `get_table_info`, `prepare_query_context`
- `uv run python -m sql_mcp.server` starts transport `stdio` as `sql-copilot`
