"""Host registry (Phase 9b): the fleet Devy can run diagnostics against.

A DB-backed registry that generalizes the static ``mcp_servers`` config. Devy
targets a host by identifier (FQDN / instance-id / id) and the proxy resolves it
here to an endpoint + decrypted token — the agent never handles connection
secrets. Per-host MCP tokens are stored encrypted (see ``encryption.py``) and are
never exposed by the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from agentic_devops.proxy.encryption import TokenCipher

# Column order shared by SELECTs (token_encrypted included for internal use only).
_COLS = (
    "id, fqdn, private_ip, public_ip, instance_id, aws_account, aws_region, "
    "mcp_port, mcp_scheme, address_preference, token_encrypted, profile, active, "
    "labels, last_seen_at, last_status, created_at, updated_at"
)


@dataclass
class Host:
    """A registered host (public view — never carries the token)."""

    id: str
    fqdn: str
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    instance_id: Optional[str] = None
    aws_account: Optional[str] = None
    aws_region: Optional[str] = None
    mcp_port: int = 8780
    mcp_scheme: str = "https"
    address_preference: str = "private_ip"
    profile: Optional[str] = None
    active: bool = True
    labels: dict[str, Any] = field(default_factory=dict)
    last_seen_at: Optional[str] = None
    last_status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    has_token: bool = False


@dataclass
class ResolvedHost:
    host: Host
    url: str
    token: Optional[str]


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _address(fqdn, private_ip, public_ip, pref) -> str:
    if pref == "public_ip" and public_ip:
        return public_ip
    if pref == "fqdn":
        return fqdn
    if pref == "private_ip" and private_ip:
        return private_ip
    return private_ip or fqdn or public_ip or fqdn


def _row_to_host(r: tuple) -> Host:
    return Host(
        id=r[0], fqdn=r[1], private_ip=r[2], public_ip=r[3], instance_id=r[4],
        aws_account=r[5], aws_region=r[6], mcp_port=r[7], mcp_scheme=r[8],
        address_preference=r[9], has_token=r[10] is not None, profile=r[11],
        active=r[12], labels=dict(r[13] or {}), last_seen_at=_iso(r[14]),
        last_status=r[15], created_at=_iso(r[16]), updated_at=_iso(r[17]),
    )


# Mutable fields accepted on create/update (token handled separately).
_FIELDS = (
    "fqdn", "private_ip", "public_ip", "instance_id", "aws_account", "aws_region",
    "mcp_port", "mcp_scheme", "address_preference", "profile", "active", "labels",
)


class HostStore:
    def __init__(self, pool: ConnectionPool, cipher: TokenCipher) -> None:
        self._pool = pool
        self._cipher = cipher

    def list(self, active_only: bool = False) -> list[Host]:
        sql = f"SELECT {_COLS} FROM hosts"
        if active_only:
            sql += " WHERE active"
        sql += " ORDER BY fqdn"
        with self._pool.connection() as conn:
            return [_row_to_host(r) for r in conn.execute(sql).fetchall()]

    def get(self, host_id: str) -> Optional[Host]:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLS} FROM hosts WHERE id = %s", (host_id,)).fetchone()
        return _row_to_host(row) if row else None

    def create(self, data: dict[str, Any], token: Optional[str] = None) -> Host:
        host_id = uuid.uuid4().hex[:12]
        fields = {k: data[k] for k in _FIELDS if k in data and data[k] is not None}
        fields["id"] = host_id
        if "labels" in fields:
            fields["labels"] = Json(fields["labels"])
        fields["token_encrypted"] = self._cipher.encrypt(token) if token else None
        cols = list(fields.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO hosts ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        return self.get(host_id)  # type: ignore[return-value]

    def update(
        self, host_id: str, data: dict[str, Any], token: Optional[str] = None,
        set_token: bool = False,
    ) -> Optional[Host]:
        sets, params = [], []
        for k in _FIELDS:
            if k in data and data[k] is not None:
                sets.append(f"{k} = %s")
                params.append(Json(data[k]) if k == "labels" else data[k])
        if set_token:  # explicit token change (None clears it)
            sets.append("token_encrypted = %s")
            params.append(self._cipher.encrypt(token) if token else None)
        if not sets:
            return self.get(host_id)
        sets.append("updated_at = now()")
        params.append(host_id)
        with self._pool.connection() as conn:
            conn.execute(f"UPDATE hosts SET {', '.join(sets)} WHERE id = %s", tuple(params))
        return self.get(host_id)

    def delete(self, host_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM hosts WHERE id = %s", (host_id,))

    def set_status(self, host_id: str, status: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE hosts SET last_status = %s, last_seen_at = now(), updated_at = now() "
                "WHERE id = %s",
                (status, host_id),
            )

    def resolve(self, identifier: str, active_only: bool = True) -> Optional[ResolvedHost]:
        """Find a host by id / fqdn / instance_id and build its MCP endpoint + token."""
        sql = (
            f"SELECT {_COLS} FROM hosts "
            "WHERE (id = %s OR fqdn = %s OR instance_id = %s)"
        )
        params: tuple = (identifier, identifier, identifier)
        if active_only:
            sql += " AND active"
        sql += " LIMIT 1"
        with self._pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        host = _row_to_host(row)
        address = _address(host.fqdn, host.private_ip, host.public_ip, host.address_preference)
        url = f"{host.mcp_scheme}://{address}:{host.mcp_port}/mcp"
        token = self._cipher.decrypt(row[10]) if row[10] else None
        return ResolvedHost(host=host, url=url, token=token)
