"""GitHub account registry (Phase D-1).

Credential-centric: one registered read-only PAT can see all of an account's
repos (repos are discovered live via the API, not pre-registered). Mirrors the
host registry — Fernet-encrypted token at rest, never returned by the API; the
agent targets repos by name and the proxy resolves the token here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from agentic_devops.proxy.encryption import TokenCipher

_COLS = (
    "id, label, login, token_encrypted, default_corpus, active, labels, "
    "last_used_at, last_status, created_at, updated_at"
)
# Mutable fields accepted on create/update (token handled separately).
_FIELDS = ("label", "login", "default_corpus", "active", "labels")


@dataclass
class GitHubAccount:
    """A registered GitHub credential (public view — never carries the token)."""

    id: str
    label: str
    login: Optional[str] = None
    default_corpus: Optional[str] = None
    active: bool = True
    labels: dict[str, Any] = field(default_factory=dict)
    last_used_at: Optional[str] = None
    last_status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    has_token: bool = False


@dataclass
class ResolvedAccount:
    account: GitHubAccount
    token: Optional[str]


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _row_to_account(r: tuple) -> GitHubAccount:
    return GitHubAccount(
        id=r[0], label=r[1], login=r[2], has_token=r[3] is not None,
        default_corpus=r[4], active=r[5], labels=dict(r[6] or {}),
        last_used_at=_iso(r[7]), last_status=r[8],
        created_at=_iso(r[9]), updated_at=_iso(r[10]),
    )


class GitHubAccountStore:
    def __init__(self, pool: ConnectionPool, cipher: TokenCipher) -> None:
        self._pool = pool
        self._cipher = cipher

    def list(self, active_only: bool = False) -> list[GitHubAccount]:
        sql = f"SELECT {_COLS} FROM github_accounts"
        if active_only:
            sql += " WHERE active"
        sql += " ORDER BY label"
        with self._pool.connection() as conn:
            return [_row_to_account(r) for r in conn.execute(sql).fetchall()]

    def get(self, account_id: str) -> Optional[GitHubAccount]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM github_accounts WHERE id = %s", (account_id,)
            ).fetchone()
        return _row_to_account(row) if row else None

    def create(self, data: dict[str, Any], token: Optional[str] = None) -> GitHubAccount:
        account_id = uuid.uuid4().hex[:12]
        fields = {k: data[k] for k in _FIELDS if k in data and data[k] is not None}
        fields["id"] = account_id
        if "labels" in fields:
            fields["labels"] = Json(fields["labels"])
        fields["token_encrypted"] = self._cipher.encrypt(token) if token else None
        cols = list(fields.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO github_accounts ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        return self.get(account_id)  # type: ignore[return-value]

    def update(
        self, account_id: str, data: dict[str, Any], token: Optional[str] = None,
        set_token: bool = False,
    ) -> Optional[GitHubAccount]:
        sets, params = [], []
        for k in _FIELDS:
            if k in data and data[k] is not None:
                sets.append(f"{k} = %s")
                params.append(Json(data[k]) if k == "labels" else data[k])
        if set_token:
            sets.append("token_encrypted = %s")
            params.append(self._cipher.encrypt(token) if token else None)
        if not sets:
            return self.get(account_id)
        sets.append("updated_at = now()")
        params.append(account_id)
        with self._pool.connection() as conn:
            conn.execute(
                f"UPDATE github_accounts SET {', '.join(sets)} WHERE id = %s", tuple(params)
            )
        return self.get(account_id)

    def delete(self, account_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM github_accounts WHERE id = %s", (account_id,))

    def touch(self, account_id: str, status: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE github_accounts SET last_status = %s, last_used_at = now(), "
                "updated_at = now() WHERE id = %s",
                (status, account_id),
            )

    def resolve(self, identifier: Optional[str] = None, active_only: bool = True) -> Optional[ResolvedAccount]:
        """Resolve an account to its decrypted token.

        With ``identifier`` (id / label / login) returns that account; without one,
        returns the single active account (the common single-PAT case). Returns
        ``None`` if nothing matches or the choice is ambiguous."""
        sql = f"SELECT {_COLS} FROM github_accounts"
        clauses, params = [], []
        if identifier:
            clauses.append("(id = %s OR label = %s OR login = %s)")
            params.extend([identifier, identifier, identifier])
        if active_only:
            clauses.append("active")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY label"
        with self._pool.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        if not rows:
            return None
        if identifier is None and len(rows) > 1:
            return None  # ambiguous: caller must name the account
        row = rows[0]
        account = _row_to_account(row)
        token = self._cipher.decrypt(row[3]) if row[3] else None
        return ResolvedAccount(account=account, token=token)

    def resolve_for_repo(self, full_name: str) -> Optional[ResolvedAccount]:
        """Pick the account most likely to access ``owner/repo``: one whose login
        matches the owner, else the single active account."""
        owner = full_name.split("/", 1)[0] if "/" in full_name else full_name
        by_login = self.resolve(owner)
        if by_login is not None:
            return by_login
        return self.resolve()  # single active account, or None if ambiguous/absent
