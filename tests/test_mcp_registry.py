"""MCP Servers registry (Phase S-4): router ops, tool normalization, admin API."""

from types import SimpleNamespace

import bcrypt
import pytest
from fastapi.testclient import TestClient

from agentic_devops.config import DatabaseConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.tools.base import ToolSpec
from agentic_devops.tools.mcp_source import build_server_tools
from agentic_devops.tools.router import ToolsRouter


def _spec(name, category):
    return ToolSpec(name=name, category=category, description="d", when_to_use="w",
                    input_schema={"type": "object", "properties": {}}, handler=lambda a: "")


# -- router unregister / replace --------------------------------------------
def test_router_unregister_and_replace():
    r = ToolsRouter()
    r.register(_spec("a_x", "a"))
    assert "a_x" in r
    r.register_or_replace(_spec("a_x", "a"))  # no raise on replace
    assert r.unregister("a_x") is True and "a_x" not in r
    assert r.unregister("missing") is False


def test_router_unregister_category():
    r = ToolsRouter()
    r.register(_spec("a_1", "a"))
    r.register(_spec("a_2", "a"))
    r.register(_spec("b_1", "b"))
    assert r.unregister_category("a") == 2
    assert "b_1" in r and "a_1" not in r


# -- tool normalization -----------------------------------------------------
def _server(**kw):
    d = {"id": "s1", "name": "grafana", "allow_writes": False, "category": None}
    d.update(kw)
    s = SimpleNamespace(**d)
    s.tool_category = d["category"] or d["name"]
    return s


def test_build_flags_and_skips_writes_by_default():
    tools = [
        {"name": "query_dashboards", "description": "List dashboards",
         "input_schema": {"type": "object", "properties": {"folder": {"description": "folder id"}}},
         "read_only_hint": True},
        {"name": "delete_dashboard", "description": "Delete a dashboard",
         "input_schema": {"type": "object", "properties": {}}, "read_only_hint": False},
    ]
    specs, write_count = build_server_tools(_server(), tools, store=None, caller=None)
    assert write_count == 1 and len(specs) == 1  # write tool counted but not registered
    s = specs[0]
    assert s.name == "grafana_query_dashboards" and s.category == "grafana"
    assert s.safety_tier == "read-only"
    assert "[grafana]" in s.when_to_use and "folder" in s.when_to_use  # index-first metadata


def test_build_includes_writes_when_opted_in():
    tools = [{"name": "delete_x", "description": "del", "input_schema": {}, "read_only_hint": False}]
    specs, wc = build_server_tools(_server(allow_writes=True), tools, store=None, caller=None)
    assert wc == 1 and len(specs) == 1 and specs[0].safety_tier == "elevated"


def test_handler_dials_on_demand():
    seen = {}

    class Caller:
        def call_tool(self, url, token, name, args, auth_header=None):
            seen.update(url=url, token=token, name=name, args=args, auth_header=auth_header)
            return "ok"

    class Store:
        def resolve(self, sid):
            return SimpleNamespace(url="http://grafana/mcp", token="tok", auth_header=None)

    tools = [{"name": "ping", "description": "p", "input_schema": {}, "read_only_hint": True}]
    specs, _ = build_server_tools(_server(), tools, store=Store(), caller=Caller())
    assert specs[0].handler({"a": 1}) == "ok"
    assert seen == {"url": "http://grafana/mcp", "token": "tok", "name": "ping",
                    "args": {"a": 1}, "auth_header": None}


def test_handler_forwards_custom_auth_header():
    # A server that resolves a non-standard auth header (e.g. the Grafana MCP's
    # X-Grafana-Api-Key) must have it threaded through to the caller.
    seen = {}

    class Caller:
        def call_tool(self, url, token, name, args, auth_header=None):
            seen["auth_header"] = auth_header
            return "ok"

    class Store:
        def resolve(self, sid):
            return SimpleNamespace(url="http://grafana/mcp", token="glsa_x",
                                   auth_header="X-Grafana-Api-Key")

    tools = [{"name": "list_datasources", "description": "d", "input_schema": {}, "read_only_hint": True}]
    specs, _ = build_server_tools(_server(), tools, store=Store(), caller=Caller())
    assert specs[0].handler({}) == "ok"
    assert seen["auth_header"] == "X-Grafana-Api-Key"


def test_auth_headers_builder():
    from agentic_devops.proxy.host_mcp_client import _auth_headers

    assert _auth_headers(None) is None
    assert _auth_headers("t") == {"Authorization": "Bearer t"}
    assert _auth_headers("glsa_x", "X-Grafana-Api-Key") == {"X-Grafana-Api-Key": "glsa_x"}
    assert _auth_headers(None, "X-Grafana-Api-Key") is None  # no token → no header


# -- admin API --------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, pool, pg_url, monkeypatch):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    tok = c.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _stub_mcp(monkeypatch):
    import agentic_devops.proxy.host_mcp_client as hmc
    detail = [
        {"name": "query", "description": "q", "input_schema": {"type": "object", "properties": {}}, "read_only_hint": True},
        {"name": "delete_x", "description": "d", "input_schema": {}, "read_only_hint": False},
    ]
    monkeypatch.setattr(hmc.HostMCPClient, "list_tools_detail", lambda self, u, t, ah=None: detail)
    monkeypatch.setattr(hmc.HostMCPClient, "list_tools", lambda self, u, t, ah=None: ["query", "delete_x"])


def test_endpoints_gated(client):
    assert TestClient(client.app).get("/v1/admin/mcp-servers").status_code == 401


def test_crud_mount_refresh_and_catalog(client, monkeypatch):
    _stub_mcp(monkeypatch)
    r = client.post("/v1/admin/mcp-servers", json={"name": "grafana", "url": "http://grafana:9000/mcp"})
    assert r.status_code == 201
    b = r.json()
    assert b["name"] == "grafana" and b["last_status"] == "reachable"
    assert b["tool_count"] == 2 and b["write_tool_count"] == 1  # one write tool flagged (not registered)
    assert b["secret_ref"] == "devy/mcp/grafana"
    sid = b["id"]

    # reserved name/category rejected
    assert client.post("/v1/admin/mcp-servers", json={"name": "diagnostics", "url": "http://x/mcp"}).status_code == 400

    # the MCP bearer token now appears in the Secrets catalog (settable there)
    cat = client.get("/v1/admin/secrets").json()
    assert any(e["ref"] == "devy/mcp/grafana" and e["category"] == "mcp" for e in cat["secrets"])

    assert client.post(f"/v1/admin/mcp-servers/{sid}/test").json()["status"] == "reachable"
    assert client.post(f"/v1/admin/mcp-servers/{sid}/refresh").json()["tool_count"] == 2

    # disable withdraws tools; status reflects it
    dis = client.patch(f"/v1/admin/mcp-servers/{sid}", json={"enabled": False}).json()
    assert dis["enabled"] is False

    assert client.delete(f"/v1/admin/mcp-servers/{sid}").status_code == 200
    assert client.get("/v1/admin/mcp-servers").json() == []
