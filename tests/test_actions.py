"""Guarded mutating actions (G-2b): store lifecycle, executor, propose tool,
fail-closed enable decision, and the tier-gated approve/deny endpoints."""

import uuid

import pytest

from agentic_devops.proxy.actions import (
    ACTION_CATALOG,
    ActionExecutor,
    ActionStore,
    guarded_actions_status,
)
from agentic_devops.tools.builtin.actions import build_request_action_tool


@pytest.fixture()
def astore(pool):
    return ActionStore(pool)


def _seed(store, **kw):
    base = dict(
        verb="restart_service", args={"name": "alloy"}, rationale="crash loop",
        reversibility="brief restart", host="host", session_id="s1", user_id="u1",
        ttl_seconds=900,
    )
    base.update(kw)
    return store.create(**base)


# -- store lifecycle --------------------------------------------------------
def test_create_and_get(astore):
    a = _seed(astore)
    assert a.status == "proposed" and a.verb == "restart_service"
    assert a.target == "alloy" and a.label == "Restart service"
    got = astore.get(a.id)
    assert got is not None and got.id == a.id and got.rationale == "crash loop"


def test_list_by_session_and_status(astore):
    sx, sy = uuid.uuid4().hex, uuid.uuid4().hex  # unique per run (DB persists across runs)
    a = _seed(astore, session_id=sx)
    _seed(astore, session_id=sy)
    assert {x.id for x in astore.list(session_id=sx)} == {a.id}
    assert all(x.status == "proposed" for x in astore.list(status="proposed"))
    assert a.id not in {x.id for x in astore.list(status="executed")}


def test_deny_is_compare_and_set(astore):
    a = _seed(astore)
    assert astore.deny(a.id, "boss") is True
    assert astore.get(a.id).status == "denied"
    assert astore.deny(a.id, "boss") is False  # already decided → no-op


def test_claim_for_execution_is_single_winner(astore):
    a = _seed(astore)
    claimed = astore.claim_for_execution(a.id, "boss")
    assert claimed is not None and claimed.status == "executing" and claimed.decided_by == "boss"
    # a second approver loses the CAS → cannot double-execute
    assert astore.claim_for_execution(a.id, "other") is None


def test_claim_rejects_expired(astore, pool):
    a = _seed(astore)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE pending_actions SET expires_at = now() - interval '1 minute' WHERE id=%s",
            (a.id,),
        )
    assert astore.claim_for_execution(a.id, "boss") is None  # past TTL → not approvable
    assert astore.get(a.id).status == "proposed"             # untouched


def test_deny_after_claim_is_noop(astore):
    a = _seed(astore)
    astore.claim_for_execution(a.id, "boss")
    assert astore.deny(a.id, "boss") is False  # only 'proposed' can be denied


def test_record_result(astore):
    a = _seed(astore)
    astore.claim_for_execution(a.id, "boss")
    final = astore.record_result(a.id, status="executed", result="$ ran\n\nok", returncode=0)
    assert final.status == "executed" and final.returncode == 0 and "ok" in final.result
    assert final.executed_at


# -- executor ---------------------------------------------------------------
class _FakeCaller:
    def __init__(self, ret):
        self.ret = ret
        self.calls = []

    def call_tool(self, url, token, name, args, auth_header=None):
        self.calls.append((url, token, name, args, auth_header))
        return self.ret


def test_executor_runs_and_reports_success(astore):
    a = _seed(astore)
    caller = _FakeCaller("$ systemctl restart alloy\n\nok")
    ex = ActionExecutor(caller, lambda h: ("http://host/mcp", "tok", None))
    result, rc = ex.execute(a)
    assert rc == 0 and "ok" in result
    assert caller.calls[0][2] == "restart_service" and caller.calls[0][3] == {"name": "alloy"}


def test_executor_reports_failure_on_error_prefix(astore):
    a = _seed(astore)
    ex = ActionExecutor(_FakeCaller("ERROR: unit not found"), lambda h: ("u", "t", None))
    result, rc = ex.execute(a)
    assert rc == 1 and result.startswith("ERROR")


def test_executor_unresolved_target(astore):
    a = _seed(astore)
    ex = ActionExecutor(_FakeCaller("unused"), lambda h: None)
    result, rc = ex.execute(a)
    assert rc is None and "could not resolve" in result


# -- propose tool -----------------------------------------------------------
def test_request_action_unknown_verb(astore):
    tool = build_request_action_tool(astore, ttl_seconds=900)
    out = tool.handler({"verb": "rm_rf", "rationale": "x"}, {})
    assert out.startswith("ERROR") and "unknown action" in out


def test_request_action_requires_rationale(astore):
    tool = build_request_action_tool(astore, ttl_seconds=900)
    out = tool.handler({"verb": "prune_images"}, {})
    assert out.startswith("ERROR") and "rationale" in out


def test_request_action_requires_params(astore):
    tool = build_request_action_tool(astore, ttl_seconds=900)
    out = tool.handler({"verb": "restart_service", "rationale": "why"}, {})
    assert out.startswith("ERROR") and "name" in out


def test_request_action_happy_path_creates_proposal(astore):
    tool = build_request_action_tool(astore, ttl_seconds=900, default_host="host")
    sid = uuid.uuid4().hex  # unique per run (persistent test DB)
    out = tool.handler(
        {"verb": "restart_service", "args": {"name": "alloy"}, "rationale": "crash loop"},
        {"session_id": sid, "user_id": "u1"},
    )
    assert "AWAITING HUMAN APPROVAL" in out and "Restart service" in out
    # a proposed row exists, stamped with provenance from context (not model args)
    rows = astore.list(session_id=sid)
    assert len(rows) == 1 and rows[0].status == "proposed" and rows[0].user_id == "u1"
    assert rows[0].args == {"name": "alloy"} and rows[0].host == "host"


def test_request_action_is_elevated_propose_only_seam(astore):
    t = build_request_action_tool(astore, ttl_seconds=900)
    assert t.category == "actions" and t.safety_tier == "elevated" and t.wants_context is True
    # the tool only proposes — its verbs are exactly the curated Tier-A catalog
    assert set(t.input_schema["properties"]["verb"]["enum"]) == set(ACTION_CATALOG)


# -- fail-closed enable decision --------------------------------------------
def test_guarded_actions_status_matrix():
    assert guarded_actions_status(enabled=False, allow_insecure_dev=False, auth_mode="jwt")[0] is False
    assert guarded_actions_status(enabled=True, allow_insecure_dev=False, auth_mode="jwt")[0] is True
    assert guarded_actions_status(enabled=True, allow_insecure_dev=True, auth_mode="password")[0] is True
    off, reason = guarded_actions_status(enabled=True, allow_insecure_dev=False, auth_mode="password")
    assert off is False and "jwt" in reason  # fail-closed without real identity


# -- endpoints --------------------------------------------------------------
def test_actions_endpoints_flow(pool, pg_url, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agentic_devops.config import (
        ActionsConfig, DatabaseConfig, MCPServerConfig, Settings,
    )
    from agentic_devops.proxy.app import create_app
    from agentic_devops.tools.router import ToolsRouter

    # Stub the host-MCP caller so approve executes without a real sidecar.
    monkeypatch.setattr(
        "agentic_devops.proxy.host_mcp_client.HostMCPClient.call_tool",
        lambda self, url, token, name, args, auth_header=None: f"$ ran {name}\n\nok",
    )
    app = create_app(
        settings=Settings(
            database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t",
            actions=ActionsConfig(enabled=True, allow_insecure_dev=True),
            mcp_servers=[MCPServerConfig(
                name="host", category="host", transport="http",
                url="http://fake/mcp", token="tok",
            )],
        ),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    a = ActionStore(pool).create(
        verb="restart_service", args={"name": "alloy"}, rationale="crash loop",
        reversibility="brief restart", host="host", session_id="s1", user_id="u1",
        ttl_seconds=900,
    )
    # list shows the proposal
    listed = c.get("/v1/actions", params={"session_id": "s1"}).json()
    assert any(x["id"] == a.id and x["status"] == "proposed" for x in listed)
    # approve → executes on the (stubbed) host, records who approved
    r = c.post(f"/v1/actions/{a.id}/approve", headers={"X-User-Id": "boss"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "executed" and body["decided_by"] == "boss"
    assert "ran restart_service" in body["result"]
    # double-approve → 409 (already executed); unknown id → 404
    assert c.post(f"/v1/actions/{a.id}/approve").status_code == 409
    assert c.post("/v1/actions/nope/approve").status_code == 404


def test_actions_endpoints_503_when_disabled(pool, pg_url, tmp_path):
    from fastapi.testclient import TestClient

    from agentic_devops.config import DatabaseConfig, Settings
    from agentic_devops.proxy.app import create_app
    from agentic_devops.tools.router import ToolsRouter

    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    assert c.get("/v1/actions").status_code == 503
    assert c.post("/v1/actions/x/approve").status_code == 503
