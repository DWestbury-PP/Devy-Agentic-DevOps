"""Conversation memory store + recall_history tool (Phase 8).

Uses a deterministic bag-of-words fake embedder (no network) and the live test
Postgres (pool fixture)."""

import hashlib

import pytest

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.history import ConversationMemoryStore
from agentic_devops.tools.builtin.recall import build_recall_history_tool

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
    return ConversationMemoryStore(pool, embedder)


def test_add_and_search_ranks_by_similarity(store):
    store.add_exchange("s1", "u", 0, "the database connection pool was exhausted at 20 of 20")
    store.add_exchange("s1", "u", 1, "we increased the worker memory limit to fix the oom")
    hits = store.search("database connection pool exhausted", user_id="u", k=2)
    assert hits[0].text.startswith("the database connection pool")
    assert hits[0].score >= hits[1].score


def test_scope_by_session_and_user(store):
    store.add_exchange("s1", "alice", 0, "alpha pool exhausted")
    store.add_exchange("s2", "alice", 0, "beta memory limit")
    store.add_exchange("s3", "bob", 0, "gamma disk full")

    all_alice = store.search("pool", user_id="alice", k=10)
    assert {h.session_id for h in all_alice} == {"s1", "s2"}  # cross-conversation, alice only

    this_only = store.search("pool", session_id="s1", k=10)
    assert {h.session_id for h in this_only} == {"s1"}  # current conversation only

    others = store.search("pool", user_id="alice", k=10, exclude_session="s1")
    assert {h.session_id for h in others} == {"s2"}  # excludes the current one


def test_count_and_delete_session(store):
    store.add_exchange("s1", "u", 0, "one")
    store.add_exchange("s1", "u", 1, "two")
    store.add_exchange("s2", "u", 0, "three")
    assert store.count() == 3
    store.delete_session("s1")
    assert store.count() == 1


def test_idempotent_on_session_turn(store):
    store.add_exchange("s1", "u", 0, "first")
    store.add_exchange("s1", "u", 0, "second")  # same (session, turn) → same id
    assert store.count() == 1
    assert store.search("second", session_id="s1", k=1)[0].text == "second"


# ---- recall_history tool ----------------------------------------------------

def test_recall_tool_scopes_via_context(store):
    store.add_exchange("s1", "alice", 0, "pool exhausted on web-1")
    store.add_exchange("s2", "alice", 0, "latency spike on api-2")
    store.add_exchange("s9", "mallory", 0, "secret on mallory-box")
    tool = build_recall_history_tool(store)
    assert tool.wants_context is True

    # Cross-conversation for alice; never sees mallory's rows.
    out = tool.handler({"query": "pool exhausted"}, {"user_id": "alice", "session_id": "s1"})
    assert "web-1" in out and "mallory-box" not in out

    # scope=this restricts to the current conversation.
    out_this = tool.handler(
        {"query": "latency", "scope": "this"}, {"user_id": "alice", "session_id": "s2"}
    )
    assert "api-2" in out_this

    assert tool.handler({"query": "  "}, {"user_id": "alice"}).startswith("ERROR")
    assert "unavailable" in tool.handler({"query": "x"}, {}).lower()
