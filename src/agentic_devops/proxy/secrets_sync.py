"""Out-of-band secret sync: dev (LocalStack) → real AWS Secrets Manager.

A bootstrap/rotation convenience for the common loop of *developing locally, then
pushing the same secret catalog up to a real AWS deployment*. It is deliberately
NOT part of the app runtime or the CD deploy — it runs with an operator's admin
AWS credentials (which may create/put secrets), whereas the deployed instance role
only ever *reads* at runtime (least privilege).

Design points:
* **Ref parity is the whole trick.** Dev and prod resolve the *same* refs
  (``devy/provider/*``, ``devy/github/*``, ``devy/host/*``) — only the backend
  differs. So a sync just makes the same names present in the other backend; the
  app is unchanged.
* **Idempotent + diff-aware.** Re-runnable: each ref is add / update / unchanged.
  Re-running after a local change pushes only the delta — this doubles as the
  rotation path.
* **Values never touch disk or logs.** Source→target copy happens in-process; the
  plan prints masked lengths only, never the value.
* **Non-destructive.** A ref present in AWS but absent in dev is left alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


def known_secret_refs(settings: Any, secrets: Any, pool: Any) -> list[tuple[str, str]]:
    """The canonical ref inventory Devy manages: provider keys + connector tokens.

    Shared by ``secrets list`` and ``secrets sync`` so both agree on exactly which
    refs exist (a correctness property — a sync must cover what list reports).
    ``pool`` may be None (registry unavailable → provider keys only).
    """
    from agentic_devops.proxy.secrets import provider_key_refs

    refs: list[tuple[str, str]] = [
        (ref, "provider key") for ref in provider_key_refs(settings.secrets.namespace)
    ]
    if pool is not None:
        from agentic_devops.proxy.github import GitHubAccountStore
        from agentic_devops.proxy.hosts import HostStore

        refs += [
            (a.secret_ref, f"github:{a.label}")
            for a in GitHubAccountStore(pool, secrets).list() if a.secret_ref
        ]
        refs += [
            (h.secret_ref, f"host:{h.fqdn}")
            for h in HostStore(pool, secrets).list() if h.secret_ref
        ]
    return refs


@dataclass(frozen=True)
class SyncItem:
    ref: str
    kind: str                    # provider key | github:… | host:…
    action: str                  # add | update | unchanged | absent
    src_len: Optional[int]       # length of the source value (None if absent in dev)
    dst_len: Optional[int]       # length of the current target value (None if not there)


def plan_sync(source: Any, target: Any, refs: list[tuple[str, str]]) -> list[SyncItem]:
    """Classify each ref by comparing source (dev) vs target (AWS). Reads values to
    diff but never returns them — only lengths, for a masked plan."""
    items: list[SyncItem] = []
    for ref, kind in refs:
        sv = source.get(ref)
        if sv is None:
            items.append(SyncItem(ref, kind, "absent", None, None))
            continue
        tv = target.get(ref)
        if tv is None:
            action = "add"
        elif tv == sv:
            action = "unchanged"
        else:
            action = "update"
        items.append(SyncItem(ref, kind, action, len(sv), len(tv) if tv is not None else None))
    return items


def apply_sync(
    source: Any, target: Any, items: list[SyncItem]
) -> tuple[list[SyncItem], list[tuple[str, str]]]:
    """Write add/update items into the target. Returns (applied, failed) where
    failed is [(ref, error)]. Re-reads the source value at write time so nothing is
    held in a structure longer than needed."""
    applied: list[SyncItem] = []
    failed: list[tuple[str, str]] = []
    for it in items:
        if it.action not in ("add", "update"):
            continue
        value = source.get(it.ref)
        if value is None:
            failed.append((it.ref, "value vanished from source mid-sync"))
            continue
        try:
            target.set(it.ref, value)
            applied.append(it)
        except Exception as exc:  # noqa: BLE001 — report per-ref, keep going
            failed.append((it.ref, f"{type(exc).__name__}: {exc}"))
    return applied, failed


def build_aws_target_provider(region: str, profile: Optional[str] = None) -> Any:
    """A writable SecretsProvider bound to REAL AWS SM (no LocalStack endpoint),
    authenticated by the operator's default cred chain (or ``profile``). cache_ttl=0
    so diffs always read fresh."""
    import boto3

    from agentic_devops.proxy.secrets import SecretsProvider

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    client = session.client("secretsmanager", region_name=region)
    return SecretsProvider(client, writable=True, cache_ttl=0)


def target_identity(region: str, profile: Optional[str] = None) -> tuple[str, str]:
    """(account_id, caller_arn) for the target — so the operator confirms WHICH
    AWS account is about to be written to."""
    import boto3

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    ident = session.client("sts", region_name=region).get_caller_identity()
    return ident["Account"], ident["Arn"]


def mask(length: Optional[int]) -> str:
    return "—" if not length else f"•••• (len {length})"
