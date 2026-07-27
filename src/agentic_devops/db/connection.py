"""Shared connection pool.

The store writes embeddings as ``vector`` literals (``%s::vector``) and never
reads them back, so no pgvector array adapter is needed — the pool is plain
psycopg. Schema creation + evolution is owned by the versioned migration runner
(``agentic_devops.db.migrate`` / ``agentic-devops db migrate``); this module only
manages the runtime connection pool.
"""

from __future__ import annotations

import threading

from psycopg_pool import ConnectionPool

_pools: dict[str, ConnectionPool] = {}
_lock = threading.Lock()


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
