"""Vector store: Postgres + pgvector, with hybrid (vector + full-text) search.

Chunks and their embeddings live in the shared Postgres (``chunks`` table).
Retrieval is **hybrid** (Phase 9c-1): exact cosine nearest-neighbour via
pgvector's ``<=>`` *fused with* Postgres full-text (`tsv @@ plainto_tsquery`)
using Reciprocal Rank Fusion — the vector arm catches paraphrase/semantics, the
full-text arm catches exact tokens vectors miss (error codes, hostnames, flags).

``VectorStore`` is the swap seam — the retrieval tool and the ingest pipeline
depend only on it. Embeddings are written as ``vector`` literals and never read
back, so no array adapter is needed. The ``embedding`` column is
dimension-agnostic so any embedder works; for large corpora, pin the dimension
and add an HNSW index (see docs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional, Protocol

from psycopg_pool import ConnectionPool

# Columns selected for a hydrated chunk, in order (see _row_to_chunk).
_CHUNK_COLS = "id, corpus, source_path, heading_path, text, content_hash, context_prefix, metadata"
# Reciprocal Rank Fusion constant — the standard 60; damps the tail so a item
# ranked #1 by one retriever isn't swamped by many mid-ranked items.
_RRF_K = 60


@dataclass
class StoredChunk:
    """A chunk as persisted/retrieved, with its provenance for citation."""

    id: str
    corpus: str
    source_path: str
    heading_path: str
    text: str
    content_hash: str
    context_prefix: str = ""  # contextual blurb (prepended before embedding)
    metadata: dict = field(default_factory=dict)  # {title, doc_type, headings, ...}
    document_id: Optional[str] = None  # links to documents.id (Phase 9c-2)


@dataclass
class SearchHit:
    chunk: StoredChunk
    score: float  # cosine similarity for vector search; fused RRF score for hybrid
    sources: tuple[str, ...] = ()  # which retrievers matched: "vector" and/or "keyword"


class VectorStore(Protocol):
    def upsert(self, chunks: list[StoredChunk], embeddings: list[list[float]]) -> None: ...
    def search(
        self, query: list[float], k: int = 5, corpus: Optional[str] = None,
        frontmatter: Optional[dict] = None,
    ) -> list[SearchHit]: ...
    def hybrid_search(
        self, query_text: str, query_vec: list[float], k: int = 5,
        corpus: Optional[str] = None, candidates: int = 20,
        frontmatter: Optional[dict] = None,
    ) -> list[SearchHit]: ...
    def corpora(self) -> dict[str, int]: ...
    def facets(self) -> dict[str, list[str]]: ...
    def count(self) -> int: ...
    def hashes_for_source(self, corpus: str, source_path: str) -> set[str]: ...
    def delete_source(self, corpus: str, source_path: str) -> None: ...


def _vec_literal(vec: list[float]) -> str:
    """Render a vector as a pgvector text literal: ``[1.0,2.0,3.0]``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _row_to_chunk(r: tuple) -> StoredChunk:
    return StoredChunk(
        id=r[0], corpus=r[1], source_path=r[2], heading_path=r[3],
        text=r[4], content_hash=r[5], context_prefix=r[6] or "",
        metadata=r[7] if isinstance(r[7], dict) else {},
    )


def _rrf_fuse(rank_lists: list[list[str]], k_rrf: int = _RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: each id scores sum(1/(k + rank)) across lists."""
    scores: dict[str, float] = {}
    for ranked in rank_lists:
        for rank, cid in enumerate(ranked):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k_rrf + rank + 1)
    return scores


class PgVectorStore:
    """Postgres/pgvector-backed store with exact cosine + hybrid search."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    # -- ingest -------------------------------------------------------------
    def hashes_for_source(self, corpus: str, source_path: str) -> set[str]:
        """Content hashes already stored for a source — lets ingest skip
        re-embedding unchanged files."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT content_hash FROM chunks WHERE corpus = %s AND source_path = %s",
                (corpus, source_path),
            ).fetchall()
        return {r[0] for r in rows}

    def delete_source(self, corpus: str, source_path: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE corpus = %s AND source_path = %s", (corpus, source_path)
            )

    def upsert(self, chunks: list[StoredChunk], embeddings: list[list[float]]) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must be the same length")
        if not chunks:
            return
        rows = [
            (
                c.id, c.corpus, c.source_path, c.heading_path, c.text, c.content_hash,
                _vec_literal(e), c.context_prefix, json.dumps(c.metadata or {}), c.document_id,
            )
            for c, e in zip(chunks, embeddings)
        ]
        with self._pool.connection() as conn:
            conn.cursor().executemany(
                """
                INSERT INTO chunks
                    (id, corpus, source_path, heading_path, text, content_hash,
                     embedding, context_prefix, metadata, document_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s::jsonb, %s)
                ON CONFLICT (id) DO UPDATE SET
                    corpus=EXCLUDED.corpus, source_path=EXCLUDED.source_path,
                    heading_path=EXCLUDED.heading_path, text=EXCLUDED.text,
                    content_hash=EXCLUDED.content_hash, embedding=EXCLUDED.embedding,
                    context_prefix=EXCLUDED.context_prefix, metadata=EXCLUDED.metadata,
                    document_id=EXCLUDED.document_id
                """,
                rows,
            )

    # -- query --------------------------------------------------------------
    @staticmethod
    def _filters(corpus: Optional[str], frontmatter: Optional[dict]) -> tuple[str, list]:
        """Build the shared WHERE fragment (no leading WHERE/AND) + its params.

        ``frontmatter`` is a JSONB containment filter (``metadata @> …``): exact
        match on scalar keys, subset on array keys (e.g. ``{"tags": ["oncall"]}``
        matches any chunk whose ``tags`` includes ``oncall``)."""
        clauses: list[str] = []
        params: list = []
        if corpus:
            clauses.append("corpus = %s")
            params.append(corpus)
        if frontmatter:
            clauses.append("metadata @> %s::jsonb")
            params.append(json.dumps(frontmatter))
        return " AND ".join(clauses), params

    def search(
        self, query: list[float], k: int = 5, corpus: Optional[str] = None,
        frontmatter: Optional[dict] = None,
    ) -> list[SearchHit]:
        """Pure vector (cosine) search. Hybrid search is preferred for the tool;
        this stays for callers/tests that want vector-only ranking."""
        qlit = _vec_literal(query)
        clause, fparams = self._filters(corpus, frontmatter)
        where = f" WHERE {clause}" if clause else ""
        sql = (
            f"SELECT {_CHUNK_COLS}, 1 - (embedding <=> %s::vector) AS score "
            f"FROM chunks{where} ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        params = [qlit, *fparams, qlit, max(1, k)]
        with self._pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [SearchHit(chunk=_row_to_chunk(r), score=float(r[8]), sources=("vector",)) for r in rows]

    def hybrid_search(
        self, query_text: str, query_vec: list[float], k: int = 5,
        corpus: Optional[str] = None, candidates: int = 20,
        frontmatter: Optional[dict] = None,
    ) -> list[SearchHit]:
        """Fuse vector and full-text candidate lists with RRF.

        Pulls up to ``candidates`` from each arm, fuses by id, returns the top
        ``k``. ``sources`` records which arm(s) matched each hit. An optional
        ``frontmatter`` JSONB filter is applied to both arms.
        """
        qlit = _vec_literal(query_vec)
        cand = max(k, candidates)
        clause, fparams = self._filters(corpus, frontmatter)
        where = f" WHERE {clause}" if clause else ""

        vec_sql = f"SELECT {_CHUNK_COLS} FROM chunks{where} ORDER BY embedding <=> %s::vector LIMIT %s"
        vec_params: list = [*fparams, qlit, cand]

        fts_sql = (
            f"SELECT {_CHUNK_COLS} FROM chunks "
            "WHERE tsv @@ plainto_tsquery('english', %s)"
            + (f" AND {clause}" if clause else "")
        )
        fts_params: list = [query_text, *fparams]
        fts_sql += " ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC LIMIT %s"
        fts_params.extend([query_text, cand])

        with self._pool.connection() as conn:
            vec_rows = conn.execute(vec_sql, vec_params).fetchall()
            fts_rows = conn.execute(fts_sql, fts_params).fetchall()

        by_id: dict[str, StoredChunk] = {}
        for r in (*vec_rows, *fts_rows):
            by_id.setdefault(r[0], _row_to_chunk(r))
        vec_ids = [r[0] for r in vec_rows]
        fts_ids = [r[0] for r in fts_rows]
        vec_set, fts_set = set(vec_ids), set(fts_ids)

        fused = _rrf_fuse([vec_ids, fts_ids])
        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[: max(1, k)]

        hits: list[SearchHit] = []
        for cid, score in ranked:
            srcs = tuple(s for s, present in (("vector", cid in vec_set), ("keyword", cid in fts_set)) if present)
            hits.append(SearchHit(chunk=by_id[cid], score=float(score), sources=srcs))
        return hits

    def corpora(self) -> dict[str, int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT corpus, COUNT(*) FROM chunks GROUP BY corpus ORDER BY corpus"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def facets(self) -> dict[str, list[str]]:
        """Distinct frontmatter facets across all chunks — the values an agent can
        filter by (doc/OKF types and tags). Powers the memory_index orientation
        tool so Devy can see what's filterable before querying."""
        with self._pool.connection() as conn:
            type_rows = conn.execute(
                "SELECT DISTINCT metadata->>'doc_type' FROM chunks "
                "WHERE metadata ? 'doc_type' ORDER BY 1"
            ).fetchall()
            # tags is a JSON array; unnest distinct values.
            tag_rows = conn.execute(
                "SELECT DISTINCT jsonb_array_elements_text(metadata->'tags') AS tag "
                "FROM chunks WHERE jsonb_typeof(metadata->'tags') = 'array' ORDER BY tag"
            ).fetchall()
        return {
            "doc_types": [r[0] for r in type_rows if r[0]],
            "tags": [r[0] for r in tag_rows if r[0]],
        }

    def count(self) -> int:
        with self._pool.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
