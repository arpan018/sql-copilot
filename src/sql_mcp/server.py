"""
MCP server entry point.

Creates the FastMCP instance, initialises the DB connection pool,
loads the schema cache, and registers every tool module.

Phase 2 registers tools via phase2.register(mcp) next to Phase 1.
load_dotenv() runs before those imports so MODE and LOG_PATH are visible.

Transport is stdio by default (mcp.run()). Cursor launches this as a
subprocess; there is no HTTP port.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from fastmcp import FastMCP

# Load .env before importing tools that read MODE / LOG_PATH / DB_URL.
load_dotenv()

from sql_mcp import connection, schema
from sql_mcp.tools import phase1
from sql_mcp.tools import phase2

mcp = FastMCP("sql-copilot")

phase1.register(mcp)
phase2.register(mcp)


def main() -> None:
    """
    Start order matters: pool, then cache, then stdio loop.

    Cursor keeps this process alive for the session. If DB_URL is
    missing we exit before mcp.run() so the client sees a clear error
    instead of hanging tools.
    """
    dsn = os.environ.get("DB_URL")
    if not dsn:
        print("DB_URL is not set. Copy .env.example to .env.", file=sys.stderr)
        sys.exit(1)

    connection.init_pool(dsn)
    schema.refresh_schema()
    mcp.run()


if __name__ == "__main__":
    main()
