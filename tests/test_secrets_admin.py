"""Secrets / Connections admin API (Phase S-2): catalog, set/clear, test dispatch."""

import bcrypt
import pytest
from fastapi.testclient import TestClient

import agentic_devops.proxy.secrets_catalog as secrets_catalog
from agentic_devops.config import DatabaseConfig, SecretsConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.tools.router import ToolsRouter


def _client(tmp_path, pg_url, monkeypatch, *, mode="dev"):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    app = create_app(
        settings=Settings(
            database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t",
            secrets=SecretsConfig(mode=mode),
        ),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    tok = c.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


@pytest.fixture()
def client(tmp_path, pool, pg_url, monkeypatch):
    return _client(tmp_path, pg_url, monkeypatch)


def test_requires_admin(client):
    anon = TestClient(client.app)
    assert anon.get("/v1/admin/secrets").status_code == 401
    assert anon.put("/v1/admin/secrets", json={"ref": "devy/provider/anthropic", "value": "x"}).status_code == 401
    assert anon.post("/v1/admin/secrets/test", json={"ref": "devy/provider/anthropic"}).status_code == 401


def test_catalog_lists_provider_keys_and_connectors(client):
    # a github account contributes a connector secret to the inventory
    client.post("/v1/admin/github/accounts", json={"label": "home", "login": "me", "token": "ghp_x"})

    cat = client.get("/v1/admin/secrets").json()
    assert cat["mode"] == "dev" and cat["writable"] is True
    by_ref = {e["ref"]: e for e in cat["secrets"]}
    # all four provider keys present, editable, not yet loaded
    for svc in ("anthropic", "openai", "tavily", "langsmith"):
        e = by_ref[f"devy/provider/{svc}"]
        assert e["category"] == "provider" and e["editable"] is True and e["loaded"] is False
    # the github connector secret is listed, loaded, but not editable here
    gh = by_ref["devy/github/home"]
    assert gh["category"] == "github" and gh["loaded"] is True and gh["editable"] is False


def test_set_and_clear_provider_key(client):
    ref = "devy/provider/anthropic"
    r = client.put("/v1/admin/secrets", json={"ref": ref, "value": "sk-ant-xyz"})
    assert r.status_code == 200 and r.json()["loaded"] is True
    assert {e["ref"]: e for e in client.get("/v1/admin/secrets").json()["secrets"]}[ref]["loaded"] is True

    assert client.delete(f"/v1/admin/secrets?ref={ref}").status_code == 200
    assert {e["ref"]: e for e in client.get("/v1/admin/secrets").json()["secrets"]}[ref]["loaded"] is False


def test_set_rejects_non_provider_ref(client):
    # connector tokens are edited on their own tab, not here
    assert client.put("/v1/admin/secrets", json={"ref": "devy/github/home", "value": "x"}).status_code == 400
    assert client.put("/v1/admin/secrets", json={"ref": "devy/random/thing", "value": "x"}).status_code == 400


def test_prod_mode_refuses_writes(tmp_path, pool, pg_url, monkeypatch):
    c = _client(tmp_path, pg_url, monkeypatch, mode="prod")
    assert c.get("/v1/admin/secrets").json()["writable"] is False
    assert c.put("/v1/admin/secrets", json={"ref": "devy/provider/anthropic", "value": "x"}).status_code == 403
    assert c.delete("/v1/admin/secrets?ref=devy/provider/anthropic").status_code == 403


def test_test_dispatch_for_provider(client, monkeypatch):
    client.put("/v1/admin/secrets", json={"ref": "devy/provider/tavily", "value": "tvly-abc"})
    seen = {}

    def fake_probe(service, value):
        seen["service"], seen["value"] = service, value
        return True, "valid"

    monkeypatch.setattr(secrets_catalog, "probe_provider", fake_probe)
    r = client.post("/v1/admin/secrets/test", json={"ref": "devy/provider/tavily"})
    assert r.status_code == 200 and r.json() == {"ok": True, "detail": "valid"}
    assert seen == {"service": "tavily", "value": "tvly-abc"}


def test_test_reports_not_set(client):
    r = client.post("/v1/admin/secrets/test", json={"ref": "devy/provider/openai"})
    assert r.json() == {"ok": False, "detail": "not set"}


def test_test_dispatch_for_github(client, monkeypatch):
    client.post("/v1/admin/github/accounts", json={"label": "home", "login": "me", "token": "ghp_x"})

    def fake_gh(clientobj, value):
        return True, f"authenticated as octo ({value[:4]})"

    monkeypatch.setattr(secrets_catalog, "probe_github", fake_gh)
    r = client.post("/v1/admin/secrets/test", json={"ref": "devy/github/home"})
    assert r.status_code == 200 and r.json()["ok"] is True and "authenticated" in r.json()["detail"]
