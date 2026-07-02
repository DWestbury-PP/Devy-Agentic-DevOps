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
            "TRUNCATE chunks, sessions, conversation_memories, memories, hosts, "
            "github_accounts, repo_crawls, repo_docgen, doc_components, documents, "
            "ingest_jobs, mcp_servers"
        )
    return p


# -- secrets backend test double (Phase S-1) --------------------------------
# An in-memory AWS Secrets Manager client mirroring just the boto3 surface the
# SecretsProvider uses, so the suite is hermetic (no boto3 / LocalStack / network).
class _FakeSMClient:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

        class ResourceExistsException(Exception):
            pass

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get_secret_value(self, SecretId):
        if SecretId not in self._d:
            raise self.exceptions.ResourceNotFoundException()
        return {"SecretString": self._d[SecretId]}

    def describe_secret(self, SecretId):
        if SecretId not in self._d:
            raise self.exceptions.ResourceNotFoundException()
        return {"Name": SecretId}

    def create_secret(self, Name, SecretString):
        if Name in self._d:
            raise self.exceptions.ResourceExistsException()
        self._d[Name] = SecretString

    def put_secret_value(self, SecretId, SecretString):
        self._d[SecretId] = SecretString

    def delete_secret(self, SecretId, ForceDeleteWithoutRecovery=False):
        if SecretId not in self._d:
            raise self.exceptions.ResourceNotFoundException()
        del self._d[SecretId]

    def list_secrets(self, MaxResults=10):
        return {"SecretList": [{"Name": k} for k in list(self._d)[:MaxResults]]}


def make_fake_secrets(writable: bool = True, store_file=None):
    from agentic_devops.proxy.secrets import SecretsProvider

    return SecretsProvider(_FakeSMClient(), writable=writable, store_file=store_file)


@pytest.fixture()
def secrets():
    """A writable in-memory SecretsProvider for store-level tests."""
    return make_fake_secrets(writable=True)


@pytest.fixture(autouse=True)
def _patch_app_secrets(monkeypatch):
    """Make every create_app() in the suite use an in-memory secrets backend whose
    writability tracks settings.secrets.mode (so prod read-only / 403 tests work),
    instead of a real boto3 client. Patched on the app module (where it's bound)."""
    from agentic_devops.proxy.secrets import SecretsProvider

    def _fake_build(settings):
        return SecretsProvider(_FakeSMClient(), writable=settings.secrets.mode == "dev")

    monkeypatch.setattr("agentic_devops.proxy.app.build_secrets_provider", _fake_build)
