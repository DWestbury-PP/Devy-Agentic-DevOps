"""Postgres/pgvector store: round-trip, cosine ranking, corpus filter, idempotency."""

import pytest

from agentic_devops.knowledge.store import PgVectorStore, StoredChunk


def _chunk(cid, corpus, source, text, h=None):
    return StoredChunk(
        id=cid, corpus=corpus, source_path=source, heading_path="", text=text,
        content_hash=h or cid,
    )


@pytest.fixture()
def store(pool):
    return PgVectorStore(pool)


def test_upsert_and_search_ranks_by_cosine(store):
    # Three orthogonal vectors; query closest to the second.
    store.upsert(
        [
            _chunk("a", "c1", "a.md", "alpha"),
            _chunk("b", "c1", "b.md", "beta"),
            _chunk("c", "c1", "c.md", "gamma"),
        ],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    )
    hits = store.search([0.1, 0.9, 0.0], k=2)
    assert hits[0].chunk.id == "b"
    assert len(hits) == 2
    assert hits[0].score > hits[1].score


def test_corpus_filter(store):
    store.upsert([_chunk("a", "repo", "x.md", "x")], [[1.0, 0.0]])
    store.upsert([_chunk("b", "acme", "y.md", "y")], [[1.0, 0.0]])

    hits = store.search([1.0, 0.0], k=5, corpus="acme")
    assert {h.chunk.corpus for h in hits} == {"acme"}
    assert store.corpora() == {"acme": 1, "repo": 1}


def test_upsert_is_idempotent_on_id(store):
    store.upsert([_chunk("a", "c1", "a.md", "first")], [[1.0, 0.0]])
    store.upsert([_chunk("a", "c1", "a.md", "second")], [[0.0, 1.0]])
    assert store.count() == 1
    hits = store.search([0.0, 1.0], k=1)
    assert hits[0].chunk.text == "second"


def test_hashes_and_delete_source(store):
    store.upsert(
        [_chunk("a1", "c1", "doc.md", "one"), _chunk("a2", "c1", "doc.md", "two")],
        [[1.0, 0.0], [0.0, 1.0]],
    )
    assert store.hashes_for_source("c1", "doc.md") == {"a1", "a2"}
    store.delete_source("c1", "doc.md")
    assert store.hashes_for_source("c1", "doc.md") == set()
    assert store.count() == 0


def test_search_empty_store(store):
    assert store.search([1.0, 0.0], k=3) == []


# -- enriched fields + hybrid (Phase 9c-1) ----------------------------------

def _echunk(cid, corpus, source, text, prefix="", meta=None):
    return StoredChunk(
        id=cid, corpus=corpus, source_path=source, heading_path="", text=text,
        content_hash=cid, context_prefix=prefix, metadata=meta or {},
    )


def test_upsert_persists_context_prefix_and_metadata(store):
    store.upsert(
        [_echunk("a", "c1", "a.md", "body text", prefix="situated context",
                 meta={"title": "Doc", "doc_type": "runbook"})],
        [[1.0, 0.0]],
    )
    hit = store.search([1.0, 0.0], k=1)[0]
    assert hit.chunk.context_prefix == "situated context"
    assert hit.chunk.metadata == {"title": "Doc", "doc_type": "runbook"}
    assert hit.sources == ("vector",)


def test_hybrid_keyword_arm_finds_what_vector_misses(store):
    # Query vector points at A, but the exact token lives in B.
    store.upsert(
        [
            _echunk("a", "c1", "a.md", "alpha beta gamma"),
            _echunk("b", "c1", "b.md", "the failover runbook mentions pgbouncer"),
            _echunk("c", "c1", "c.md", "delta epsilon"),
        ],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    )
    hits = store.hybrid_search("pgbouncer", [1.0, 0.0, 0.0], k=3)
    by_id = {h.chunk.id: h for h in hits}
    assert "b" in by_id and "keyword" in by_id["b"].sources  # exact-token match
    assert "a" in by_id and "vector" in by_id["a"].sources   # semantic match


def test_hybrid_both_arms_rank_top_and_tag_sources(store):
    store.upsert(
        [
            _echunk("a", "c1", "a.md", "alpha checkout latency"),
            _echunk("b", "c1", "b.md", "unrelated beta"),
        ],
        [[1.0, 0.0], [0.0, 1.0]],
    )
    hits = store.hybrid_search("checkout", [1.0, 0.0], k=2)
    assert hits[0].chunk.id == "a"  # matched by both arms → fused to the top
    assert set(hits[0].sources) == {"vector", "keyword"}


def test_hybrid_respects_corpus_filter(store):
    store.upsert([_echunk("a", "repo", "x.md", "shared keyword token")], [[1.0, 0.0]])
    store.upsert([_echunk("b", "acme", "y.md", "shared keyword token")], [[1.0, 0.0]])
    hits = store.hybrid_search("keyword", [1.0, 0.0], k=5, corpus="acme")
    assert {h.chunk.corpus for h in hits} == {"acme"}


def test_rrf_fuse_rewards_agreement():
    from agentic_devops.knowledge.store import _rrf_fuse

    # "x" is top of both lists; "y"/"z" appear once each → x scores highest.
    scores = _rrf_fuse([["x", "y"], ["x", "z"]])
    assert scores["x"] > scores["y"]
    assert scores["x"] > scores["z"]
