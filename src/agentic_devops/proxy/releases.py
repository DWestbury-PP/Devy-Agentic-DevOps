"""The release ledger — Devy's read layer over the CI→CD build manifest.

CI (``.github/workflows/build.yml``) records every build to **SSM Parameter Store**
under ``/devy/builds`` in three shapes (the seam documented in
[`docs/ci-cd.md`](../../../docs/ci-cd.md#2-the-handoff)):

    by-commit/<sha>              -> the full release manifest (JSON)
    by-branch/<branch>/latest    -> <sha>            (newest-on-branch pointer)
    components/<component>/latest -> {image,tag,...}  (newest image per component)

This module is the **single reader** of that ledger. Every surface — the ``releases``
CLI, the ``/v1/admin/releases`` API, and the ``list_releases`` agent tool — goes
through :class:`ReleaseLedger`, so the schema is parsed in exactly one place and no
surface re-derives it from raw JSON.

It is strictly **read-only** (the ledger is written by CI, never by Devy) and
**best-effort**: an unreachable ledger (e.g. the instance role lacks
``ssm:GetParameter*`` on ``/devy/builds/*``) degrades to empty results + ``health()``
=> False, never an exception that would take a surface down.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger("agentic_devops.releases")


def safe_branch(branch: str) -> str:
    """Sanitize a branch name to the ledger's key form — identical to ``build.yml``'s
    ``tr '/' '-' | tr -cd 'A-Za-z0-9._-'`` (so ``feat/foo`` → ``feat-foo``)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", branch.replace("/", "-"))
    return cleaned or "detached"


def _is_not_found(exc: Exception) -> bool:
    """True for an SSM 'parameter does not exist' error, across the real boto3
    client (``ParameterNotFound`` / a ``ClientError`` with that code) and the fake."""
    if exc.__class__.__name__ == "ParameterNotFound":
        return True
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return resp.get("Error", {}).get("Code") == "ParameterNotFound"
    return False


@dataclass(frozen=True)
class Component:
    """One built component within a release (or a per-component 'latest' pointer)."""

    component: str
    image: str
    tag: str = ""
    digest: str = ""
    repository: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Release:
    """A recorded build manifest (the ``by-commit/<sha>`` value), parsed + typed."""

    sha: str
    short_sha: str = ""
    branch: str = ""
    ref: str = ""
    built_at: str = ""
    label: str = ""
    run_url: str = ""
    actor: str = ""
    status: str = ""  # "complete" | "partial"
    components: dict[str, Component] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """A coherent, deployable whole-platform release (all requested components)."""
        return self.status == "complete"

    @classmethod
    def from_manifest(cls, data: dict[str, Any]) -> "Release":
        comps: dict[str, Component] = {}
        for name, c in (data.get("components") or {}).items():
            c = c or {}
            comps[name] = Component(
                component=name,
                image=c.get("image", ""),
                tag=c.get("tag", ""),
                digest=c.get("digest", ""),
                repository=c.get("repository", ""),
            )
        return cls(
            sha=data.get("sha", ""),
            short_sha=data.get("short_sha", ""),
            branch=data.get("branch", ""),
            ref=data.get("ref", ""),
            built_at=data.get("built_at", ""),
            label=data.get("label", ""),
            run_url=data.get("run_url", ""),
            actor=data.get("actor", ""),
            status=data.get("status", ""),
            components=comps,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["components"] = {k: v for k, v in d["components"].items()}
        d["complete"] = self.complete
        return d


class ReleaseLedger:
    """Read-only accessor over the SSM build ledger. Inject any SSM-shaped client
    (real boto3 or a fake) — the surfaces stay hermetic and testable."""

    def __init__(self, client: Any, prefix: str = "/devy/builds") -> None:
        self._ssm = client
        self.prefix = "/" + prefix.strip("/")

    # -- low-level SSM reads (never raise for the common 'missing'/'denied' cases) --

    def _get(self, name: str) -> Optional[str]:
        try:
            return self._ssm.get_parameter(Name=name)["Parameter"]["Value"]
        except Exception as exc:  # noqa: BLE001 — classify: missing/denied → None, else re-raise
            if _is_not_found(exc):
                return None
            logger.warning("release ledger get_parameter(%s) failed: %s", name, exc)
            return None

    def _by_path(self, subpath: str) -> list[tuple[str, str]]:
        path = f"{self.prefix}/{subpath.strip('/')}"
        out: list[tuple[str, str]] = []
        token: Optional[str] = None
        try:
            while True:
                kwargs: dict[str, Any] = {"Path": path, "Recursive": True, "MaxResults": 10}
                if token:
                    kwargs["NextToken"] = token
                resp = self._ssm.get_parameters_by_path(**kwargs)
                out.extend((p["Name"], p["Value"]) for p in resp.get("Parameters", []))
                token = resp.get("NextToken")
                if not token:
                    break
        except Exception as exc:  # noqa: BLE001 — best-effort: an unreadable path is empty
            logger.warning("release ledger get_parameters_by_path(%s) failed: %s", path, exc)
        return out

    # -- public API -----------------------------------------------------------------

    def health(self) -> bool:
        """Can we read the ledger at all? (Cheap MaxResults=1 probe — surfaces the
        'instance role lacks ssm:GetParameter* on the prefix' case explicitly.)"""
        try:
            self._ssm.get_parameters_by_path(Path=self.prefix, Recursive=True, MaxResults=1)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info("release ledger unreachable at %s: %s", self.prefix, exc)
            return False

    def latest_sha_on_branch(self, branch: str) -> Optional[str]:
        val = self._get(f"{self.prefix}/by-branch/{safe_branch(branch)}/latest")
        return val or None

    def get_release(self, sha: str) -> Optional[Release]:
        raw = self._get(f"{self.prefix}/by-commit/{sha}")
        if not raw:
            return None
        try:
            return Release.from_manifest(json.loads(raw))
        except (ValueError, TypeError) as exc:
            logger.warning("release ledger: malformed manifest at by-commit/%s: %s", sha, exc)
            return None

    def resolve_branch(self, branch: str) -> Optional[Release]:
        """The newest recorded release on ``branch`` (pointer → manifest)."""
        sha = self.latest_sha_on_branch(branch)
        return self.get_release(sha) if sha else None

    def list_releases(self, limit: int = 20) -> list[Release]:
        """All recorded ``by-commit`` releases, newest ``built_at`` first."""
        releases: list[Release] = []
        for _, val in self._by_path("by-commit"):
            try:
                releases.append(Release.from_manifest(json.loads(val)))
            except (ValueError, TypeError):
                continue
        releases.sort(key=lambda r: r.built_at, reverse=True)
        return releases[:limit] if limit else releases

    def components_latest(self) -> dict[str, Component]:
        """The newest image recorded for each component (the ``components/*/latest``
        pointers), keyed by component name."""
        out: dict[str, Component] = {}
        for name, val in self._by_path("components"):
            try:
                data = json.loads(val)
            except (ValueError, TypeError):
                continue
            # name = <prefix>/components/<component>/latest
            parts = name.rstrip("/").split("/")
            comp = parts[-2] if parts[-1] == "latest" and len(parts) >= 2 else parts[-1]
            out[comp] = Component(
                component=comp,
                image=data.get("image", ""),
                tag=data.get("tag", ""),
                digest=data.get("digest", ""),
                repository=data.get("repository", ""),
            )
        return out

    def assembled_latest(self) -> dict[str, str]:
        """The ``assembled-latest`` image map exactly as ``deploy.yml`` computes it:
        ``{repository sans 'devy-' : image}`` from each component's latest pointer."""
        out: dict[str, str] = {}
        for comp in self.components_latest().values():
            repo = comp.repository
            key = repo[len("devy-"):] if repo.startswith("devy-") else repo
            if key and comp.image:
                out[key] = comp.image
        return out


def build_release_ledger(
    settings: Any,
    *,
    region: Optional[str] = None,
    profile: Optional[str] = None,
    endpoint_url: Optional[str] = None,
) -> ReleaseLedger:
    """Construct a :class:`ReleaseLedger` with a real boto3 SSM client.

    In-proxy: ``region`` from settings, credentials from the ambient instance IAM
    role. CLI: pass ``profile``/``region`` to use your SSO credentials (like
    ``secrets sync``). ``endpoint_url`` targets LocalStack for a dev loop; unset =
    real AWS.
    """
    import boto3

    rc = getattr(settings, "releases", None)
    reg = region or (rc.region if rc else None) or settings.secrets.region
    ep = endpoint_url or (rc.endpoint_url if rc else None)
    prefix = rc.ssm_prefix if rc else "/devy/builds"

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    kwargs: dict[str, Any] = {"region_name": reg}
    if ep:
        kwargs["endpoint_url"] = ep
        # LocalStack ignores credential validity but boto3 requires *some* creds.
        kwargs["aws_access_key_id"] = "test"
        kwargs["aws_secret_access_key"] = "test"
    return ReleaseLedger(session.client("ssm", **kwargs), prefix=prefix)
