"""
Build dvd_rental_mcp_exp from dvd_rental_db without mutating the source.

Why a clone
-----------
Phase 2 executes INSERT/UPDATE/DELETE/ALTER. The original dvd_rental
sample must stay intact. This script creates a same-schema experiment
database with at most ROW_CAP rows per table, plus sql_mcp_sandbox for
mutation demos.

How
---
1. CREATE DATABASE dvd_rental_mcp_exp (drop first if --recreate).
2. pg_dump --schema-only from dvd_rental_db, restore with psql.
3. Copy min(ROW_CAP, all) rows per public base table with
   session_replication_role = replica so FK sampling does not fail.
4. CREATE TABLE sql_mcp_sandbox for Phase 2 mutation tests.

Credentials come from SOURCE_DB_URL (defaults to the original dvd_rental
URL). This script does not rewrite .env; you point DB_URL at the
experiment database after it succeeds.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

ROW_CAP = 200
SOURCE_DB = "dvd_rental_db"
TARGET_DB = "dvd_rental_mcp_exp"
PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")


def _parse_url(url: str) -> dict:
    parsed = urlparse(url)
    return {
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "dbname": (parsed.path or "/").lstrip("/") or "postgres",
    }


def _connect(url: str, dbname: str | None = None):
    parts = _parse_url(url)
    if dbname is not None:
        parts["dbname"] = dbname
    return psycopg2.connect(
        user=parts["user"],
        password=parts["password"],
        host=parts["host"],
        port=parts["port"],
        dbname=parts["dbname"],
    )


def _run_pg(tool: str, args: list[str], env: dict, check: bool = True) -> subprocess.CompletedProcess:
    exe = PG_BIN / f"{tool}.exe"
    if not exe.exists():
        raise FileNotFoundError(f"PostgreSQL 18 tool not found: {exe}")
    return subprocess.run(
        [str(exe), *args],
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def create_database(admin_url: str, recreate: bool) -> None:
    """CREATE DATABASE cannot run inside a transaction."""
    conn = _connect(admin_url, "postgres")
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TARGET_DB,))
    exists = cur.fetchone() is not None
    if exists and recreate:
        # Terminate sessions so DROP DATABASE succeeds.
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (TARGET_DB,),
        )
        cur.execute(f'DROP DATABASE "{TARGET_DB}"')
        exists = False
    if not exists:
        cur.execute(f'CREATE DATABASE "{TARGET_DB}"')
        print(f"created database {TARGET_DB}")
    else:
        print(f"database {TARGET_DB} already exists (pass --recreate to rebuild)")
    cur.close()
    conn.close()


def restore_schema(parts: dict, env: dict) -> None:
    dump = _run_pg(
        "pg_dump",
        [
            "-h",
            parts["host"],
            "-p",
            parts["port"],
            "-U",
            parts["user"],
            "-d",
            SOURCE_DB,
            "--schema-only",
            "--no-owner",
            "--no-privileges",
        ],
        env,
    )
    restore = subprocess.run(
        [
            str(PG_BIN / "psql.exe"),
            "-h",
            parts["host"],
            "-p",
            parts["port"],
            "-U",
            parts["user"],
            "-d",
            TARGET_DB,
            "-v",
            "ON_ERROR_STOP=1",
        ],
        env=env,
        input=dump.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if restore.returncode != 0:
        print(restore.stderr, file=sys.stderr)
        raise RuntimeError("schema restore failed")
    print("schema restored from", SOURCE_DB)


def copy_sampled_rows(source_url: str) -> None:
    src = _connect(source_url, SOURCE_DB)
    dst = _connect(source_url, TARGET_DB)
    src.autocommit = True
    dst.autocommit = True
    with src.cursor() as scur, dst.cursor() as dcur:
        scur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        tables = [row[0] for row in scur.fetchall()]
        dcur.execute("SET session_replication_role = replica")
        for table in tables:
            ident = f'"{table}"'
            scur.execute(f"SELECT COUNT(*) FROM {ident}")
            total = scur.fetchone()[0]
            take = min(ROW_CAP, total)
            scur.execute(f"SELECT * FROM {ident} LIMIT {take}")
            rows = scur.fetchall()
            colnames = [desc[0] for desc in scur.description]
            if not rows:
                print(f"  {table}: 0 rows")
                continue
            cols = ", ".join(f'"{c}"' for c in colnames)
            placeholders = ", ".join(["%s"] * len(colnames))
            dcur.executemany(
                f"INSERT INTO {ident} ({cols}) VALUES ({placeholders})",
                rows,
            )
            print(f"  {table}: copied {len(rows)} of {total}")
        dcur.execute("SET session_replication_role = origin")
    src.close()
    dst.close()


def create_sandbox(source_url: str) -> None:
    conn = _connect(source_url, TARGET_DB)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sql_mcp_sandbox (
                id SERIAL PRIMARY KEY,
                label TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                notes TEXT
            )
            """
        )
        cur.execute("TRUNCATE sql_mcp_sandbox")
        cur.execute(
            """
            INSERT INTO sql_mcp_sandbox (label, amount, notes)
            VALUES
                ('alpha', 10, 'seed row'),
                ('beta', 20, 'seed row'),
                ('gamma', 30, 'seed row')
            """
        )
    conn.close()
    print("sql_mcp_sandbox ready (3 seed rows)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-url",
        default=os.environ.get(
            "SOURCE_DB_URL",
            "postgresql://postgres:postgres@localhost:5432/dvd_rental_db",
        ),
        help="Admin URL used to reach localhost Postgres (database name is ignored).",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="DROP and recreate dvd_rental_mcp_exp if it already exists.",
    )
    args = parser.parse_args()
    parts = _parse_url(args.source_url)
    env = os.environ.copy()
    env["PGPASSWORD"] = parts["password"]
    create_database(args.source_url, args.recreate)
    restore_schema(parts, env)
    print("copying sampled rows (cap", ROW_CAP, ")")
    copy_sampled_rows(args.source_url)
    create_sandbox(args.source_url)
    print("done. Point DB_URL at postgresql://...@localhost:5432/dvd_rental_mcp_exp")


if __name__ == "__main__":
    main()
