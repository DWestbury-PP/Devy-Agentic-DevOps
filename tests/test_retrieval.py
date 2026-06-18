"""End-to-end knowledge path with a deterministic fake embedder (no network):
ingest a temp dir → register the search_knowledge tool → retrieve with citations.
"""

import hashlib

import pytest

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.ingest import ingest_path
from agentic_devops.knowledge.retrieval import build_search_knowledge_tool
from agentic_devops.knowledge.store import PgVectorStore
from agentic_devops.tools.router import ToolsRouter

_DIM = 64


def _fake_embed(texts, model, api_base):
    """Deterministic bag-of-words hashing embedder — similar text → similar vector,
    so nearest-neighbour search is meaningful without a real model."""
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
def corpus_dir(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "latency.md").write_text(
        "# Checkout Latency Runbook\n\n## Mitigation\n\n"
        "Enable the risk fail open feature flag and scale acme-risk replicas.\n"
    )
    (d / "failover.md").write_text(
        "# Database Failover Runbook\n\n## Procedure\n\n"
        "Trigger a managed aurora failover to a healthy reader replica.\n"
    )
    return d


def test_ingest_then_retrieve_with_citation(pool, embedder, corpus_dir):
    store = PgVectorStore(pool)
    stats = ingest_path(corpus_dir, store, embedder, corpus="acme")

    assert stats.files_ingested == 2
    assert stats.chunks_written >= 2
    assert store.corpora() == {"acme": store.count()}

    router = ToolsRouter()
    router.register(build_search_knowledge_tool(store, embedder))

    # Discoverable via find_tools and categorised under "knowledge".
    assert any(s.name == "search_knowledge" for s in router.find(intent="runbook for an alert"))

    out = router.execute("search_knowledge", {"query": "risk fail open flag scale replicas"})
    assert "latency.md" in out  # cited the right source
    assert "Mitigation" in out  # cited the heading path
    assert "fail open" in out.lower()


def test_corpus_filter_through_tool(pool, embedder, corpus_dir):
    store = PgVectorStore(pool)
    ingest_path(corpus_dir, store, embedder, corpus="acme")
    tool = build_search_knowledge_tool(store, embedder)

    out = tool.handler({"query": "aurora failover reader", "corpus": "acme"})
    assert "failover.md" in out

    missing = tool.handler({"query": "anything", "corpus": "does-not-exist"})
    assert "No knowledge-base entries" in missing


def test_reingest_is_idempotent(pool, embedder, corpus_dir):
    store = PgVectorStore(pool)
    ingest_path(corpus_dir, store, embedder, corpus="acme")
    first = store.count()

    stats = ingest_path(corpus_dir, store, embedder, corpus="acme")
    assert stats.files_ingested == 0
    assert stats.files_skipped == 2
    assert store.count() == first


def test_empty_query_errors(pool, embedder):
    store = PgVectorStore(pool)
    tool = build_search_knowledge_tool(store, embedder)
    assert tool.handler({"query": "  "}).startswith("ERROR")


def test_coverage_is_read_live_not_snapshotted(pool, embedder, corpus_dir):
    # Tool built against an EMPTY store (as at a fresh proxy startup).
    store = PgVectorStore(pool)
    tool = build_search_knowledge_tool(store, embedder)
    assert "knowledge base is empty" in tool.handler({"query": "anything at all"})

    # Ingest AFTER the tool was built — it must be searchable immediately, and a
    # miss must report live corpus coverage (no stale snapshot).
    ingest_path(corpus_dir, store, embedder, corpus="acme")
    assert "latency.md" in tool.handler({"query": "risk fail open flag"})
    # A guaranteed miss (non-existent corpus) reports live coverage of the rest.
    miss = tool.handler({"query": "anything", "corpus": "does-not-exist"})
    assert "acme (" in miss  # live count, computed on the call
