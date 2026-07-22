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
    # the github connector secret is listed, loaded, and editable here in dev
    gh = by_ref["devy/github/home"]
    assert gh["category"] == "github" and gh["loaded"] is True and gh["editable"] is True
    assert gh["env"] is None  # connector tokens are resolved on demand (no env var)


def test_set_and_clear_provider_key(client):
    ref = "devy/provider/anthropic"
    r = client.put("/v1/admin/secrets", json={"ref": ref, "value": "sk-ant-xyz"})
    assert r.status_code == 200 and r.json()["loaded"] is True
    assert {e["ref"]: e for e in client.get("/v1/admin/secrets").json()["secrets"]}[ref]["loaded"] is True

    assert client.delete(f"/v1/admin/secrets?ref={ref}").status_code == 200
    assert {e["ref"]: e for e in client.get("/v1/admin/secrets").json()["secrets"]}[ref]["loaded"] is False


def test_set_connector_secret_from_secrets_tab(client):
    # the PAT for an existing account can be set here (single write-point for values)
    client.post("/v1/admin/github/accounts", json={"label": "home", "login": "me"})
    r = client.put("/v1/admin/secrets", json={"ref": "devy/github/home", "value": "ghp_new"})
    assert r.status_code == 200 and r.json()["loaded"] is True and r.json()["category"] == "github"


def test_set_rejects_unknown_ref(client):
    # only known refs (provider keys + registered connectors) are writable
    assert client.put("/v1/admin/secrets", json={"ref": "devy/github/nope", "value": "x"}).status_code == 400
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


def test_test_dispatch_for_config_mounted_mcp(tmp_path, pool, pg_url, monkeypatch):
    # A config-mounted MCP bearer (settings.mcp_servers, not the DB registry) is set
    # here too, so Test must probe THAT server rather than reporting "no server bound".
    import agentic_devops.proxy.host_mcp_client as hmc
    from agentic_devops.config import MCPServerConfig

    monkeypatch.setattr(hmc.HostMCPClient, "list_tools", lambda self, url, token, ah=None: ["disk", "memory", "cpu"])
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    app = create_app(
        settings=Settings(
            database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t",
            secrets=SecretsConfig(mode="dev"),
            mcp_servers=[MCPServerConfig(name="host", transport="http",
                                         url="http://host-mcp:8780/mcp", secret_ref="devy/mcp/host")],
        ),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    tok = c.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})

    # the catalog names the endpoint and marks it testable
    e = {x["ref"]: x for x in c.get("/v1/admin/secrets").json()["secrets"]}["devy/mcp/host"]
    assert e["testable"] is True and "host-mcp:8780" in e["label"]

    # set the bearer, then Test → probes the config-mounted server (not "no server bound")
    c.put("/v1/admin/secrets", json={"ref": "devy/mcp/host", "value": "sekret"})
    r = c.post("/v1/admin/secrets/test", json={"ref": "devy/mcp/host"})
    assert r.status_code == 200
    assert r.json()["ok"] is True and "3 tools available" in r.json()["detail"]
