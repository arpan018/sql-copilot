"""
Inspect and execute tool modules.

Each module exposes register(mcp) and must never import sql_mcp.server,
which would create a circular import.
"""
