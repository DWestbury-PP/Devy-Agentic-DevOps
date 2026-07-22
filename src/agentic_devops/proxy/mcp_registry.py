"""MCP Servers registry (Phase S-4): general MCP tool sources.

DB-backed registry of HTTP MCP servers Devy mounts as tools (Grafana, CloudWatch,
3rd-party, …) — distinct from the ``hosts`` registry (diagnostic targets). The
bearer token never lives in this DB: the row holds a ``secret_ref`` resolved from
the secrets manager on demand. Tool schemas are captured at register/refresh and
normalized into the tools-router; execution dials the server on demand.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from psycopg_pool import ConnectionPool

from agentic_devops.proxy.secrets import SecretsProvider

_COLS = (
    "id, name, url, secret_ref, category, description, allow_writes, enabled, "
    "last_status, tool_count, write_tool_count, created_at, updated_at, auth_header"
)
_FIELDS = ("name", "url", "category", "description", "allow_writes", "enabled", "auth_header")


def mcp_secret_ref(name: str, namespace: str = "devy") -> str:
    from agentic_devops.proxy.secrets import _slug

    return f"{namespace}/mcp/{_slug(name)}"


@dataclass
class MCPServer:
    id: str
    name: str
    url: str
    secret_ref: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    allow_writes: bool = False
    enabled: bool = True
    last_status: Optional[str] = None
    tool_count: int = 0
    write_tool_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Non-standard auth header name a server wants the vault token in (e.g. the
    # Grafana MCP wants `X-Grafana-Api-Key`, not `Authorization: Bearer`). NULL/None
    # → the default `Authorization: Bearer <token>`.
    auth_header: Optional[str] = None
    has_token: bool = False

    @property
    def tool_category(self) -> str:
        return self.category or self.name


@dataclass
class ResolvedServer:
    server: MCPServer
    url: str
    token: Optional[str]
    auth_header: Optional[str] = None


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _row(r: tuple) -> MCPServer:
    return MCPServer(
        id=r[0], name=r[1], url=r[2], secret_ref=r[3], category=r[4], description=r[5],
        allow_writes=r[6], enabled=r[7], last_status=r[8], tool_count=r[9],
        write_tool_count=r[10], created_at=_iso(r[11]), updated_at=_iso(r[12]),
        auth_header=r[13],
    )


class MCPServerStore:
    def __init__(self, pool: ConnectionPool, secrets: SecretsProvider, namespace: str = "devy") -> None:
        self._pool = pool
        self._secrets = secrets
        self._ns = namespace

    def _flag(self, s: MCPServer) -> MCPServer:
        s.has_token = bool(s.secret_ref) and self._secrets.exists(s.secret_ref)
        return s

    def list(self, enabled_only: bool = False) -> list[MCPServer]:
        sql = f"SELECT {_COLS} FROM mcp_servers"
        if enabled_only:
            sql += " WHERE enabled"
        sql += " ORDER BY name"
        with self._pool.connection() as conn:
            return [self._flag(_row(r)) for r in conn.execute(sql).fetchall()]

    def get(self, server_id: str) -> Optional[MCPServer]:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLS} FROM mcp_servers WHERE id = %s", (server_id,)).fetchone()
        return self._flag(_row(row)) if row else None

    def create(self, data: dict[str, Any], token: Optional[str] = None) -> MCPServer:
        server_id = uuid.uuid4().hex[:12]
        fields = {k: data[k] for k in _FIELDS if k in data and data[k] is not None}
        fields["id"] = server_id
        secret_ref = data.get("secret_ref") or mcp_secret_ref(fields["name"], self._ns)
        fields["secret_ref"] = secret_ref
        cols = list(fields.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO mcp_servers ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        if token:
            self._secrets.set(secret_ref, token)
        return self.get(server_id)  # type: ignore[return-value]

    def update(self, server_id: str, data: dict[str, Any]) -> Optional[MCPServer]:
        sets, params = [], []
        for k in _FIELDS:
            if k in data and data[k] is not None:
                sets.append(f"{k} = %s")
                params.append(data[k])
        if not sets:
            return self.get(server_id)
        sets.append("updated_at = now()")
        params.append(server_id)
        with self._pool.connection() as conn:
            conn.execute(f"UPDATE mcp_servers SET {', '.join(sets)} WHERE id = %s", tuple(params))
        return self.get(server_id)

    def set_health(self, server_id: str, status: str, tool_count: int, write_tool_count: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE mcp_servers SET last_status = %s, tool_count = %s, write_tool_count = %s, "
                "updated_at = now() WHERE id = %s",
                (status, tool_count, write_tool_count, server_id),
            )

    def delete(self, server_id: str) -> Optional[MCPServer]:
        current = self.get(server_id)
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = %s", (server_id,))
        if current and current.secret_ref and self._secrets.writable:
            try:
                self._secrets.delete(current.secret_ref)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        return current

    def resolve(self, server_id: str) -> Optional[ResolvedServer]:
        s = self.get(server_id)
        if s is None:
            return None
        token = self._secrets.get(s.secret_ref) if s.secret_ref else None
        return ResolvedServer(server=s, url=s.url, token=token, auth_header=s.auth_header)
