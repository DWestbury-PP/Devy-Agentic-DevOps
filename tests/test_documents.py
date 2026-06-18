"""Document registry, ingest jobs, and the in-process ingest worker (Phase 9c-2)."""

import hashlib

import pytest

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.store import PgVectorStore, StoredChunk
from agentic_devops.proxy.documents import DocumentStore, JobStore
from agentic_devops.proxy.ingest_worker import IngestWorker

_DIM = 32


def _fake_embed(texts, model, api_base):
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in t.lower().split():
            v[int(hashlib.sha256(tok.encode()).hexdigest(), 16) % _DIM] += 1.0
        out.append(v)
    return out


@pytest.fixture()
def docs(pool):
    return DocumentStore(pool)


@pytest.fixture()
def jobs(pool):
    return JobStore(pool)


# -- DocumentStore ----------------------------------------------------------

def test_register_inserts_then_versions_on_change(docs):
    d = docs.register("acme", "r.md", title="Runbook", doc_type="runbook",
                      content="v1", content_hash="h1", bytes_=2)
    assert d.version == 1 and d.status == "ready" and d.title == "Runbook"

    same = docs.register("acme", "r.md", content="v1", content_hash="h1")
    assert same.id == d.id and same.version == 1  # unchanged content → no bump

    changed = docs.register("acme", "r.md", content="v2", content_hash="h2")
    assert changed.id == d.id and changed.version == 2  # content changed → bump


def test_set_status_and_chunk_count(docs):
    d = docs.register("acme", "r.md", content_hash="h", status="pending")
    docs.set_status(d.id, "ready", chunk_count=7)
    got = docs.get(d.id)
    assert got.status == "ready" and got.chunk_count == 7

    # register again (no content change) must preserve chunk_count
    docs.register("acme", "r.md", content_hash="h", status="ready")
    assert docs.get(d.id).chunk_count == 7


def test_delete_removes_document_and_its_chunks(pool, docs):
    store = PgVectorStore(pool)
    d = docs.register("acme", "r.md", content_hash="h")
    store.upsert(
        [StoredChunk(id="acme:r.md:0", corpus="acme", source_path="r.md",
                     heading_path="", text="hi", content_hash="x", document_id=d.id)],
        [[1.0] + [0.0] * (_DIM - 1)],
    )
    assert store.count() == 1
    assert docs.delete(d.id) is True
    assert docs.get(d.id) is None
    assert store.count() == 0  # chunk cascaded


def test_reconcile_fails_orphaned_processing(docs):
    d = docs.register("acme", "r.md", content_hash="h", status="processing")
    assert docs.reconcile() == 1
    got = docs.get(d.id)
    assert got.status == "failed" and "interrupted" in got.error


# -- JobStore ---------------------------------------------------------------

def test_job_lifecycle(jobs):
    j = jobs.create("acme", total=3)
    assert j.status == "queued" and j.total == 3 and j.done == 0
    assert jobs.next_queued().id == j.id
    jobs.set_status(j.id, "running")
    assert jobs.next_queued() is None  # no longer queued
    jobs.bump(j.id); jobs.bump(j.id)
    assert jobs.get(j.id).done == 2
    jobs.set_status(j.id, "done")
    assert jobs.get(j.id).status == "done"


# -- IngestWorker -----------------------------------------------------------

def test_worker_processes_a_batch(pool, docs, jobs):
    store = PgVectorStore(pool)
    embedder = Embedder(model="fake", embed_fn=_fake_embed)
    worker = IngestWorker(docs, jobs, store, embedder)  # no enricher → deterministic

    job = jobs.create("acme", total=2)
    for name, body in [("a.md", "# Disk Runbook\n\n## Mitigation\n\nfree up space"),
                       ("b.md", "# Memory Runbook\n\n## Mitigation\n\nrestart the pod")]:
        docs.register("acme", name, content=body, content_hash=name, status="pending", job_id=job.id)

    assert worker.run_once() is True
    # All docs ready with chunk counts; chunks landed linked to their document.
    for name in ("a.md", "b.md"):
        d = docs.by_source("acme", name)
        assert d.status == "ready" and d.chunk_count >= 1
    assert jobs.get(job.id).status == "done" and jobs.get(job.id).done == 2
    assert store.count() >= 2

    # Idempotent: no more queued work.
    assert worker.run_once() is False


def test_worker_isolates_a_failing_doc(pool, docs, jobs, monkeypatch):
    store = PgVectorStore(pool)
    embedder = Embedder(model="fake", embed_fn=_fake_embed)
    worker = IngestWorker(docs, jobs, store, embedder)

    job = jobs.create("acme", total=2)
    docs.register("acme", "ok.md", content="# Ok\n\n## S\n\nbody", content_hash="ok", status="pending", job_id=job.id)
    docs.register("acme", "bad.md", content="# Bad\n\n## S\n\nbody", content_hash="bad", status="pending", job_id=job.id)

    real = store.upsert

    def flaky_upsert(chunks, embeddings):
        if chunks and chunks[0].source_path == "bad.md":
            raise RuntimeError("boom")
        return real(chunks, embeddings)

    monkeypatch.setattr(store, "upsert", flaky_upsert)
    worker.run_once()

    assert docs.by_source("acme", "ok.md").status == "ready"
    bad = docs.by_source("acme", "bad.md")
    assert bad.status == "failed" and "boom" in bad.error
    assert jobs.get(job.id).status == "done"  # batch completes despite one failure
