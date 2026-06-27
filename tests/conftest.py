"""Shared test fixtures — chiefly the Postgres the store and sessions need.

Postgres is required (there is no SQLite/JSON fallback), so the DB-backed tests
need a live pgvector instance. Point ``AGENTIC_TEST_DATABASE_URL`` at one, or use
the default below and start a throwaway:

    docker run -d --name agentic-test-pg \
        -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agentic_test \
        -p 5433:5432 pgvector/pgvector:pg16

If no database is reachable, the DB-backed tests skip (with this hint) rather
than erroring — the pure-logic suites (chunking, router, harness, …) still run.
"""

from __future__ import annotations

import os

import pytest

TEST_DSN = os.environ.get(
    "AGENTIC_TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/agentic_test",
)


@pytest.fixture(scope="session")
def pg_url():
    """Session-wide DSN with the bootstrap schema applied; skips if unreachable."""
    from agentic_devops.db import apply_schema, close_all

    try:
        apply_schema(TEST_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"Postgres not reachable at {TEST_DSN} ({exc}). Start one:\n"
            "  docker run -d --name agentic-test-pg -e POSTGRES_PASSWORD=postgres "
            "-e POSTGRES_DB=agentic_test -p 5433:5432 pgvector/pgvector:pg16"
        )
    yield TEST_DSN
    close_all()


@pytest.fixture()
def pool(pg_url):
    """A clean pool: truncates the tables before each test for isolation."""
    from agentic_devops.db import get_pool

    p = get_pool(pg_url)
    with p.connection() as conn:
        conn.execute(
            "TRUNCATE chunks, sessions, conversation_memories, memories, hosts, documents, ingest_jobs"
        )
    return p
