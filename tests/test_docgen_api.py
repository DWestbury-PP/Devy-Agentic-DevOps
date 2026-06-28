"""Doc-generation admin API (Phase D-2-2): trigger, list, brief.

Covers the thin endpoint layer — auth gating, the disabled/no-account guards,
brief persistence, list shape, and that a trigger spawns the background run.
The generation engine itself is exercised end-to-end in ``test_docgen.py``.
"""

import bcrypt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import agentic_devops.proxy.docgen_run as docgen_run
from agentic_devops.config import DatabaseConfig, KnowledgeConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.tools.router import ToolsRouter


def _make_client(tmp_path, pg_url, monkeypatch, *, docgen_enabled):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    monkeypatch.setenv("DEVY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = create_app(
        settings=Settings(
            database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t",
            knowledge=KnowledgeConfig(docgen_enabled=docgen_enabled),
        ),
        provider=object(), router=ToolsRouter(),
    )
    client = TestClient(app)
    token = client.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture()
def client(tmp_path, pool, pg_url, monkeypatch):
    return _make_client(tmp_path, pg_url, monkeypatch, docgen_enabled=True)


def test_endpoints_require_admin(tmp_path, pool, pg_url, monkeypatch):
    client = _make_client(tmp_path, pg_url, monkeypatch, docgen_enabled=True)
    anon = TestClient(client.app)
    assert anon.get("/v1/admin/github/docgen").status_code == 401
    assert anon.post("/v1/admin/github/docgen", json={"repo": "a/b"}).status_code == 401
    assert anon.put("/v1/admin/github/docgen/brief", json={"repo": "a/b", "brief": "x"}).status_code == 401


def test_set_brief_then_listed(client):
    r = client.put("/v1/admin/github/docgen/brief", json={"repo": "acme/widgets", "brief": "market-data feed"})
    assert r.status_code == 200
    assert r.json()["scan_brief"] == "market-data feed"

    listed = client.get("/v1/admin/github/docgen").json()
    row = next(d for d in listed if d["full_name"] == "acme/widgets")
    assert row["scan_brief"] == "market-data feed"
    assert row["components"] == []
    assert row["status"] == "idle"


def test_trigger_disabled_returns_400(tmp_path, pool, pg_url, monkeypatch):
    client = _make_client(tmp_path, pg_url, monkeypatch, docgen_enabled=False)
    r = client.post("/v1/admin/github/docgen", json={"repo": "acme/widgets"})
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"]


def test_trigger_without_account_returns_404(client):
    r = client.post("/v1/admin/github/docgen", json={"repo": "acme/widgets"})
    assert r.status_code == 404


def test_trigger_spawns_background_run(client, monkeypatch):
    # Register an account whose login owns the repo, so resolve_for_repo finds a token.
    created = client.post(
        "/v1/admin/github/accounts", json={"label": "home", "login": "acme", "token": "ghp_x"},
    )
    assert created.status_code == 201

    calls: list[dict] = []

    def fake_run_docgen(gh_client, token, full_name, **kwargs):
        calls.append({"full_name": full_name, "token": token, "force": kwargs.get("force")})

    # The endpoint imports run_docgen from the module at call time — patch it there.
    monkeypatch.setattr(docgen_run, "run_docgen", fake_run_docgen)

    r = client.post("/v1/admin/github/docgen", json={"repo": "acme/widgets", "brief": "b", "force": True})
    assert r.status_code == 200
    assert r.json() == {"repo": "acme/widgets", "started": True}

    # The brief was persisted and the repo now appears in the list.
    listed = client.get("/v1/admin/github/docgen").json()
    row = next(d for d in listed if d["full_name"] == "acme/widgets")
    assert row["scan_brief"] == "b"
