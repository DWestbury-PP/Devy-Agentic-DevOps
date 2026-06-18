"""Load the host MCP's allow-list and server settings.

Everything is overridable by environment variables so the same image/binary can
be deployed with different postures per host:

  HOST_MCP_ALLOWLIST   path to an allow-list YAML (defaults to the packaged one)
  HOST_MCP_PROFILE     read-only | diagnostic | elevated (overrides the file)
  HOST_MCP_AUDIT       path to a JSONL audit log
  HOST_MCP_TRANSPORT   stdio | http
  HOST_MCP_HOST        bind host for http (default 0.0.0.0)
  HOST_MCP_PORT        bind port for http (default 8780)
  HOST_MCP_TOKEN       bearer token required for http requests
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


def load() -> ServerConfig:
    data = yaml.safe_load(_allowlist_path().read_text(encoding="utf-8")) or {}

    audit = os.environ.get("HOST_MCP_AUDIT")
    allowlist = Allowlist.from_dict(
        data,
        active_profile=os.environ.get("HOST_MCP_PROFILE"),
        audit_path=Path(audit).expanduser() if audit else None,
    )

    return ServerConfig(
        allowlist=allowlist,
        transport=os.environ.get("HOST_MCP_TRANSPORT", data.get("transport", "stdio")),
        host=os.environ.get("HOST_MCP_HOST", data.get("host", "0.0.0.0")),
        port=int(os.environ.get("HOST_MCP_PORT", data.get("port", 8780))),
        token=os.environ.get("HOST_MCP_TOKEN", data.get("token")),
    )
