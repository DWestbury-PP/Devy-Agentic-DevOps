"""Attachment metadata + the one-time vision digest (attachments Phase 3).

The raw bytes live in the S3 blob store (Phase 1); this tracks each unique image's
metadata and its **digest** — a durable text description generated ONCE per image
(dedup by hash) so later turns carry the description instead of re-processing the
pixels. That is the "process once" property: pixels enter the model only on the
turn an image is attached (or when ``view_image`` re-pulls them); every later turn
uses the digest text. See ``.claude/plans/multimodal-attachments.md``.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Optional

from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

# Faithful-transcription prompt: these images are CLI output, dashboards, consoles
# — dense text/numbers where accuracy matters (the digest is authoritative + reused).
_DIGEST_PROMPT = (
    "You are creating a durable, faithful reference description of an image for later "
    "use in a technical DevOps conversation. Transcribe and describe EVERYTHING visibly "
    "present: all text, numbers, error messages, log lines, chart values / legends / axes, "
    "table contents, UI state, timestamps, and container/service names. Do not interpret, "
    "speculate, or add commentary — only what is visibly in the image. Be thorough and "
    "precise; this description stands in for the image in later turns."
)


class AttachmentStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record(self, digest_hash: str, mime: str, size: int) -> None:
        """Register a stored image (idempotent — dedup by hash)."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO attachments (hash, mime, bytes) VALUES (%s, %s, %s) "
                "ON CONFLICT (hash) DO NOTHING",
                (digest_hash, mime, size),
            )

    def get_digest(self, digest_hash: str) -> Optional[str]:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT digest FROM attachments WHERE hash = %s AND digest_status = 'ready'",
                (digest_hash,),
            ).fetchone()
        return row[0] if row else None

    def set_digest(self, digest_hash: str, digest: str, tier: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE attachments SET digest = %s, digest_status = 'ready', digest_tier = %s "
                "WHERE hash = %s",
                (digest, tier, digest_hash),
            )

    def mark(self, digest_hash: str, status: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE attachments SET digest_status = %s WHERE hash = %s", (status, digest_hash)
            )


class DigestService:
    """Generates + caches the one-time vision digest for an image."""

    def __init__(self, store: AttachmentStore, blob_store: Any, provider: Any, settings: Any) -> None:
        self._store = store
        self._blobs = blob_store
        self._provider = provider
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return (
            self._blobs is not None
            and getattr(self._settings.attachments, "digest_enabled", False)
        )

    def ensure(self, digest_hash: str) -> Optional[str]:
        """Return the image's digest, generating it once if needed. ``None`` if
        disabled, the blob is gone, or generation failed (caller falls back to a
        bare placeholder)."""
        cached = self._store.get_digest(digest_hash)
        if cached:
            return cached
        if not self.enabled:
            return None
        got = self._blobs.get(digest_hash)
        if got is None:
            return None
        data, mime = got
        try:
            tier = self._settings.resolve_tier(self._settings.attachments.digest_tier)
        except Exception:  # noqa: BLE001 — bad tier name → skip digesting
            self._store.mark(digest_hash, "skipped")
            return None
        b64 = base64.b64encode(data).decode()
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": _DIGEST_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }]
        try:
            resp = self._provider.complete(messages, tier, None)
            digest = (resp.text or "").strip()
        except Exception as exc:  # noqa: BLE001 — vision call failed (non-vision tier, provider) → skip
            logger.warning("digest generation failed for %s (%s)", digest_hash[:12], exc)
            self._store.mark(digest_hash, "error")
            return None
        if not digest:
            self._store.mark(digest_hash, "error")
            return None
        self._store.set_digest(digest_hash, digest, tier.model)
        return digest
