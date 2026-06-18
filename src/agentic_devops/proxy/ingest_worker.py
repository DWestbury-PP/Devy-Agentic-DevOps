"""In-process ingest worker (Phase 9c-2).

A single background thread drains queued ingest jobs: for each job it processes
its pending documents through the shared ``ingest_markdown`` pipeline (chunk →
context → embed → store), flipping each document pending → processing →
ready/failed and bumping the job's progress so the UI can poll. Best-effort and
crash-safe: ``DocumentStore.reconcile`` / ``JobStore.reconcile`` on startup fail
anything left mid-flight by a previous (crashed) run.

Single-instance only — a real multi-replica queue is a documented future upgrade
(see docs/plans/admin-control-plane.md §8).
"""

from __future__ import annotations

import threading
from typing import Optional

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.enrich import Enricher
from agentic_devops.knowledge.ingest import ingest_markdown
from agentic_devops.knowledge.store import VectorStore
from agentic_devops.proxy.documents import DocumentStore, IngestJob, JobStore


class IngestWorker:
    def __init__(
        self,
        documents: DocumentStore,
        jobs: JobStore,
        store: VectorStore,
        embedder: Embedder,
        enricher: Optional[Enricher] = None,
        *,
        split_level: int = 2,
        max_chars: int = 8000,
        overlap: int = 200,
        poll_interval: float = 2.0,
    ) -> None:
        self._docs = documents
        self._jobs = jobs
        self._store = store
        self._embedder = embedder
        self._enricher = enricher
        self._split_level = split_level
        self._max_chars = max_chars
        self._overlap = overlap
        self._poll = poll_interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="ingest-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def notify(self) -> None:
        """Wake the worker immediately (called after an upload enqueues a job)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            worked = self.run_once()
            if not worked:
                self._wake.wait(timeout=self._poll)
                self._wake.clear()

    # -- unit of work (also called directly in tests) -----------------------
    def run_once(self) -> bool:
        """Process the next queued job, if any. Returns True if work was done."""
        job = self._jobs.next_queued()
        if job is None:
            return False
        self._process_job(job)
        return True

    def _process_job(self, job: IngestJob) -> None:
        self._jobs.set_status(job.id, "running")
        try:
            for doc in self._docs.by_job(job.id, status="pending"):
                self._process_doc(doc.id, doc.corpus, doc.source_path)
                self._jobs.bump(job.id)
            self._jobs.set_status(job.id, "done")
        except Exception as exc:  # noqa: BLE001 — never let the worker thread die
            self._jobs.set_status(job.id, "failed", error=str(exc)[:500])

    def _process_doc(self, doc_id: str, corpus: str, source_path: str) -> None:
        self._docs.set_status(doc_id, "processing")
        try:
            raw = self._docs.content_of(doc_id) or ""
            result = ingest_markdown(
                raw, corpus, source_path, self._store, self._embedder,
                split_level=self._split_level, max_chars=self._max_chars,
                overlap=self._overlap, enricher=self._enricher, document_id=doc_id,
            )
            self._docs.set_status(doc_id, "ready", chunk_count=result.chunks_written)
        except Exception as exc:  # noqa: BLE001 — isolate a bad doc; keep the batch going
            self._docs.set_status(doc_id, "failed", error=str(exc)[:500])
