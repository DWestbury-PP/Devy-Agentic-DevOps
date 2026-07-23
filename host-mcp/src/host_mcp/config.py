"""Load the host MCP's allow-list and server settings.

Everything is overridable by environment variables so the same image/binary can
be deployed with different postures per host:

  HOST_MCP_ALLOWLIST        path to an allow-list YAML (defaults to the packaged one)
  HOST_MCP_PROFILE          read-only | diagnostic | elevated (overrides the file)
  HOST_MCP_ALLOW_MUTATIONS  enable state-changing checks (default OFF; see below)
  HOST_MCP_AUDIT            path to a JSONL audit log
  HOST_MCP_TRANSPORT        stdio | http
  HOST_MCP_HOST             bind host for http (default 0.0.0.0)
  HOST_MCP_PORT             bind port for http (default 8780)
  HOST_MCP_TOKEN            bearer token required for http requests

``HOST_MCP_ALLOW_MUTATIONS`` is the dedicated, default-off switch that governs
whether the sidecar can perform ANY mutating (state-changing) check — a single,
auditable control independent of the read profile, so SecOps decides "can this
host act?" explicitly. It is fail-closed for the network case: enabling mutations
over ``http`` WITHOUT a ``HOST_MCP_TOKEN`` refuses to start (a mutating endpoint
must never be reachable unauthenticated). stdio has no auth by design (local pipe).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from host_mcp.allowlist import Allowlist

DEFAULT_ALLOWLIST = Path(__file__).parent / "default_allowlist.yaml"


@dataclass
class ServerConfig:
    allowlist: Allowlist
    transport: str
    host: str
    port: int
    token: Optional[str]


def _allowlist_path() -> Path:
    env = os.environ.get("HOST_MCP_ALLOWLIST")
    if env:
        return Path(env).expanduser()
    return DEFAULT_ALLOWLIST


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def load() -> ServerConfig:
    data = yaml.safe_load(_allowlist_path().read_text(encoding="utf-8")) or {}

    audit = os.environ.get("HOST_MCP_AUDIT")
    allow_mutations = _env_bool("HOST_MCP_ALLOW_MUTATIONS")
    allowlist = Allowlist.from_dict(
        data,
        active_profile=os.environ.get("HOST_MCP_PROFILE"),
        audit_path=Path(audit).expanduser() if audit else None,
        allow_mutations=allow_mutations,
    )

    transport = os.environ.get("HOST_MCP_TRANSPORT", data.get("transport", "stdio"))
    token = os.environ.get("HOST_MCP_TOKEN", data.get("token"))

    # Fail-closed: a mutating sidecar must never be reachable over the network
    # without auth. Enabling mutations on http with no bearer token is a refusal,
    # not a warning. (stdio is a local pipe — not network-reachable — so it's exempt.)
    if allow_mutations and transport == "http" and not token:
        raise SystemExit(
            "REFUSING TO START: HOST_MCP_ALLOW_MUTATIONS is enabled with http "
            "transport but no HOST_MCP_TOKEN is set. A network-reachable mutating "
            "endpoint requires a bearer token. Set HOST_MCP_TOKEN, disable "
            "mutations, or use stdio transport."
        )

    return ServerConfig(
        allowlist=allowlist,
        transport=transport,
        host=os.environ.get("HOST_MCP_HOST", data.get("host", "0.0.0.0")),
        port=int(os.environ.get("HOST_MCP_PORT", data.get("port", 8780))),
        token=token,
    )
