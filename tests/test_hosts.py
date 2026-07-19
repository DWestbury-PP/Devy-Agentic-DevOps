"""Host registry (Phase 9b): store + secrets resolution, the host tools,
and the admin CRUD endpoints."""

import bcrypt
import pytest
from fastapi.testclient import TestClient

from agentic_devops.config import DatabaseConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.proxy.hosts import HostStore
from agentic_devops.tools.builtin.hosts import build_host_tools
from agentic_devops.tools.router import ToolsRouter


@pytest.fixture()
def store(pool, secrets):
    return HostStore(pool, secrets)


# ---- store + secrets backend + resolution -----------------------------------

def test_create_get_list_with_encrypted_token(store):
    h = store.create(
        {"fqdn": "web-1.example.com", "private_ip": "10.0.0.5", "aws_region": "us-east-1"},
        token="secret-token",
    )
    assert h.id and h.fqdn == "web-1.example.com" and h.has_token is True
    assert store.get(h.id).aws_region == "us-east-1"
    assert any(x.id == h.id for x in store.list())


def test_resolve_builds_endpoint_and_decrypts_token(store):
    store.create(
        {"fqdn": "web-1", "private_ip": "10.0.0.5", "mcp_port": 8780, "address_preference": "private_ip"},
        token="tok-123",
    )
    rh = store.resolve("web-1")
    assert rh.url == "https://10.0.0.5:8780/mcp"
    assert rh.token == "tok-123"  # fetched from the secrets manager


def test_address_preference(store):
    store.create(
        {"fqdn": "h2.example.com", "private_ip": "10.0.0.6", "public_ip": "1.2.3.4",
         "address_preference": "public_ip", "mcp_port": 9000},
    )
    assert store.resolve("h2.example.com").url == "https://1.2.3.4:9000/mcp"


def test_inactive_hosts_not_resolved_by_default(store):
    store.create({"fqdn": "h3", "active": False})
    assert store.resolve("h3") is None
    assert store.resolve("h3", active_only=False) is not None


def test_update_and_token_lifecycle(store):
    h = store.create({"fqdn": "h4", "private_ip": "10.0.0.1"}, token="old")
    store.update(h.id, {"active": False})
    assert store.get(h.id).active is False
    store.update(h.id, {}, token="new", set_token=True)
    assert store.resolve("h4", active_only=False).token == "new"
    store.update(h.id, {}, token=None, set_token=True)  # clear
    assert store.get(h.id).has_token is False


def test_delete(store):
    h = store.create({"fqdn": "h5"})
    store.delete(h.id)
    assert store.get(h.id) is None


# ---- the host tools (with a fake MCP caller) --------------------------------

class FakeCaller:
    def __init__(self):
        self.calls = []

    def call_tool(self, url, token, name, args):
        self.calls.append((url, token, name, args))
        return f"ran {name} on {url}"

    def list_tools(self, url, token):
        return ["disk", "memory", "docker_ps"]


def test_host_tools_lookup_and_run(store):
    store.create({"fqdn": "web-1", "private_ip": "10.0.0.5", "aws_region": "us-east-1"}, token="t")
    caller = FakeCaller()
    tools = {t.name: t for t in build_host_tools(store, caller)}

    out = tools["host_details_lookup"].handler({"query": "web"})
    assert "web-1" in out and "available checks" in out and "disk" in out

    r = tools["run_host_check"].handler({"host": "web-1", "check": "disk"})
    assert "ran disk on https://10.0.0.5:8780/mcp" in r
    assert caller.calls[-1][2] == "disk"

    assert "No active host" in tools["run_host_check"].handler({"host": "nope", "check": "disk"})

    batch = tools["run_host_checks"].handler({"host": "web-1", "checks": ["disk", "memory"]})
    assert "### disk" in batch and "### memory" in batch
    assert store.get(store.resolve("web-1").host.id).last_status == "reachable"


# ---- admin CRUD endpoints ---------------------------------------------------

@pytest.fixture()
def admin_client(tmp_path, pool, pg_url, monkeypatch):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    client = TestClient(app)
    client.headers.update(
        {"Authorization": f"Bearer {client.post('/v1/admin/login', json={'password': 'pw'}).json()['token']}"}
    )
    return client


def test_host_crud_endpoints(admin_client):
    # gated
    bare = TestClient(admin_client.app)
    assert bare.get("/v1/admin/hosts").status_code == 401

    created = admin_client.post(
        "/v1/admin/hosts",
        json={"fqdn": "api-1.example.com", "private_ip": "10.0.0.9", "aws_region": "us-east-1",
              "token": "sekret"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["fqdn"] == "api-1.example.com" and body["has_token"] is True
    assert "token" not in body  # never returned
    hid = body["id"]

    assert any(h["id"] == hid for h in admin_client.get("/v1/admin/hosts").json())
    assert admin_client.get(f"/v1/admin/hosts/{hid}").json()["fqdn"] == "api-1.example.com"
    assert admin_client.patch(f"/v1/admin/hosts/{hid}", json={"active": False}).json()["active"] is False
    assert admin_client.delete(f"/v1/admin/hosts/{hid}").status_code == 200
    assert admin_client.get(f"/v1/admin/hosts/{hid}").status_code == 404


# ---- mounted MCP servers (built-in hosts from config, S-4) -------------------
def _mounted_client(tmp_path, pg_url, monkeypatch, mcp_servers):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t",
                          mcp_servers=mcp_servers),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    tok = c.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def test_mcp_mounts_empty_when_none_configured(tmp_path, pool, pg_url, monkeypatch):
    c = _mounted_client(tmp_path, pg_url, monkeypatch, [])
    assert c.get("/v1/admin/mcp-mounts").json() == []
    assert TestClient(c.app).get("/v1/admin/mcp-mounts").status_code == 401  # gated


def test_mcp_mounts_lists_reachable_host(tmp_path, pool, pg_url, monkeypatch):
    import agentic_devops.proxy.host_mcp_client as hmc
    from agentic_devops.config import MCPServerConfig

    monkeypatch.setattr(hmc.HostMCPClient, "list_tools", lambda self, url, token: ["df", "free"])
    c = _mounted_client(
        tmp_path, pg_url, monkeypatch,
        [MCPServerConfig(name="host", transport="http", url="http://host-mcp:8780/mcp", token="t")],
    )
    data = c.get("/v1/admin/mcp-mounts").json()
    assert len(data) == 1
    m = data[0]
    assert m["name"] == "host" and m["address"] == "host-mcp:8780"
    assert m["reachable"] is True and m["checks"] == 2


def test_mcp_mounts_probe_resolves_secret_ref_from_vault(tmp_path, pool, pg_url, monkeypatch):
    """The reachability probe must resolve a `secret_ref` bearer from the vault,
    not the inline `s.token` (which is only populated as a boot side-effect). With
    the router passed in, that boot mutation never runs — so a mount configured
    purely via `secret_ref` would falsely report unreachable if the probe trusted
    `s.token`. The fake `list_tools` accepts ONLY the vault-resolved token."""
    import agentic_devops.proxy.app as app_mod
    import agentic_devops.proxy.host_mcp_client as hmc
    from agentic_devops.config import MCPServerConfig
    from tests.conftest import make_fake_secrets

    vault = make_fake_secrets(writable=True)
    vault.set("devy/mcp/host", "bearer-from-vault")
    monkeypatch.setattr(app_mod, "build_secrets_provider", lambda settings: vault)

    def _list(self, url, token):
        assert token == "bearer-from-vault", f"probe passed the wrong token: {token!r}"
        return ["df", "free", "ps"]

    monkeypatch.setattr(hmc.HostMCPClient, "list_tools", _list)
    c = _mounted_client(
        tmp_path, pg_url, monkeypatch,
        # secret_ref only, NO inline token — the fragile configuration
        [MCPServerConfig(name="host", transport="http",
                         url="http://host-mcp:8780/mcp", secret_ref="devy/mcp/host")],
    )
    m = c.get("/v1/admin/mcp-mounts").json()[0]
    assert m["reachable"] is True and m["checks"] == 3
