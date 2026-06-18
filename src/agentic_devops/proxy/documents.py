"""Document registry + ingest jobs (Phase 9c-2).

The control-plane front door to the knowledge base. ``DocumentStore`` is the
unified registry every ingest path writes to — both the ``ingest`` CLI and the
UI upload register a row here, so the Knowledge admin page shows every corpus.
``JobStore`` tracks an upload **batch** so the UI can poll progress while the
in-process worker (see ``ingest_worker.py``) drains it.

Chunks link back via ``chunks.document_id``; deleting a document deletes its
chunks explicitly here (no FK cascade, keeping the schema simple).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from psycopg_pool import ConnectionPool

_DOC_COLS = (
    "id, corpus, source_path, title, doc_type, content_hash, bytes, version, "
    "status, chunk_count, error, uploaded_by, job_id, created_at, updated_at"
)
_JOB_COLS = "id, corpus, status, total, done, error, created_at, updated_at"


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


@dataclass
class Document:
    id: str
    corpus: str
    source_path: str
    title: str = ""
    doc_type: str = "doc"
    content_hash: str = ""
    bytes: int = 0
    version: int = 1
    status: str = "pending"
    chunk_count: int = 0
    error: str = ""
    uploaded_by: str = ""
    job_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class IngestJob:
    id: str
    corpus: str = ""
    status: str = "queued"
    total: int = 0
    done: int = 0
    error: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _row_to_doc(r: tuple) -> Document:
    return Document(
        id=r[0], corpus=r[1], source_path=r[2], title=r[3], doc_type=r[4],
        content_hash=r[5], bytes=r[6], version=r[7], status=r[8], chunk_count=r[9],
        error=r[10], uploaded_by=r[11], job_id=r[12],
        created_at=_iso(r[13]), updated_at=_iso(r[14]),
    )


def _row_to_job(r: tuple) -> IngestJob:
    return IngestJob(
        id=r[0], corpus=r[1], status=r[2], total=r[3], done=r[4], error=r[5],
        created_at=_iso(r[6]), updated_at=_iso(r[7]),
    )


class DocumentStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def list(self, corpus: Optional[str] = None) -> list[Document]:
        sql = f"SELECT {_DOC_COLS} FROM documents"
        params: tuple = ()
        if corpus:
            sql += " WHERE corpus = %s"
            params = (corpus,)
        sql += " ORDER BY corpus, source_path"
        with self._pool.connection() as conn:
            return [_row_to_doc(r) for r in conn.execute(sql, params).fetchall()]

    def get(self, doc_id: str) -> Optional[Document]:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_DOC_COLS} FROM documents WHERE id = %s", (doc_id,)).fetchone()
        return _row_to_doc(row) if row else None

    def by_source(self, corpus: str, source_path: str) -> Optional[Document]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_DOC_COLS} FROM documents WHERE corpus = %s AND source_path = %s",
                (corpus, source_path),
            ).fetchone()
        return _row_to_doc(row) if row else None

    def by_job(self, job_id: str, status: Optional[str] = None) -> list[Document]:
        sql = f"SELECT {_DOC_COLS} FROM documents WHERE job_id = %s"
        params: list = [job_id]
        if status:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY source_path"
        with self._pool.connection() as conn:
            return [_row_to_doc(r) for r in conn.execute(sql, params).fetchall()]

    def register(
        self, corpus: str, source_path: str, *, title: str = "", doc_type: str = "doc",
        content: str = "", content_hash: str = "", bytes_: int = 0,
        uploaded_by: str = "", status: str = "ready", job_id: Optional[str] = None,
    ) -> Document:
        """Insert or update a document by ``(corpus, source_path)``.

        ``version`` bumps when the content hash changes vs the stored row.
        ``chunk_count`` is NOT touched here — it's owned by :meth:`set_status`
        once chunks are written, so a no-op re-ingest preserves the prior count.
        Used by the CLI (status=ready after ingest) and the UI upload
        (status=pending; the worker flips it to ready/failed).
        """
        existing = self.by_source(corpus, source_path)
        doc_id = existing.id if existing else uuid.uuid4().hex[:12]
        version = 1
        if existing:
            changed = content_hash and content_hash != existing.content_hash
            version = existing.version + 1 if changed else existing.version
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO documents
                    (id, corpus, source_path, title, doc_type, content, content_hash,
                     bytes, version, status, error, uploaded_by, job_id, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'',%s,%s, now())
                ON CONFLICT (corpus, source_path) DO UPDATE SET
                    title=EXCLUDED.title, doc_type=EXCLUDED.doc_type, content=EXCLUDED.content,
                    content_hash=EXCLUDED.content_hash, bytes=EXCLUDED.bytes,
                    version=EXCLUDED.version, status=EXCLUDED.status, error='',
                    uploaded_by=EXCLUDED.uploaded_by, job_id=EXCLUDED.job_id, updated_at=now()
                """,
                (doc_id, corpus, source_path, title, doc_type, content, content_hash,
                 bytes_, version, status, uploaded_by, job_id),
            )
        return self.by_source(corpus, source_path)  # type: ignore[return-value]

    def corpora(self) -> dict[str, int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT corpus, COUNT(*) FROM documents GROUP BY corpus ORDER BY corpus"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def content_of(self, doc_id: str) -> Optional[str]:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT content FROM documents WHERE id = %s", (doc_id,)).fetchone()
        return row[0] if row else None

    def set_status(
        self, doc_id: str, status: str, error: str = "", chunk_count: Optional[int] = None
    ) -> None:
        sets = ["status = %s", "error = %s", "updated_at = now()"]
        params: list = [status, error]
        if chunk_count is not None:
            sets.insert(1, "chunk_count = %s")
            params.insert(1, chunk_count)
        params.append(doc_id)
        with self._pool.connection() as conn:
            conn.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id = %s", tuple(params))

    def delete(self, doc_id: str) -> bool:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
            cur = conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            return cur.rowcount > 0

    def delete_corpus(self, corpus: str) -> int:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE corpus = %s", (corpus,))
            cur = conn.execute("DELETE FROM documents WHERE corpus = %s", (corpus,))
            return cur.rowcount

    def reconcile(self) -> int:
        """On startup, fail any document orphaned mid-processing (worker died)."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE documents SET status='failed', error='interrupted — re-ingest', "
                "updated_at=now() WHERE status='processing'"
            )
            return cur.rowcount


class JobStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def create(self, corpus: str, total: int) -> IngestJob:
        job_id = uuid.uuid4().hex[:12]
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO ingest_jobs (id, corpus, status, total, done) "
                "VALUES (%s, %s, 'queued', %s, 0)",
                (job_id, corpus, total),
            )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> Optional[IngestJob]:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_JOB_COLS} FROM ingest_jobs WHERE id = %s", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def next_queued(self) -> Optional[IngestJob]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_JOB_COLS} FROM ingest_jobs WHERE status = 'queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
        return _row_to_job(row) if row else None

    def set_status(self, job_id: str, status: str, error: str = "") -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE ingest_jobs SET status = %s, error = %s, updated_at = now() WHERE id = %s",
                (status, error, job_id),
            )

    def bump(self, job_id: str, delta: int = 1) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE ingest_jobs SET done = done + %s, updated_at = now() WHERE id = %s",
                (delta, job_id),
            )

    def reconcile(self) -> int:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE ingest_jobs SET status='failed', error='interrupted', updated_at=now() "
                "WHERE status IN ('queued','running')"
            )
            return cur.rowcount
