"""PostgreSQL connection and transaction helpers for IDDb ETL scripts."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()


def get_connection_kwargs() -> dict[str, Any]:
    """Build psycopg connection arguments from environment variables.

    Supports either a full DATABASE_URL connection string or standard
    PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD variables.
    """
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return {"conninfo": database_url, "connect_timeout": 10}

    pgdatabase = os.getenv("PGDATABASE")
    pguser = os.getenv("PGUSER")
    if not pgdatabase or not pguser:
        raise ValueError(
            "Database configuration missing: set DATABASE_URL or PGDATABASE and PGUSER."
        )

    kwargs: dict[str, Any] = {
        "dbname": pgdatabase,
        "user": pguser,
        "connect_timeout": 10,
    }
    if os.getenv("PGHOST"):
        kwargs["host"] = os.environ["PGHOST"]
    if os.getenv("PGPORT"):
        kwargs["port"] = int(os.environ["PGPORT"])
    if os.getenv("PGPASSWORD"):
        kwargs["password"] = os.environ["PGPASSWORD"]
    return kwargs


def connect() -> psycopg.Connection[dict[str, Any]]:
    """Open a PostgreSQL connection with dict rows and autocommit disabled."""
    kwargs = get_connection_kwargs()
    conn = psycopg.connect(**kwargs, row_factory=dict_row, autocommit=False)
    return conn


@contextmanager
def transaction(
    conn: psycopg.Connection[dict[str, Any]] | None = None,
) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """Run work inside a database transaction, rolling back on error.

    If no connection is supplied, a new connection is opened and closed
    automatically when the context exits.
    """
    owns_conn = conn is None
    active_conn = connect() if conn is None else conn
    try:
        with active_conn.transaction():
            yield active_conn
    finally:
        if owns_conn:
            active_conn.close()
