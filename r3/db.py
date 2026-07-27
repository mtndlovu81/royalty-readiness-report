"""Postgres access.

Parameterized queries only — every helper here takes SQL and a params tuple and
passes them separately to psycopg. Never build SQL with f-strings or `%`; where
a query needs a dynamic column (sorting, for instance), whitelist the value
against a fixed set and interpolate the *whitelisted constant*, never the user's
input.

Rows come back as dicts. The pool is created lazily, so importing this module
does not open a connection — scripts and tests can import it without a database.
"""

import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from r3 import config

log = logging.getLogger(__name__)

Params = Sequence[Any] | dict[str, Any] | None

# How long a request waits for a connection before giving up.
CONNECT_TIMEOUT_SECONDS = 5.0

# Everything that means "the database isn't answering". `PoolTimeout` is not an
# `OperationalError` — it comes from psycopg_pool — so anything catching only
# the latter misses the case where Postgres is unreachable entirely.
UNAVAILABLE = (psycopg.OperationalError, PoolTimeout, PoolClosed)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
    """The process-wide connection pool, opened on first use."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                p = ConnectionPool(
                    conninfo=config.DATABASE_URL,
                    min_size=1,
                    max_size=10,
                    kwargs={"row_factory": dict_row},
                    # Fail fast when Postgres is unreachable. The default is 30
                    # seconds, which means a visitor stares at a blank tab for
                    # half a minute before getting an error — worse than being
                    # told promptly that something is wrong.
                    timeout=CONNECT_TIMEOUT_SECONDS,
                    open=False,
                )
                p.open()
                _pool = p
                log.debug("connection pool opened")
    return _pool


def close_pool() -> None:
    """Close the pool. Called on application shutdown; safe if never opened."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
            log.debug("connection pool closed")


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection wrapped in a transaction.

    The block commits on clean exit and rolls back on exception, so multi-table
    writes get atomicity by doing all the work inside one `with` block.
    """
    with pool().connection() as conn:
        yield conn


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    with connection() as conn:
        with conn.cursor() as cur:
            yield cur


def connect() -> psycopg.Connection:
    """A single unpooled connection, for one-shot scripts like init_db."""
    return psycopg.connect(config.DATABASE_URL, row_factory=dict_row)


def query(sql: str, params: Params = None) -> list[dict[str, Any]]:
    """Run a SELECT and return every row."""
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: Params = None) -> dict[str, Any] | None:
    """Run a SELECT and return the first row, or None."""
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def query_value(sql: str, params: Params = None) -> Any:
    """Run a SELECT and return the first column of the first row, or None."""
    row = query_one(sql, params)
    if row is None:
        return None
    return next(iter(row.values()), None)


def execute(sql: str, params: Params = None) -> int:
    """Run a write and return the number of rows affected."""
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_returning(sql: str, params: Params = None) -> dict[str, Any] | None:
    """Run a write with a RETURNING clause and give back the row."""
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def healthy() -> bool:
    """True if the database answers. Used by the load balancer health check."""
    try:
        return query_value("SELECT 1") == 1
    except UNAVAILABLE + (psycopg.Error,):
        log.exception("database health check failed")
        return False
