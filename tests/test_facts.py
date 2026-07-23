"""Evolving fact tier — bi-temporal supersession + hybrid/as_of retrieval (Phase A).

Ported from the validated standalone prototype. Uses a deterministic bag-of-words
fake embedder (no network) and the live test Postgres (`pool` fixture). The three
load-bearing scenarios: supersession keeps exactly one current fact and preserves
history; slotless facts coexist; and concurrent same-slot writers serialize to a
single current fact (advisory lock + partial-unique invariant).
"""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.facts import FactStore
from agentic_devops.tools.builtin.facts import (
    build_memory_add_tool,
    build_recall_facts_tool,
)

_DIM = 64


def _fake_embed(texts, model, api_base):
    vectors = []
    for text in texts:
        vec = [0.0] * _DIM
        for token in text.lower().split():
            h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
            vec[h % _DIM] += 1.0
        vectors.append(vec)
    return vectors


@pytest.fixture()
def embedder():
    return Embedder(model="fake", embed_fn=_fake_embed)


@pytest.fixture()
def store(pool, embedder):
    return FactStore(pool, embedder)


# -- scenario 1: supersession ----------------------------------------------
def test_supersession_keeps_one_current_and_preserves_history(store):
    first = store.add_fact(
        "pricing service exposes port 8080",
        source="test", subject="svc:pricing", attribute="port",
    )
    assert first.superseded == []  # first fact in the slot retires nothing

    second = store.add_fact(
        "pricing service exposes port 9090",
        source="test", subject="svc:pricing", attribute="port",
    )
    # The contradicting deposit reports retiring the first.
    assert second.superseded == [first.memory_id]

    # Exactly one currently-true fact in the slot, and it's the new one.
    current = store.current_for_slot("svc:pricing", "port")
    assert current is not None and current.memory_id == second.memory_id
    assert "9090" in current.content

    # The old fact is retired (valid_to set) and linked, NOT deleted.
    old = store.get(first.memory_id)
    assert old is not None and old.valid_to is not None
    assert store.superseded_by(first.memory_id) == second.memory_id

    # Two rows of history total, one current.
    assert len(store.history_for_slot("svc:pricing", "port")) == 2
    assert store.count(current_only=True) == 1


# -- scenario 2: slotless coexistence --------------------------------------
def test_slotless_facts_never_supersede(store):
    a = store.add_fact("the team prefers blue-green deploys", source="test")
    b = store.add_fact("the team prefers canary deploys", source="test")
    assert a.superseded == [] and b.superseded == []
    # Both coexist — no slot, so no contradiction.
    assert store.count(current_only=True) == 2


def test_partial_slot_does_not_supersede(store):
    # subject without attribute (or vice versa) is treated as slotless.
    a = store.add_fact("orphan subject fact", source="test", subject="svc:x")
    b = store.add_fact("another orphan subject fact", source="test", subject="svc:x")
    assert a.superseded == [] and b.superseded == []
    assert store.count(current_only=True) == 2


# -- scenario 3: concurrent same-slot race ---------------------------------
def test_concurrent_same_slot_writers_leave_one_current(store):
    # Seed one fact, then fire N writers at the same slot simultaneously.
    store.add_fact("initial value v0", source="seed", subject="svc:race", attribute="state")

    n = 5
    barrier = threading.Barrier(n)

    def writer(i: int):
        barrier.wait()  # maximize contention on the advisory lock
        return store.add_fact(
            f"concurrent value v{i + 1}",
            source=f"writer-{i}", subject="svc:race", attribute="state",
        )

    with ThreadPoolExecutor(max_workers=n) as pool_exec:
        results = list(pool_exec.map(writer, range(n)))

    # All writers succeeded (no unique-violation crash) ...
    assert all(r.memory_id for r in results)
    # ... and the invariant held: exactly one currently-true fact in the slot.
    assert store.current_for_slot("svc:race", "state") is not None
    rows = store.history_for_slot("svc:race", "state")
    assert len([f for f in rows if f.is_current]) == 1
    assert len(rows) == n + 1  # seed + N, all preserved


# -- read path: as_of + hybrid ---------------------------------------------
def test_as_of_reconstructs_prior_belief(store):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store.add_fact(
        "pricing exposes port 8080", source="test",
        subject="svc:pricing", attribute="port", valid_from=t0,
    )
    store.add_fact(
        "pricing exposes port 9090", source="test",
        subject="svc:pricing", attribute="port", valid_from=t1,
    )

    # Current belief.
    now_hits = store.search_facts("pricing port", k=3)
    assert now_hits and "9090" in now_hits[0].fact.content

    # Belief as-of a moment between the two deposits → the old value.
    mid = datetime(2026, 3, 1, tzinfo=timezone.utc)
    past_hits = store.search_facts("pricing port", k=3, as_of=mid)
    assert past_hits and all(h.fact.content for h in past_hits)
    assert "8080" in past_hits[0].fact.content
    assert all("9090" not in h.fact.content for h in past_hits)


def test_search_facts_hybrid_matches_exact_token(store):
    # The full-text arm catches an exact token the bag-of-words vector also has,
    # but the point is hybrid returns it with a "keyword" source tag.
    store.add_fact(
        "the gateway listens on host edge-gw-01 for ingress", source="test",
        subject="host:edge-gw-01", attribute="role",
    )
    store.add_fact("unrelated note about logging", source="test")

    hits = store.search_facts("edge-gw-01", k=5)
    assert hits and "edge-gw-01" in hits[0].fact.content
    assert "keyword" in hits[0].sources


def test_subject_filter_scopes_results(store):
    store.add_fact("alpha runs on 8080", source="t", subject="svc:alpha", attribute="port")
    store.add_fact("beta runs on 9090", source="t", subject="svc:beta", attribute="port")
    hits = store.search_facts("port", k=5, subject="svc:beta")
    assert hits and all(h.fact.subject == "svc:beta" for h in hits)


# -- tools: memory_add (write-back seam) + recall_facts ---------------------
def test_memory_add_tool_stamps_provenance_from_context(store):
    tool = build_memory_add_tool(store)
    out = tool.handler(
        {"content": "pricing exposes port 9090", "subject": "svc:pricing", "attribute": "port"},
        {"user_id": "darrell", "session_id": "sess-1"},
    )
    assert "Stored fact" in out and "svc:pricing" in out

    fact = store.current_for_slot("svc:pricing", "port")
    assert fact is not None
    # Provenance comes from context, never the model's args.
    assert fact.source == "darrell"
    assert fact.metadata.get("session_id") == "sess-1"


def test_memory_add_then_supersede_reports_via_tool(store):
    tool = build_memory_add_tool(store)
    ctx = {"user_id": "u", "session_id": "s"}
    tool.handler({"content": "port is 8080", "subject": "svc:x", "attribute": "port"}, ctx)
    out = tool.handler({"content": "port is 9090", "subject": "svc:x", "attribute": "port"}, ctx)
    assert "superseded" in out
    assert store.count(current_only=True) == 1


def test_memory_add_requires_content(store):
    tool = build_memory_add_tool(store)
    assert tool.handler({}, {}).startswith("ERROR")


def test_recall_facts_tool_formats_and_filters(store):
    store.add_fact("pricing exposes port 9090", source="t", subject="svc:pricing", attribute="port")
    tool = build_recall_facts_tool(store)
    out = tool.handler({"query": "pricing port"})
    assert "svc:pricing" in out and "9090" in out and "current" in out


def test_recall_facts_tool_rejects_bad_as_of(store):
    tool = build_recall_facts_tool(store)
    out = tool.handler({"query": "x", "as_of": "not-a-date"})
    assert out.startswith("ERROR") and "as_of" in out


def test_recall_facts_tool_requires_query(store):
    tool = build_recall_facts_tool(store)
    assert tool.handler({}).startswith("ERROR")


def test_fact_tools_share_knowledge_category(store):
    # The family seam: both fact tools sit in `knowledge` so a category-scoped
    # find_tools returns the whole durable-knowledge surface together.
    assert build_recall_facts_tool(store).category == "knowledge"
    assert build_memory_add_tool(store).category == "knowledge"
    # memory_add takes request context (provenance); recall_facts does not.
    assert build_memory_add_tool(store).wants_context is True
    assert build_recall_facts_tool(store).wants_context is False


# -- admin hygiene: list / retract / delete ---------------------------------
def test_retract_makes_fact_non_current_but_keeps_history(store):
    f = store.add_fact("panel uid is old-123", source="test",
                       subject="grafana:cpu", attribute="uid")
    assert store.current_for_slot("grafana:cpu", "uid") is not None

    assert store.retract(f.memory_id) is True
    # no longer current, slot is now empty
    assert store.current_for_slot("grafana:cpu", "uid") is None
    # but the row is preserved (bi-temporal): still fetchable, with valid_to set
    kept = store.get(f.memory_id)
    assert kept is not None and kept.valid_to is not None and kept.is_current is False
    # retracting again is a no-op
    assert store.retract(f.memory_id) is False


def test_delete_removes_row_and_clears_superseded_pointers(store):
    a = store.add_fact("region is us-east-1", source="test",
                       subject="svc:del", attribute="region")
    b = store.add_fact("region is us-west-2", source="test",
                       subject="svc:del", attribute="region")
    assert b.superseded == [a.memory_id]  # b retired a (a.superseded_by = b)

    # hard-delete the current fact b — its superseded_by pointer from a must be
    # cleared first so the FK holds (no crash)
    assert store.delete(b.memory_id) is True
    assert store.get(b.memory_id) is None
    assert store.get(a.memory_id) is not None            # a survives
    assert store.superseded_by(a.memory_id) is None       # pointer cleared

    assert store.delete("nonexistent-id") is False


def test_list_facts_scoped_and_current_first(store):
    store.add_fact("v-current", source="test", subject="svc:list", attribute="k")
    store.add_fact("v-newer", source="test", subject="svc:list", attribute="k")  # retires the first
    rows = store.list_facts(subject="svc:list", current_only=False)
    assert len(rows) == 2 and rows[0].is_current  # current sorts first
    only_current = store.list_facts(subject="svc:list", current_only=True)
    assert len(only_current) == 1 and only_current[0].content == "v-newer"


# -- admin endpoints: gating + graceful-disabled -----------------------------
def test_admin_facts_endpoints_gated_and_graceful(pool, pg_url, tmp_path, monkeypatch):
    import bcrypt
    from fastapi.testclient import TestClient

    from agentic_devops.config import DatabaseConfig, Settings
    from agentic_devops.proxy.app import create_app
    from agentic_devops.tools.router import ToolsRouter

    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    # router passed in → the fact tier isn't wired in this app, so endpoints
    # degrade gracefully (enabled: false) rather than erroring.
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    assert c.get("/v1/admin/facts").status_code == 401  # admin-gated
    tok = c.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})

    body = c.get("/v1/admin/facts").json()
    assert body["enabled"] is False and body["facts"] == []
    assert c.post("/v1/admin/facts/x/retract").status_code == 404
    assert c.delete("/v1/admin/facts/x").status_code == 404
