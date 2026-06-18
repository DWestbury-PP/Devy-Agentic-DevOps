"""Ingestion pipeline: sweep a directory → chunk → embed → upsert.

Idempotent: a file whose chunks are byte-identical to what's already stored is
skipped (no re-embedding); a changed file has its old chunks dropped and
re-embedded. So you can re-run ``agentic-devops ingest .`` after editing docs and
only pay for what changed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from agentic_devops.knowledge.chunking import chunk_markdown
from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.enrich import Enricher, doc_title, doc_type, lineage_context
from agentic_devops.knowledge.store import StoredChunk, VectorStore

DEFAULT_EXTENSIONS = (".md", ".markdown", ".txt", ".rst")
# Bump when the embedding recipe changes (e.g. how context is prepended) so a
# re-ingest re-embeds existing chunks even when their source text is unchanged.
_EMBED_RECIPE = "ctx2"
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "dist", "build", "traces", "sessions", ".idea", ".vscode",
}


@dataclass
class IngestStats:
    corpus: str
    files_seen: int = 0
    files_ingested: int = 0
    files_skipped: int = 0  # unchanged since last ingest
    chunks_written: int = 0
    chunks_contextualized: int = 0  # chunks that got a contextual prefix


@dataclass
class IngestResult:
    """Outcome of ingesting one document's markdown."""

    chunks_written: int = 0
    contextualized: int = 0
    skipped: bool = False  # unchanged since last ingest (same recipe + text)


def sweep(root: Path, extensions: Iterable[str] = DEFAULT_EXTENSIONS) -> list[Path]:
    """Recursively collect ingestable files, skipping noise dirs."""
    exts = {e.lower() for e in extensions}
    if root.is_file():
        return [root] if root.suffix.lower() in exts else []
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in exts:
            found.append(path)
    return found


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def content_hash(text: str) -> str:
    """Stable hash of a whole document's raw markdown (for the documents registry)."""
    return _hash(text)


def ingest_markdown(
    raw: str,
    corpus: str,
    source_path: str,
    store: VectorStore,
    embedder: Embedder,
    *,
    split_level: int = 2,
    max_chars: int = 8000,
    overlap: int = 200,
    enricher: Optional[Enricher] = None,
    document_id: Optional[str] = None,
) -> IngestResult:
    """Ingest ONE document's markdown into the vector store (chunks only).

    Pure knowledge-layer: chunks → deterministic ``title > heading_path`` context
    (+ optional LLM synopsis) → embed → upsert, stamping ``document_id`` on each
    chunk. Idempotent — returns ``skipped`` when the source + recipe are unchanged.
    The documents-table lifecycle is the caller's (CLI / worker).
    """
    chunks = chunk_markdown(raw, max_chars=max_chars, overlap=overlap, split_level=split_level)
    if not chunks:
        return IngestResult()

    enrich_on = enricher is not None and enricher.active
    marker = f"\x00{_EMBED_RECIPE}" + ("\x00haiku" if enrich_on else "")
    new_hashes = {_hash(c.text + marker) for c in chunks}
    if new_hashes == store.hashes_for_source(corpus, source_path):
        return IngestResult(skipped=True)

    title = doc_title(raw, fallback=Path(source_path).stem)
    dtype = doc_type(source_path, raw)

    # Context prepended before embedding: a deterministic `title > heading_path`
    # lineage (always, free) + an optional LLM synopsis on top (when enabled).
    prefixes: list[str] = []
    contextualized = 0
    for c in chunks:
        parts = [lineage_context(title, c.heading_path)]
        if enrich_on:
            blurb = enricher.contextualize(raw, c.text)
            if blurb:
                parts.append(blurb)
                contextualized += 1
        prefixes.append("\n".join(p for p in parts if p))

    embeddings = embedder.embed(
        [f"{p}\n\n{c.text}" if p else c.text for p, c in zip(prefixes, chunks)]
    )
    stored = [
        StoredChunk(
            id=f"{corpus}:{source_path}:{c.index}",
            corpus=corpus,
            source_path=source_path,
            heading_path=c.heading_path,
            text=c.text,
            content_hash=_hash(c.text + marker),
            context_prefix=prefix,
            metadata=Enricher.metadata_for(title, dtype, c.heading_path),
            document_id=document_id,
        )
        for c, prefix in zip(chunks, prefixes)
    ]
    store.delete_source(corpus, source_path)  # drop stale chunks before re-inserting
    store.upsert(stored, embeddings)
    return IngestResult(chunks_written=len(stored), contextualized=contextualized)


def ingest_path(
    root: Path | str,
    store: VectorStore,
    embedder: Embedder,
    corpus: Optional[str] = None,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    max_chars: int = 8000,
    overlap: int = 200,
    split_level: int = 2,
    enricher: Optional[Enricher] = None,
    document_store: Any = None,
) -> IngestStats:
    """Ingest every matching file under ``root`` into ``corpus``.

    ``corpus`` defaults to the directory (or file stem) name. When ``enricher``
    is active, each chunk gets an LLM synopsis on top of the deterministic
    context; otherwise ingest uses the free structural context only. When a
    ``document_store`` is given, each file is registered in the unified document
    registry and its chunks back-link via ``document_id`` (so CLI-ingested
    corpora appear in the Knowledge UI).
    """
    root = Path(root).expanduser().resolve()
    corpus = corpus or (root.name if root.is_dir() else root.stem)
    stats = IngestStats(corpus=corpus)

    files = sweep(root, extensions)
    base = root if root.is_dir() else root.parent

    for path in files:
        stats.files_seen += 1
        try:
            raw = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # skip binaries / unreadable files silently
        rel = str(path.relative_to(base)) if base in path.parents or base == path.parent else str(path)

        document_id = None
        if document_store is not None:
            doc = document_store.register(
                corpus, rel, title=doc_title(raw, fallback=Path(rel).stem),
                doc_type=doc_type(rel, raw), content=raw, content_hash=content_hash(raw),
                bytes_=len(raw.encode("utf-8")), status="ready",
            )
            document_id = doc.id

        result = ingest_markdown(
            raw, corpus, rel, store, embedder, split_level=split_level,
            max_chars=max_chars, overlap=overlap, enricher=enricher, document_id=document_id,
        )
        if result.skipped:
            stats.files_skipped += 1
            continue
        stats.files_ingested += 1
        stats.chunks_written += result.chunks_written
        stats.chunks_contextualized += result.contextualized
        if document_store is not None and document_id:
            document_store.set_status(document_id, "ready", chunk_count=result.chunks_written)

    return stats
