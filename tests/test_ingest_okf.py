"""OKF ingestion + frontmatter-filtered retrieval (Phase B), against the live DB.

Deterministic bag-of-words embedder (no network), real PgVectorStore so the JSONB
containment filter, facets, and memory_index hit actual Postgres.
"""

import hashlib

import pytest

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.facts import FactStore
from agentic_devops.knowledge.ingest import ingest_path
from agentic_devops.knowledge.retrieval import build_search_knowledge_tool
from agentic_devops.knowledge.store import PgVectorStore
from agentic_devops.tools.builtin.memory_index import build_memory_index_tool

_DIM = 64


def _fake_embed(texts, model, api_base):
    out = []
    for text in texts:
        vec = [0.0] * _DIM
        for tok in text.lower().split():
            vec[int(hashlib.sha256(tok.encode()).hexdigest(), 16) % _DIM] += 1.0
        out.append(vec)
    return out


@pytest.fixture()
def embedder():
    return Embedder(model="fake", embed_fn=_fake_embed)


@pytest.fixture()
def store(pool):
    return PgVectorStore(pool)


def _corpus(tmp_path):
    d = tmp_path / "okf"
    d.mkdir()
    (d / "runbook.md").write_text(
        "---\ntype: runbook\ntitle: Checkout Runbook\ntags: [oncall, checkout]\n"
        "resource: https://example.com/rb/checkout\n---\n\n"
        "# Checkout\n\n## Mitigation\n\nRestart the checkout worker pool to recover latency.\n"
    )
    (d / "architecture.md").write_text(
        "---\ntype: architecture\ntitle: Orders Service\ntags: [design]\n---\n\n"
        "# Orders\n\nThe orders service publishes events to the pricing service.\n"
    )
    (d / "plain.md").write_text("# Plain Doc\n\nNo frontmatter, just prose about backups.\n")
    # OKF reserved files — must be skipped as chunk sources.
    (d / "index.md").write_text("# Index\n\n* [Checkout Runbook](runbook.md)\n")
    (d / "log.md").write_text("# Log\n\n## 2026-04-01\n* Created.\n")
    return d


@pytest.fixture()
def ingested(tmp_path, store, embedder):
    stats = ingest_path(_corpus(tmp_path), store, embedder, corpus="okf")
    return stats


def test_reserved_files_are_skipped(ingested):
    # index.md and log.md are not concept documents.
    assert ingested.files_seen == 3  # runbook, architecture, plain
    assert ingested.files_ingested == 3


def test_frontmatter_becomes_chunk_metadata(ingested, store, embedder):
    hits = store.hybrid_search("checkout worker pool", embedder.embed_query("checkout worker pool"), k=1)
    assert hits
    meta = hits[0].chunk.metadata
    assert meta.get("type") == "runbook"
    assert "oncall" in (meta.get("tags") or [])
    assert meta.get("resource") == "https://example.com/rb/checkout"


def test_frontmatter_block_not_embedded_as_body(ingested, store, embedder):
    hits = store.hybrid_search("checkout", embedder.embed_query("checkout"), k=5)
    assert hits
    assert all("type: runbook" not in h.chunk.text for h in hits)


def test_frontmatter_filter_restricts_results(ingested, store, embedder):
    qv = embedder.embed_query("service")
    arch = store.hybrid_search("service", qv, k=5, frontmatter={"doc_type": "architecture"})
    assert arch and all(h.chunk.metadata.get("type") == "architecture" for h in arch)

    # Tag filter — array containment: any doc carrying the tag.
    qv2 = embedder.embed_query("restart")
    oncall = store.hybrid_search("restart", qv2, k=5, frontmatter={"tags": ["oncall"]})
    assert oncall and all("oncall" in (h.chunk.metadata.get("tags") or []) for h in oncall)


def test_facets_lists_types_and_tags(ingested, store):
    facets = store.facets()
    assert {"runbook", "architecture", "doc"} <= set(facets["doc_types"])
    assert {"oncall", "checkout", "design"} <= set(facets["tags"])


def test_search_knowledge_tool_accepts_filter(ingested, store, embedder):
    tool = build_search_knowledge_tool(store, embedder)
    out = tool.handler({"query": "service events", "filter": {"doc_type": "architecture"}})
    assert "architecture.md" in out and "runbook.md" not in out


def test_search_knowledge_tool_rejects_bad_filter(store, embedder):
    tool = build_search_knowledge_tool(store, embedder)
    assert tool.handler({"query": "x", "filter": "runbook"}).startswith("ERROR")


def test_memory_index_maps_the_surface(ingested, store, pool, embedder):
    facts = FactStore(pool, embedder)
    facts.add_fact("pricing exposes port 9090", source="t", subject="svc:pricing", attribute="port")
    out = build_memory_index_tool(store, facts).handler({})
    assert "okf" in out                      # corpus listed
    assert "runbook" in out                  # doc type facet
    assert "oncall" in out                   # tag facet
    assert "svc:pricing" in out              # fact subject
    assert "1 current facts" in out


def test_frontmatter_only_edit_reingests(tmp_path, store, embedder):
    d = _corpus(tmp_path)
    first = ingest_path(d, store, embedder, corpus="okf")
    assert first.files_ingested == 3
    # Re-ingest unchanged → all skipped.
    again = ingest_path(d, store, embedder, corpus="okf")
    assert again.files_ingested == 0 and again.files_skipped == 3
    # Change ONLY the frontmatter (add a tag) → that file re-ingests.
    (d / "runbook.md").write_text(
        "---\ntype: runbook\ntitle: Checkout Runbook\ntags: [oncall, checkout, urgent]\n"
        "resource: https://example.com/rb/checkout\n---\n\n"
        "# Checkout\n\n## Mitigation\n\nRestart the checkout worker pool to recover latency.\n"
    )
    edited = ingest_path(d, store, embedder, corpus="okf")
    assert edited.files_ingested == 1 and edited.files_skipped == 2
