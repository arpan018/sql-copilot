"""
PostgreSQL connection pool for the MCP server.

Why a pool instead of one connection at startup
-----------------------------------------------
Inspect tools only read catalog metadata, so a single long-lived
connection would appear to work. Execute tools need per-call transactions
(BEGIN / COMMIT / ROLLBACK for dry_run and mutations). Two tools cannot
safely share one connection that might already be mid-transaction.

Every database access in this project must go through
get_db_connection(). Tools never call psycopg2.connect() themselves.
That keeps the server within Postgres's connection limit and returns
connections to the pool even when a query fails.

Inputs / outputs
----------------
init_pool(dsn): dsn is a libpq URL from DB_URL in .env, for example
    postgresql://postgres:postgres@localhost:5432/dvd_rental_db
get_db_connection(autocommit=True): yields a live psycopg2 connection.
    Inspect tools keep autocommit=True (read-only). Mutation tools pass
    autocommit=False and call commit()/rollback() inside the with-block.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from psycopg2 import pool
from psycopg2.extensions import connection as PgConnection

# Created once in server.main() via init_pool(). None until then so a
# forgotten init is an obvious AttributeError/RuntimeError, not a
# mysterious hang on a dead connection.
_connection_pool: pool.SimpleConnectionPool | None = None


def init_pool(dsn: str, minconn: int = 1, maxconn: int = 5) -> None:
    """
    Create the process-wide SimpleConnectionPool.

    Called once from server.py at startup, before any tool runs.
    minconn=1 keeps a warm connection; maxconn=5 is enough for a local
    Cursor session without exhausting a default Postgres max_connections.
    """
    global _connection_pool
    _connection_pool = pool.SimpleConnectionPool(minconn, maxconn, dsn)


def close_pool() -> None:
    """Shut down every pooled connection. Optional; used in standalone tests."""
    global _connection_pool
    if _connection_pool is not None:
        _connection_pool.closeall()
        _connection_pool = None


@contextmanager
def get_db_connection(autocommit: bool = True) -> Iterator[PgConnection]:
    """
    Borrow one connection from the pool and always return it.

    The finally block runs on success and on exception, so a failed
    information_schema query cannot leak a connection. After putconn,
    the caller must not use the connection object.

    autocommit=True (default): each statement is its own transaction.
    Fine for SELECT against catalogs.
    autocommit=False: the caller owns COMMIT/ROLLBACK.
    """
    if _connection_pool is None:
        raise RuntimeError(
            "Database pool is not initialised. Call connection.init_pool() first."
        )

    conn = _connection_pool.getconn()
    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        # If a caller leaves an open transaction, reset it
        # before reuse so the next tool does not inherit that state.
        try:
            if not conn.closed and not conn.autocommit:
                conn.rollback()
        except Exception:
            pass
        _connection_pool.putconn(conn)
