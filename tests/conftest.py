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
    """Session-wide DSN migrated to head; skips if unreachable."""
    from agentic_devops.db import close_all
    from agentic_devops.db.migrate import migrate

    try:
        migrate(TEST_DSN)
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


class _FakeSSMClient:
    """In-memory SSM Parameter Store shape for the release-ledger tests — supports
    the two reads the ledger uses (get_parameter, get_parameters_by_path). Set
    ``deny=True`` to simulate an IAM-denied path (every call raises)."""

    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, params: dict[str, str] | None = None, deny: bool = False) -> None:
        self._d: dict[str, str] = dict(params or {})
        self._deny = deny

    def _guard(self) -> None:
        if self._deny:
            raise RuntimeError("AccessDeniedException: not authorized to perform ssm:GetParameter")

    def get_parameter(self, Name: str):
        self._guard()
        if Name not in self._d:
            raise self.exceptions.ParameterNotFound()
        return {"Parameter": {"Name": Name, "Value": self._d[Name]}}

    def get_parameters_by_path(self, Path: str, Recursive: bool = True, MaxResults: int = 10, NextToken: str | None = None):
        self._guard()
        matches = [
            {"Name": k, "Value": v} for k, v in self._d.items()
            if k == Path or k.startswith(Path.rstrip("/") + "/")
        ]
        # Emulate pagination so the ledger's NextToken loop is exercised.
        start = int(NextToken) if NextToken else 0
        page = matches[start:start + MaxResults]
        out: dict = {"Parameters": page}
        if start + MaxResults < len(matches):
            out["NextToken"] = str(start + MaxResults)
        return out


def make_fake_ledger(params: dict[str, str] | None = None, deny: bool = False, prefix: str = "/devy/builds"):
    from agentic_devops.proxy.releases import ReleaseLedger

    return ReleaseLedger(_FakeSSMClient(params, deny=deny), prefix=prefix)


@pytest.fixture()
def secrets():
    """A writable in-memory SecretsProvider for store-level tests."""
    return make_fake_secrets(writable=True)


@pytest.fixture()
def make_secrets():
    """Factory for fresh in-memory SecretsProviders (e.g. a source + target pair)."""
    return make_fake_secrets


@pytest.fixture(autouse=True)
def _patch_app_secrets(monkeypatch):
    """Make every create_app() in the suite use an in-memory secrets backend whose
    writability tracks settings.secrets.mode (so prod read-only / 403 tests work),
    instead of a real boto3 client. Patched on the app module (where it's bound).

    ALSO neutralize build_blob_store: attachments default to enabled, and
    build_blob_store eagerly builds a real boto3 S3 client + ensure_bucket(). On a
    host with ambient AWS creds (AWS_PROFILE) and no AWS_ENDPOINT_URL that would
    create a bucket in a REAL account. Tests that exercise blobs patch it back to a
    fake store themselves (this per-test monkeypatch wins over the autouse one)."""
    from agentic_devops.proxy.secrets import SecretsProvider

    def _fake_build(settings):
        return SecretsProvider(_FakeSMClient(), writable=settings.secrets.mode == "dev")

    monkeypatch.setattr("agentic_devops.proxy.app.build_secrets_provider", _fake_build)
    monkeypatch.setattr("agentic_devops.proxy.app.build_blob_store", lambda settings: None)
