"""Unified secrets provider — AWS Secrets Manager everywhere (Phase S-1).

One boto3 ``secretsmanager`` client backs every external credential (connector
tokens, provider keys). The *only* thing that differs by deployment mode is the
client's endpoint + credentials and whether writes are allowed:

- **dev**  → LocalStack endpoint + dummy creds; writable. Because LocalStack
  Community doesn't persist across restarts, every write is mirrored to a small
  on-disk ``store_file`` and ``rehydrate()`` re-seeds LocalStack from it on boot —
  so UI-set secrets survive ``docker compose restart``. Reads still go through the
  client (boto3 round-trip), so the dev code path matches prod exactly.
- **prod** → real AWS Secrets Manager via the ambient instance IAM role (no keys
  at rest); **read-only** from the app's view (``set``/``delete`` raise) — secrets
  are provisioned out-of-band by the operator's IaC.

The API never returns a secret value to callers other than the resolve path; the
admin surface only ever learns ``exists`` / can run ``test``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("agentic_devops.secrets")

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _slug(value: str) -> str:
    """Sanitize a business key (label / fqdn) into the AWS SM name charset
    (alphanumeric + ``/_+=.@-``). Stable + human-meaningful so prod IaC can
    provision the same ref out-of-band."""
    return _SLUG_RE.sub("-", value.strip()).strip("-").lower() or "default"


def github_secret_ref(label: str, namespace: str = "devy") -> str:
    return f"{namespace}/github/{_slug(label)}"


def host_secret_ref(fqdn: str, namespace: str = "devy") -> str:
    return f"{namespace}/host/{_slug(fqdn)}"


# Provider/LLM keys live in the same store; hydrated into os.environ at startup so
# LiteLLM/provider SDKs find them. ref → environment variable.
def provider_key_refs(namespace: str = "devy") -> dict[str, str]:
    return {
        f"{namespace}/provider/anthropic": "ANTHROPIC_API_KEY",
        f"{namespace}/provider/openai": "OPENAI_API_KEY",
        f"{namespace}/provider/tavily": "TAVILY_API_KEY",
        f"{namespace}/provider/langsmith": "LANGSMITH_API_KEY",
    }


class SecretsProvider:
    """Thin wrapper over a boto3 ``secretsmanager`` client (or a compatible double).

    The client is injected (the testing/seam pattern used elsewhere), so tests run
    against an in-memory double and the app against boto3 — same code either way.
    """

    def __init__(self, client: Any, *, writable: bool, store_file: Optional[Path] = None) -> None:
        self._client = client
        self._writable = writable
        self._store_file = store_file
        # Mirror used solely to persist the DEV write-through file; reads never use it.
        self._mirror: dict[str, str] = {}

    @property
    def writable(self) -> bool:
        return self._writable

    # -- read -----------------------------------------------------------------
    def get(self, ref: str) -> Optional[str]:
        """The secret value, or None if absent/unreachable (never raises)."""
        try:
            resp = self._client.get_secret_value(SecretId=ref)
            return resp.get("SecretString")
        except self._not_found():
            return None
        except Exception as exc:  # noqa: BLE001 — store unreachable / denied: resolve to None
            logger.warning("secrets.get(%s) failed: %s", ref, exc)
            return None

    def exists(self, ref: str) -> bool:
        try:
            self._client.describe_secret(SecretId=ref)
            return True
        except self._not_found():
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("secrets.exists(%s) failed: %s", ref, exc)
            return False

    def health(self) -> bool:
        """True if the store is reachable (used by the admin panel)."""
        try:
            self._client.list_secrets(MaxResults=1)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("secrets backend unreachable: %s", exc)
            return False

    # -- write (dev only) -----------------------------------------------------
    def set(self, ref: str, value: str) -> None:
        if not self._writable:
            raise PermissionError("secrets are read-only in this mode (provisioned out-of-band)")
        try:
            self._client.create_secret(Name=ref, SecretString=value)
        except self._exists_error():
            self._client.put_secret_value(SecretId=ref, SecretString=value)
        self._mirror[ref] = value
        self._persist()

    def delete(self, ref: str) -> None:
        if not self._writable:
            raise PermissionError("secrets are read-only in this mode (provisioned out-of-band)")
        try:
            self._client.delete_secret(SecretId=ref, ForceDeleteWithoutRecovery=True)
        except self._not_found():
            pass
        self._mirror.pop(ref, None)
        self._persist()

    # -- DEV persistence ------------------------------------------------------
    def rehydrate(self) -> int:
        """Re-seed the store from ``store_file`` on boot (DEV only). Returns the
        number of secrets loaded. No-op when there's no file or store is read-only."""
        if not self._writable or self._store_file is None or not self._store_file.exists():
            return 0
        try:
            data = json.loads(self._store_file.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("secrets store_file unreadable (%s); ignoring", exc)
            return 0
        n = 0
        for ref, value in data.items():
            try:
                self.set(ref, value)
                n += 1
            except Exception as exc:  # noqa: BLE001 — store not up yet; skip
                logger.warning("rehydrate %s failed: %s", ref, exc)
        return n

    def _persist(self) -> None:
        if self._store_file is None:
            return
        try:
            self._store_file.parent.mkdir(parents=True, exist_ok=True)
            self._store_file.write_text(json.dumps(self._mirror, indent=2))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist secrets store_file: %s", exc)

    # -- exception classes (boto3 exposes them on the client) -----------------
    def _not_found(self):
        return getattr(self._client, "exceptions", _NoExc).ResourceNotFoundException

    def _exists_error(self):
        return getattr(self._client, "exceptions", _NoExc).ResourceExistsException


class _NoExc:
    """Fallback so the provider degrades gracefully if a client lacks .exceptions."""

    class ResourceNotFoundException(Exception):
        pass

    class ResourceExistsException(Exception):
        pass


def build_secrets_provider(settings: Any) -> SecretsProvider:
    """Construct the provider from settings: a real boto3 SM client (LocalStack in
    dev via ``endpoint_url`` + dummy creds; real AWS via the IAM-role chain in prod)."""
    import boto3

    sc = settings.secrets
    kwargs: dict[str, Any] = {"region_name": sc.region}
    if sc.endpoint_url:
        kwargs["endpoint_url"] = sc.endpoint_url
        # LocalStack ignores credential validity but boto3 requires *some* creds.
        kwargs["aws_access_key_id"] = sc.access_key or "test"
        kwargs["aws_secret_access_key"] = sc.secret_key or "test"
    # prod: no endpoint, no keys → boto3 default chain picks up the instance IAM role.
    client = boto3.client("secretsmanager", **kwargs)

    writable = sc.mode == "dev"
    store_file = Path(sc.store_file).expanduser() if (writable and sc.store_file) else None
    provider = SecretsProvider(client, writable=writable, store_file=store_file)
    if writable:
        loaded = provider.rehydrate()
        if loaded:
            logger.info("re-hydrated %d secret(s) into the dev store", loaded)
    return provider
