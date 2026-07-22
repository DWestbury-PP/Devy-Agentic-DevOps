"""Content-addressed blob store for user-attached images (Phase 1).

One boto3 ``s3`` client backs it, over the SAME AWS wiring as secrets: the
endpoint + credentials come from the environment, so the code path is identical
dev→prod.

- **dev**  → ``AWS_ENDPOINT_URL`` points at LocalStack S3 (path-style addressing);
  dummy creds (LocalStack ignores them).
- **prod** → no endpoint env; boto3's default chain picks up the instance **IAM
  role**. The only operator knob is the bucket name.

Blobs are addressed by ``sha256(bytes)`` — identical images dedupe to one object.
The client is injected (the seam pattern used across the codebase), so tests run
against an in-memory double and the app against boto3 — same code either way.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def content_hash(data: bytes) -> str:
    """The blob's content address."""
    return hashlib.sha256(data).hexdigest()


class BlobStore:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_bucket(self) -> None:
        """Best-effort create the bucket (dev/LocalStack). In prod it's provisioned
        out-of-band (Terraform/CDK); a create failure there is expected and warned."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except Exception:  # noqa: BLE001 — missing/forbidden → try to create
            pass
        try:
            self._client.create_bucket(Bucket=self._bucket)
            logger.info("created blob bucket %r", self._bucket)
        except Exception as exc:  # noqa: BLE001 — never block startup on this
            logger.warning("blob bucket %r not creatable (%s) — assuming provisioned", self._bucket, exc)

    def exists(self, digest: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=digest)
            return True
        except Exception:  # noqa: BLE001 — not found / error → treat as absent
            return False

    def put(self, data: bytes, mime: str = "application/octet-stream") -> str:
        """Store ``data`` (idempotent — dedupes on content hash) and return its
        digest. A repeat of the same bytes is a no-op upload."""
        digest = content_hash(data)
        if not self.exists(digest):
            self._client.put_object(Bucket=self._bucket, Key=digest, Body=data, ContentType=mime)
        return digest

    def get(self, digest: str) -> Optional[tuple[bytes, str]]:
        """Return ``(bytes, mime)`` for a digest, or ``None`` if absent."""
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=digest)
            body = resp["Body"].read()
            mime = resp.get("ContentType") or "application/octet-stream"
            return body, mime
        except Exception:  # noqa: BLE001 — missing key / error → None
            return None


def build_blob_store(settings: Any) -> Optional[BlobStore]:
    """Construct the store from settings, or ``None`` when attachments are off.

    Uses boto3's env-driven config (``AWS_ENDPOINT_URL`` + creds + region), so
    LocalStack in dev and real S3 in prod are the same client. Path-style
    addressing is forced when an endpoint is set (LocalStack needs it; virtual-
    hosted bucket DNS doesn't resolve there)."""
    ac = getattr(settings, "attachments", None)
    if ac is None or not ac.enabled:
        return None
    import boto3
    from botocore.config import Config

    region = getattr(getattr(settings, "secrets", None), "region", None) or "us-east-1"
    kwargs: dict[str, Any] = {"region_name": region}
    if os.environ.get("AWS_ENDPOINT_URL"):  # dev/LocalStack → path-style
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    client = boto3.client("s3", **kwargs)

    store = BlobStore(client, ac.bucket)
    store.ensure_bucket()
    return store
