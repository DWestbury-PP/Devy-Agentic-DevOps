"""Evolving fact tier — durable, bi-temporal structured facts (Knowledge Memory, Phase A).

The `chunks` tier holds prose (how/why docs); this tier holds *facts that change*
— `(svc:pricing, port) = 9090` (was 8080) — with history preserved, never
overwritten. It is cross-conversation knowledge memory, distinct from working
memory (`sessions` / `conversation_memories`), which this build does not touch.

A fact's **contradiction slot** is `(subject, attribute)`. Depositing a fact for
an occupied slot *supersedes* the prior one: in a single transaction (under a
per-subject advisory lock) the current row is closed (`valid_to`) and linked
(`superseded_by`), then the new row is inserted. Slotless facts (no
subject/attribute) coexist — they never supersede. Reads default to
currently-true facts; an `as_of` timestamp reconstructs the belief at any point.

Same pgvector machinery as the knowledge base: vectors written as `%s::vector`
literals, hybrid (vector `<=>` + full-text `@@`) fused with RRF — facts benefit
especially from the full-text arm, since exact tokens (ports, hostnames, ARNs)
are what it catches and vectors miss. Reuses the configured `Embedder`, so it
stays provider- and dimension-agnostic.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from psycopg_pool import ConnectionPool

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.redaction import RedactionQuarantine, Redactor
from agentic_devops.knowledge.store import _rrf_fuse, _vec_literal

# Columns selected for a hydrated fact, in order (see _row_to_fact).
_FACT_COLS = (
    "memory_id, content, kind, source, subject, attribute, "
    "valid_from, valid_to, importance, metadata"
)


@dataclass
class StoredFact:
    """A fact as persisted/retrieved, with its temporal validity for citation."""

    memory_id: str
    content: str
    kind: str
    source: str
    subject: Optional[str]
    attribute: Optional[str]
    valid_from: str  # ISO-8601
    valid_to: Optional[str]  # ISO-8601, or None when currently true
    importance: float = 0.5
    metadata: dict = field(default_factory=dict)

    @property
    def is_current(self) -> bool:
        return self.valid_to is None


@dataclass
class FactHit:
    fact: StoredFact
    score: float  # fused RRF score for hybrid search
    sources: tuple[str, ...] = ()  # which retrievers matched: "vector" and/or "keyword"


@dataclass
class AddFactResult:
    memory_id: str
    superseded: list[str] = field(default_factory=list)  # memory_ids this deposit retired


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _row_to_fact(r: tuple) -> StoredFact:
    return StoredFact(
        memory_id=r[0], content=r[1], kind=r[2], source=r[3],
        subject=r[4], attribute=r[5],
        valid_from=_iso(r[6]) or "", valid_to=_iso(r[7]),
        importance=float(r[8]) if r[8] is not None else 0.5,
        metadata=r[9] if isinstance(r[9], dict) else {},
    )


class FactStore:
    """Postgres/pgvector-backed bi-temporal fact store with race-safe supersession."""

    def __init__(
        self, pool: ConnectionPool, embedder: Embedder, redactor: Optional[Redactor] = None
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._redactor = redactor

    # -- write --------------------------------------------------------------
    def add_fact(
        self,
        content: str,
        *,
        kind: str = "semantic",
        source: str,
        subject: Optional[str] = None,
        attribute: Optional[str] = None,
        importance: float = 0.5,
        valid_from: Optional[datetime] = None,
        metadata: Optional[dict] = None,
    ) -> AddFactResult:
        """Deposit a fact; supersede the current fact in its slot if one exists.

        The supersession runs in an explicit ``with conn.transaction():`` — which
        brackets BEGIN/COMMIT even though the pool is autocommit — so the
        per-subject advisory lock and the DEFERRABLE ``superseded_by`` FK behave.
        Returns the new ``memory_id`` and the ids it retired (empty for a first
        fact or a slotless deposit).
        """
        if not content or not content.strip():
            raise ValueError("content is required")
        if kind not in ("semantic", "episodic"):
            raise ValueError(f"kind must be 'semantic' or 'episodic', got {kind!r}")
        # Redact secrets before embed/store. Tier-1 patterns are stripped inline;
        # an ambiguous high-entropy deposit (fail-closed) raises RedactionQuarantine
        # so the caller rejects it rather than silently storing a possible secret.
        if self._redactor is not None:
            red = self._redactor.scan(content)
            if red.quarantine:
                raise RedactionQuarantine(red.summary)
            content = red.text
        memory_id = uuid.uuid4().hex
        when = valid_from or datetime.now(timezone.utc)
        embedding = self._embedder.embed_query(content)
        superseded: list[str] = []

        with self._pool.connection() as conn:
            with conn.transaction():  # explicit tx (pool is autocommit)
                if subject is not None and attribute is not None:
                    # Serialize writers on the same subject; different subjects run
                    # in parallel. Held until this transaction commits.
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (subject,))
                    rows = conn.execute(
                        "SELECT memory_id FROM memories "
                        "WHERE subject = %s AND attribute = %s AND valid_to IS NULL",
                        (subject, attribute),
                    ).fetchall()
                    if rows:
                        superseded = [r[0] for r in rows]
                        # Close + link the prior current fact. Must precede the
                        # INSERT (the partial unique index forbids two current rows
                        # per slot); the FK is deferred so linking a not-yet-inserted
                        # id is fine until COMMIT.
                        conn.execute(
                            "UPDATE memories SET valid_to = %s, superseded_by = %s "
                            "WHERE subject = %s AND attribute = %s AND valid_to IS NULL",
                            (when, memory_id, subject, attribute),
                        )
                conn.execute(
                    "INSERT INTO memories "
                    "(memory_id, content, kind, source, subject, attribute, "
                    " valid_from, recorded_at, importance, metadata, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s::jsonb, %s::vector)",
                    (
                        memory_id, content, kind, source, subject, attribute,
                        when, importance, json.dumps(metadata or {}), _vec_literal(embedding),
                    ),
                )
        return AddFactResult(memory_id=memory_id, superseded=superseded)

    # -- query --------------------------------------------------------------
    def _temporal_clause(
        self, as_of: Optional[datetime], subject: Optional[str]
    ) -> tuple[str, list]:
        """Build the temporal (+subject) WHERE fragment shared by both arms.

        Default (``as_of is None``) = currently-true facts (`valid_to IS NULL`).
        With ``as_of`` = the fact believed at that instant.
        """
        clauses: list[str] = []
        params: list = []
        if as_of is not None:
            clauses.append("valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)")
            params.extend([as_of, as_of])
        else:
            clauses.append("valid_to IS NULL")
        if subject is not None:
            clauses.append("subject = %s")
            params.append(subject)
        return " AND ".join(clauses), params

    def search_facts(
        self,
        query: str,
        *,
        k: int = 5,
        as_of: Optional[datetime] = None,
        subject: Optional[str] = None,
        candidates: int = 20,
    ) -> list[FactHit]:
        """Hybrid (vector + full-text, RRF-fused) search over facts valid now (or
        ``as_of`` a given instant), optionally scoped to a ``subject``."""
        qvec = self._embedder.embed_query(query)
        qlit = _vec_literal(qvec)
        cand = max(k, candidates)
        tclause, tparams = self._temporal_clause(as_of, subject)

        vec_sql = (
            f"SELECT {_FACT_COLS} FROM memories WHERE {tclause} "
            "ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        vec_params = [*tparams, qlit, cand]

        fts_sql = (
            f"SELECT {_FACT_COLS} FROM memories "
            "WHERE tsv @@ plainto_tsquery('english', %s) AND " + tclause + " "
            "ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC LIMIT %s"
        )
        fts_params = [query, *tparams, query, cand]

        with self._pool.connection() as conn:
            vec_rows = conn.execute(vec_sql, vec_params).fetchall()
            fts_rows = conn.execute(fts_sql, fts_params).fetchall()

        by_id: dict[str, StoredFact] = {}
        for r in (*vec_rows, *fts_rows):
            by_id.setdefault(r[0], _row_to_fact(r))
        vec_ids = [r[0] for r in vec_rows]
        fts_ids = [r[0] for r in fts_rows]
        vec_set, fts_set = set(vec_ids), set(fts_ids)

        fused = _rrf_fuse([vec_ids, fts_ids])
        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[: max(1, k)]

        hits: list[FactHit] = []
        for cid, score in ranked:
            srcs = tuple(
                s for s, present in (("vector", cid in vec_set), ("keyword", cid in fts_set)) if present
            )
            hits.append(FactHit(fact=by_id[cid], score=float(score), sources=srcs))
        return hits

    # -- readers (used by tools + tests) -----------------------------------
    def current_for_slot(self, subject: str, attribute: str) -> Optional[StoredFact]:
        """The single currently-true fact in a slot, or None."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_FACT_COLS} FROM memories "
                "WHERE subject = %s AND attribute = %s AND valid_to IS NULL",
                (subject, attribute),
            ).fetchone()
        return _row_to_fact(row) if row else None

    def history_for_slot(self, subject: str, attribute: str) -> list[StoredFact]:
        """Every fact ever recorded in a slot, newest first (current + retired)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_FACT_COLS} FROM memories "
                "WHERE subject = %s AND attribute = %s ORDER BY valid_from DESC",
                (subject, attribute),
            ).fetchall()
        return [_row_to_fact(r) for r in rows]

    def get(self, memory_id: str) -> Optional[StoredFact]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_FACT_COLS} FROM memories WHERE memory_id = %s", (memory_id,)
            ).fetchone()
        return _row_to_fact(row) if row else None

    def superseded_by(self, memory_id: str) -> Optional[str]:
        """The id that retired ``memory_id`` (None if it's current/never retired)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT superseded_by FROM memories WHERE memory_id = %s", (memory_id,)
            ).fetchone()
        return row[0] if row else None

    def count(self, *, current_only: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM memories"
        if current_only:
            sql += " WHERE valid_to IS NULL"
        with self._pool.connection() as conn:
            return conn.execute(sql).fetchone()[0]

    def subjects(self, limit: int = 50) -> list[str]:
        """Distinct subjects with at least one currently-true fact (for orientation)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT subject FROM memories "
                "WHERE valid_to IS NULL AND subject IS NOT NULL ORDER BY subject LIMIT %s",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]
