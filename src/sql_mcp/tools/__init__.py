"""
Phase 1 / Phase 2 tool packages.

Each module exposes register(mcp) and must never import sql_mcp.server,
which would create a circular import.
"""
