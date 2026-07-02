"""Admin control-plane auth (Phase 9a): login, token-gated endpoint, disabled mode."""

import bcrypt
import pytest
from fastapi.testclient import TestClient

from agentic_devops.config import DatabaseConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.tools.router import ToolsRouter


def _app(pg_url, tmp_path):
    return create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(),  # admin endpoints never touch the provider
        router=ToolsRouter(),
    )


@pytest.fixture()
def admin_client(tmp_path, pool, pg_url, monkeypatch):
    pw_hash = bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode()
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", pw_hash)
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)  # realistic length (≥32 bytes)
    return TestClient(_app(pg_url, tmp_path))


def test_login_succeeds_and_issues_token(admin_client):
    r = admin_client.post("/v1/admin/login", json={"password": "hunter2"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"] and body["token_type"] == "bearer" and body["expires_in"] > 0


def test_login_rejects_wrong_password(admin_client):
    assert admin_client.post("/v1/admin/login", json={"password": "nope"}).status_code == 401


def test_me_requires_a_valid_token(admin_client):
    # no token / bad token → 401
    assert admin_client.get("/v1/admin/me").status_code == 401
    assert admin_client.get(
        "/v1/admin/me", headers={"Authorization": "Bearer not-a-token"}
    ).status_code == 401

    token = admin_client.post("/v1/admin/login", json={"password": "hunter2"}).json()["token"]
    ok = admin_client.get("/v1/admin/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    # RBAC-1: password mode grants the admin role
    assert ok.json()["authenticated"] is True and "admin" in ok.json()["roles"]
    assert ok.json()["source"] == "password"


def test_admin_plane_disabled_without_secrets(tmp_path, pool, pg_url, monkeypatch):
    monkeypatch.delenv("DEVY_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("DEVY_ADMIN_SECRET", raising=False)
    client = TestClient(_app(pg_url, tmp_path))
    assert client.post("/v1/admin/login", json={"password": "x"}).status_code == 503
    assert client.get("/v1/admin/me").status_code == 503
