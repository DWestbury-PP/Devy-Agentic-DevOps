"""Conversation memory: retrieval-over-history (Phase 8).

Each exchange (user turn + Devy's answer, plus that turn's tool findings) is
embedded and stored in ``conversation_memories``. The ``recall_history`` tool
searches it — scoped to the current conversation or across all of a user's past
conversations — so Devy can pull back the *specifics* that compaction dropped
(exact values, error strings, which host) and recognise recurring incidents.

Same pgvector machinery as the knowledge base (`PgVectorStore`): vectors written
as ``%s::vector`` literals, exact cosine search via ``<=>``. Reuses the
configured embedder, so it stays provider-agnostic and dimension-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from psycopg_pool import ConnectionPool

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.store import _vec_literal


@dataclass
class MemoryHit:
    session_id: str
    turn: int
    text: str
    created_at: str  # ISO-8601
    score: float  # cosine similarity in [-1, 1]


class ConversationMemoryStore:
    """Embedded conversation history, searchable per user / per session."""

    def __init__(self, pool: ConnectionPool, embedder: Embedder) -> None:
        self._pool = pool
        self._embedder = embedder

    def add_exchange(
        self, session_id: str, user_id: Optional[str], turn: int, text: str
    ) -> None:
        """Embed and upsert one exchange. Idempotent on (session_id, turn)."""
        if not text.strip():
            return
        vec = self._embedder.embed_query(text)
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO conversation_memories (id, session_id, user_id, turn, text, embedding)
                VALUES (%s, %s, %s, %s, %s, %s::vector)
                ON CONFLICT (id) DO UPDATE SET
                    text = EXCLUDED.text, embedding = EXCLUDED.embedding,
                    user_id = COALESCE(EXCLUDED.user_id, conversation_memories.user_id)
                """,
                (f"{session_id}:{turn}", session_id, user_id, turn, text, _vec_literal(vec)),
            )

    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        k: int = 5,
        exclude_session: Optional[str] = None,
    ) -> list[MemoryHit]:
        """Cosine-nearest exchanges. Filters: ``session_id`` (this conversation)
        or ``user_id`` (cross-conversation). ``exclude_session`` drops the current
        conversation's own rows (so cross-conversation recall doesn't echo it)."""
        qvec = _vec_literal(self._embedder.embed_query(query))
        sql = (
            "SELECT session_id, turn, text, created_at, "
            "1 - (embedding <=> %s::vector) AS score FROM conversation_memories"
        )
        params: list = [qvec]
        clauses: list[str] = []
        if session_id:
            clauses.append("session_id = %s")
            params.append(session_id)
        if user_id:
            clauses.append("user_id = %s")
            params.append(user_id)
        if exclude_session:
            clauses.append("session_id <> %s")
            params.append(exclude_session)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params.extend([qvec, max(1, k)])
        with self._pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            MemoryHit(
                session_id=r[0], turn=r[1], text=r[2],
                created_at=r[3].isoformat() if r[3] else "", score=float(r[4]),
            )
            for r in rows
        ]

    def count(self) -> int:
        with self._pool.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM conversation_memories").fetchone()[0]

    def delete_session(self, session_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM conversation_memories WHERE session_id = %s", (session_id,)
            )
