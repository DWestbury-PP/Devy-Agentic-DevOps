"""Build the knowledge store + embedder from settings.

One place so the proxy (app.py) and the ``ingest`` CLI construct them
identically. The store is Postgres/pgvector backed by the shared connection pool
(``database.url``); the embedder is configured separately (Anthropic has no
embeddings endpoint).
"""

from __future__ import annotations

from typing import Optional

from agentic_devops.config import DatabaseConfig, KnowledgeConfig, Settings
from agentic_devops.db import get_pool
from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.enrich import Enricher, make_context_fn
from agentic_devops.knowledge.store import PgVectorStore


def build_store(database: DatabaseConfig) -> PgVectorStore:
    return PgVectorStore(get_pool(database.url))


def build_embedder(cfg: KnowledgeConfig) -> Embedder:
    return Embedder(
        model=cfg.embedding.model,
        api_base=cfg.embedding.api_base,
        batch_size=cfg.embedding.batch_size,
    )


def build_enricher(settings: Settings, force: bool = False) -> Optional[Enricher]:
    """An ``Enricher`` backed by the ``fast`` tier, or ``None`` when disabled.

    Returns ``None`` (deterministic-context-only ingest, no LLM calls) unless the
    LLM synopsis is enabled — either by config (``contextual_enabled``) or an
    explicit ``force`` (e.g. the CLI ``--context`` flag). Also ``None`` when the
    ``fast`` tier isn't configured, so ingest degrades gracefully.
    """
    if not (force or settings.knowledge.contextual_enabled):
        return None
    try:
        tier = settings.resolve_tier("fast")
    except KeyError:
        return None
    from agentic_devops.proxy.providers import ProviderClient

    return Enricher(
        context_fn=make_context_fn(ProviderClient(), tier),
        enabled=True,
        max_doc_chars=settings.knowledge.contextual_max_doc_chars,
    )
