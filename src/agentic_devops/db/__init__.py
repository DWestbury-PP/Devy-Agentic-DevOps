"""Postgres connectivity for the proxy.

One Postgres (with the pgvector extension) backs everything that must persist
across restarts: the knowledge base (chunks + embeddings) and conversation
history (sessions). The DSN is the single deployment knob — point it at the
bundled compose container or a managed instance (RDS/Aurora); nothing else
changes. See ``schema.sql`` for the idempotent bootstrap and ``db init``.
"""

from __future__ import annotations

from agentic_devops.db.connection import apply_schema, close_all, get_pool, schema_sql

__all__ = ["apply_schema", "close_all", "get_pool", "schema_sql"]
