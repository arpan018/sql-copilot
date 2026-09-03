"""
sql_mcp — MCP server package for schema-aware PostgreSQL access.

This package is the local subprocess Cursor launches via stdio. Phase 1
only reads metadata and returns JSON context; it does not execute
user-authored DML/DDL.
"""
