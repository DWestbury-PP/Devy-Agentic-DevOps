"""Shared connection pool + schema bootstrap.

The store writes embeddings as ``vector`` literals (``%s::vector``) and never
reads them back, so no pgvector array adapter is needed — the pool is plain
psycopg. ``apply_schema`` creates the ``vector`` extension + tables and is
idempotent; the app applies it best-effort on startup and ``db init`` applies it
on demand (e.g. against managed databases).
"""

from __future__ import annotations

import threading
from importlib import resources

from psycopg_pool import ConnectionPool

_pools: dict[str, ConnectionPool] = {}
_lock = threading.Lock()


def schema_sql() -> str:
    """The bootstrap DDL, read from the packaged ``schema.sql``."""
    return resources.files("agentic_devops.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _statements(sql: str) -> list[str]:
    """Split the schema into individual statements (strip ``--`` comment lines
    first, then split on ``;``). The schema is deliberately simple — no functions
    or string literals containing semicolons — so this is safe, and it sidesteps
    psycopg3's single-statement-per-execute rule."""
    body = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def apply_schema(url: str) -> None:
    """Apply the idempotent bootstrap schema (extension + tables) to ``url``.

    Safe to run repeatedly. Requires a role allowed to ``CREATE EXTENSION`` the
    first time (on managed databases, run ``agentic-devops db init`` as an admin).
    """
    import psycopg  # local import: only needed at bootstrap, keeps import graph light

    with psycopg.connect(url, autocommit=True) as conn:
        for stmt in _statements(schema_sql()):
            conn.execute(stmt)


def get_pool(url: str) -> ConnectionPool:
    """Return the process-wide pool for ``url`` (created once, connections
    autocommit). Idempotent — repeated calls with the same DSN reuse the pool."""
    with _lock:
        pool = _pools.get(url)
        if pool is None:
            pool = ConnectionPool(
                url,
                min_size=1,
                max_size=8,
                kwargs={"autocommit": True},
                open=True,
            )
            _pools[url] = pool
        return pool


def close_all() -> None:
    """Close every open pool (used on shutdown / between test sessions)."""
    with _lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
