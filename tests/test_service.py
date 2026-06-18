"""HTTP service smoke tests via FastAPI TestClient with an injected fake
provider (no litellm, no network, no API keys)."""

import pytest
from fastapi.testclient import TestClient

from agentic_devops.config import DatabaseConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.proxy.providers import ProviderResponse
from agentic_devops.tools.builtin.diagnostics import build_diagnostics_tool
from agentic_devops.tools.router import ToolsRouter


class FakeProvider:
    """Always answers directly (no tool use) for a deterministic smoke test."""

    def complete(self, messages, tier, tools=None):
        return ProviderResponse(text="All healthy.")

    def stream(self, messages, tier, tools=None):
        for piece in ["All ", "healthy."]:
            yield {"type": "delta", "text": piece}
        return ProviderResponse(text="All healthy.")


@pytest.fixture()
def client(tmp_path, pool, pg_url):
    settings = Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "traces")
    router = ToolsRouter()
    router.register(build_diagnostics_tool())
    app = create_app(settings=settings, provider=FakeProvider(), router=router)
    return TestClient(app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["default_tier"] == "balanced"


def test_tiers_hide_concrete_model(client):
    r = client.get("/v1/tiers")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()}
    assert {"fast", "balanced", "deep"} <= names
    # The concrete model string must NOT leak to clients.
    assert all("model" not in t for t in r.json())


def test_tools_lists_diagnostics(client):
    r = client.get("/v1/tools")
    assert r.status_code == 200
    assert any(t["name"] == "host_diagnostics" for t in r.json())


def test_complete_returns_markdown(client):
    r = client.post("/v1/complete", json={"prompt": "is the disk ok?"})
    assert r.status_code == 200
    assert r.json()["markdown"] == "All healthy."


def test_complete_respects_max_chars(client):
    r = client.post("/v1/complete", json={"prompt": "hi", "max_chars": 4})
    assert len(r.json()["markdown"]) <= 5  # 4 chars + ellipsis


def test_unknown_tier_is_rejected(client):
    r = client.post("/v1/complete", json={"prompt": "hi", "tier": "ludicrous"})
    assert r.status_code == 400


def test_chat_streams_sse(client):
    with client.stream("POST", "/v1/chat", json={"message": "is the disk ok?"}) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "session" in body  # the up-front session event
    assert "delta" in body
    assert "healthy." in body


def test_sessions_persist_and_list_for_user(client):
    # A turn tagged with a user_id should be recallable via GET /v1/sessions.
    r = client.post(
        "/v1/complete",
        json={"prompt": "remember this", "session_id": "sess-xyz", "user_id": "alice"},
    )
    assert r.status_code == 200
    assert r.json()["session_id"] == "sess-xyz"

    listed = client.get("/v1/sessions", params={"user_id": "alice"})
    assert listed.status_code == 200
    rows = listed.json()
    assert any(s["id"] == "sess-xyz" and s["preview"] == "remember this" for s in rows)

    # Listing requires an identity.
    assert client.get("/v1/sessions").status_code == 400


def test_session_detail_returns_display_transcript(client):
    client.post(
        "/v1/complete",
        json={"prompt": "is the disk ok?", "session_id": "sess-detail", "user_id": "alice"},
    )
    r = client.get("/v1/sessions/sess-detail")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "sess-detail"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]  # faithful prompt + answer
    assert body["messages"][0]["content"] == "is the disk ok?"

    assert client.get("/v1/sessions/nope-nope").status_code == 404


def test_auto_title_rename_and_delete(client):
    client.post(
        "/v1/complete",
        json={"prompt": "hello", "session_id": "sx", "user_id": "bob"},
    )
    # A title is auto-generated on the first exchange (via the fast tier).
    rows = client.get("/v1/sessions", params={"user_id": "bob"}).json()
    item = next(s for s in rows if s["id"] == "sx")
    assert item["title"]  # non-empty

    # Rename.
    r = client.patch("/v1/sessions/sx", json={"title": "Renamed Thread"})
    assert r.status_code == 200
    assert client.get("/v1/sessions/sx").json()["title"] == "Renamed Thread"
    assert client.patch("/v1/sessions/sx", json={"title": "   "}).status_code == 400

    # Delete.
    assert client.delete("/v1/sessions/sx").status_code == 200
    assert client.get("/v1/sessions/sx").status_code == 404
