"""Shared ingest pipeline: contextual prefix + metadata + enrichment-aware idempotency.

Uses a fake store and a seam-injected embedder, so no DB or network is touched.
"""

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.enrich import Enricher
from agentic_devops.knowledge.ingest import ingest_path


class FakeStore:
    def __init__(self):
        self.chunks = {}      # id -> StoredChunk
        self.embeddings = {}  # id -> vector

    def hashes_for_source(self, corpus, source_path):
        return {
            c.content_hash for c in self.chunks.values()
            if c.corpus == corpus and c.source_path == source_path
        }

    def delete_source(self, corpus, source_path):
        for cid in [k for k, c in self.chunks.items()
                    if c.corpus == corpus and c.source_path == source_path]:
            self.chunks.pop(cid, None)
            self.embeddings.pop(cid, None)

    def upsert(self, chunks, embeddings):
        for c, e in zip(chunks, embeddings):
            self.chunks[c.id] = c
            self.embeddings[c.id] = e


def _embedder(captured):
    def embed_fn(texts, model, api_base):
        captured.extend(texts)
        return [[float(len(t))] for t in texts]
    return Embedder(embed_fn=embed_fn)


def _write_doc(tmp_path):
    d = tmp_path / "kb"
    d.mkdir()
    (d / "runbook.md").write_text(
        "# Checkout Runbook\n\n## Mitigation\n\nRestart the worker pool to recover.\n"
    )
    return d


def test_llm_synopsis_is_additive_over_deterministic_context(tmp_path):
    captured = []
    store = FakeStore()
    enricher = Enricher(context_fn=lambda doc, chunk: "CTX about checkout")

    stats = ingest_path(_write_doc(tmp_path), store, _embedder(captured), enricher=enricher)

    assert stats.files_ingested == 1
    assert stats.chunks_written >= 1
    assert stats.chunks_contextualized == stats.chunks_written
    chunk = next(iter(store.chunks.values()))
    # Prefix carries BOTH the deterministic lineage and the LLM blurb.
    assert "Checkout Runbook > Mitigation" in chunk.context_prefix
    assert "CTX about checkout" in chunk.context_prefix
    # Embedded text = prefix + chunk text.
    assert all("CTX about checkout" in t and "Mitigation" in t for t in captured)
    assert chunk.metadata["title"] == "Checkout Runbook"
    assert chunk.metadata["doc_type"] == "runbook"


def test_plain_ingest_still_has_deterministic_context(tmp_path):
    captured = []
    store = FakeStore()
    stats = ingest_path(_write_doc(tmp_path), store, _embedder(captured))  # no enricher
    assert stats.chunks_contextualized == 0  # no LLM calls
    chunk = next(iter(store.chunks.values()))
    # The free structural lineage is still prepended (and embedded).
    assert chunk.context_prefix == "Checkout Runbook > Mitigation"
    assert all(t.startswith("Checkout Runbook > Mitigation\n\n") for t in captured)


def test_idempotent_when_enrichment_unchanged(tmp_path):
    store = FakeStore()
    enricher = Enricher(context_fn=lambda doc, chunk: "CTX")
    src = _write_doc(tmp_path)

    first = ingest_path(src, store, _embedder([]), enricher=enricher)
    second = ingest_path(src, store, _embedder([]), enricher=enricher)
    assert first.files_ingested == 1
    assert second.files_ingested == 0 and second.files_skipped == 1


def test_toggling_enrichment_forces_reingest(tmp_path):
    store = FakeStore()
    src = _write_doc(tmp_path)

    plain = ingest_path(src, store, _embedder([]))  # plain first
    enriched = ingest_path(src, store, _embedder([]), enricher=Enricher(context_fn=lambda d, c: "CTX"))
    # Same source text, but enabling enrichment changes the fingerprint → re-embed.
    assert plain.files_ingested == 1
    assert enriched.files_ingested == 1 and enriched.files_skipped == 0
    assert "CTX" in next(iter(store.chunks.values())).context_prefix
