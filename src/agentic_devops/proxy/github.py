"""GitHub account registry (Phase D-1; secrets-backed since S-1).

Credential-centric: one registered read-only PAT can see all of an account's
repos (repos are discovered live via the API, not pre-registered). Mirrors the
host registry — the token never lives in this DB. The row holds metadata + a
``secret_ref`` (a name in the secrets manager); the value is resolved on demand
via the :class:`SecretsProvider` and never returned by the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from agentic_devops.proxy.secrets import SecretsProvider, github_secret_ref

_COLS = (
    "id, label, login, secret_ref, default_corpus, active, labels, "
    "last_used_at, last_status, created_at, updated_at"
)
# Mutable fields accepted on create/update (token handled separately).
_FIELDS = ("label", "login", "default_corpus", "active", "labels")


@dataclass
class GitHubAccount:
    """A registered GitHub credential (public view — never carries the token).

    ``secret_ref`` is the *name* of the PAT in the secrets manager (not the value);
    exposing it lets a prod operator know which secret to provision out-of-band."""

    id: str
    label: str
    login: Optional[str] = None
    secret_ref: Optional[str] = None
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
        id=r[0], label=r[1], login=r[2], secret_ref=r[3],
        default_corpus=r[4], active=r[5], labels=dict(r[6] or {}),
        last_used_at=_iso(r[7]), last_status=r[8],
        created_at=_iso(r[9]), updated_at=_iso(r[10]),
    )


class GitHubAccountStore:
    def __init__(self, pool: ConnectionPool, secrets: SecretsProvider) -> None:
        self._pool = pool
        self._secrets = secrets

    def _with_token_flag(self, account: GitHubAccount) -> GitHubAccount:
        account.has_token = bool(account.secret_ref) and self._secrets.exists(account.secret_ref)
        return account

    def list(self, active_only: bool = False) -> list[GitHubAccount]:
        sql = f"SELECT {_COLS} FROM github_accounts"
        if active_only:
            sql += " WHERE active"
        sql += " ORDER BY label"
        with self._pool.connection() as conn:
            return [self._with_token_flag(_row_to_account(r)) for r in conn.execute(sql).fetchall()]

    def get(self, account_id: str) -> Optional[GitHubAccount]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM github_accounts WHERE id = %s", (account_id,)
            ).fetchone()
        return self._with_token_flag(_row_to_account(row)) if row else None

    def create(self, data: dict[str, Any], token: Optional[str] = None) -> GitHubAccount:
        account_id = uuid.uuid4().hex[:12]
        fields = {k: data[k] for k in _FIELDS if k in data and data[k] is not None}
        fields["id"] = account_id
        # Fixed at create (label can change later without orphaning the secret); an
        # explicit secret_ref overrides the derived one (prod provisioning).
        secret_ref = data.get("secret_ref") or github_secret_ref(fields.get("label", account_id))
        fields["secret_ref"] = secret_ref
        if "labels" in fields:
            fields["labels"] = Json(fields["labels"])
        cols = list(fields.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO github_accounts ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        if token:
            self._secrets.set(secret_ref, token)
        return self.get(account_id)  # type: ignore[return-value]

    def update(
        self, account_id: str, data: dict[str, Any], token: Optional[str] = None,
        set_token: bool = False,
    ) -> Optional[GitHubAccount]:
        current = self.get(account_id)
        if current is None:
            return None
        sets, params = [], []
        for k in _FIELDS:
            if k in data and data[k] is not None:
                sets.append(f"{k} = %s")
                params.append(Json(data[k]) if k == "labels" else data[k])
        if sets:
            sets.append("updated_at = now()")
            params.append(account_id)
            with self._pool.connection() as conn:
                conn.execute(
                    f"UPDATE github_accounts SET {', '.join(sets)} WHERE id = %s", tuple(params)
                )
        if set_token and current.secret_ref:
            if token:
                self._secrets.set(current.secret_ref, token)
            else:
                self._secrets.delete(current.secret_ref)
        return self.get(account_id)

    def delete(self, account_id: str) -> None:
        current = self.get(account_id)
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM github_accounts WHERE id = %s", (account_id,))
        if current and current.secret_ref and self._secrets.writable:
            try:
                self._secrets.delete(current.secret_ref)
            except Exception:  # noqa: BLE001 — best-effort secret cleanup
                pass

    def touch(self, account_id: str, status: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE github_accounts SET last_status = %s, last_used_at = now(), "
                "updated_at = now() WHERE id = %s",
                (status, account_id),
            )

    def resolve(self, identifier: Optional[str] = None, active_only: bool = True) -> Optional[ResolvedAccount]:
        """Resolve an account to its token (fetched from the secrets manager).

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
        account = _row_to_account(rows[0])
        token = self._secrets.get(account.secret_ref) if account.secret_ref else None
        account.has_token = token is not None
        return ResolvedAccount(account=account, token=token)

    def resolve_for_repo(self, full_name: str) -> Optional[ResolvedAccount]:
        """Pick the account most likely to access ``owner/repo``: one whose login
        matches the owner, else the single active account."""
        owner = full_name.split("/", 1)[0] if "/" in full_name else full_name
        by_login = self.resolve(owner)
        if by_login is not None:
            return by_login
        return self.resolve()  # single active account, or None if ambiguous/absent


_CRAWL_COLS = (
    "full_name, corpus, account_id, commit_sha, default_branch, files_ingested, "
    "chunks_written, files_quarantined, secrets_redacted, crawled_at"
)


@dataclass
class RepoCrawl:
    """A record of the last crawl of a repo into the knowledge base."""

    full_name: str
    corpus: str
    account_id: Optional[str] = None
    commit_sha: Optional[str] = None
    default_branch: Optional[str] = None
    files_ingested: int = 0
    chunks_written: int = 0
    files_quarantined: int = 0
    secrets_redacted: int = 0
    crawled_at: Optional[str] = None


def _row_to_crawl(r: tuple) -> RepoCrawl:
    return RepoCrawl(
        full_name=r[0], corpus=r[1], account_id=r[2], commit_sha=r[3],
        default_branch=r[4], files_ingested=r[5], chunks_written=r[6],
        files_quarantined=r[7], secrets_redacted=r[8], crawled_at=_iso(r[9]),
    )


class RepoCrawlStore:
    """Tracks the last crawl per repo (commit, when, counts) so the admin UI can
    show what has been scanned. One upserted row per ``owner/name``."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def list(self) -> list[RepoCrawl]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_CRAWL_COLS} FROM repo_crawls ORDER BY crawled_at DESC"
            ).fetchall()
        return [_row_to_crawl(r) for r in rows]

    def get(self, full_name: str) -> Optional[RepoCrawl]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_CRAWL_COLS} FROM repo_crawls WHERE full_name = %s", (full_name,)
            ).fetchone()
        return _row_to_crawl(row) if row else None

    def record(
        self, full_name: str, corpus: str, *, account_id: Optional[str] = None,
        commit_sha: Optional[str] = None, default_branch: Optional[str] = None,
        files_ingested: int = 0, chunks_written: int = 0,
        files_quarantined: int = 0, secrets_redacted: int = 0,
    ) -> RepoCrawl:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO repo_crawls "
                "(full_name, corpus, account_id, commit_sha, default_branch, "
                " files_ingested, chunks_written, files_quarantined, secrets_redacted, crawled_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (full_name) DO UPDATE SET "
                "corpus=EXCLUDED.corpus, account_id=EXCLUDED.account_id, "
                "commit_sha=EXCLUDED.commit_sha, default_branch=EXCLUDED.default_branch, "
                "files_ingested=EXCLUDED.files_ingested, chunks_written=EXCLUDED.chunks_written, "
                "files_quarantined=EXCLUDED.files_quarantined, "
                "secrets_redacted=EXCLUDED.secrets_redacted, crawled_at=now()",
                (full_name, corpus, account_id, commit_sha, default_branch,
                 files_ingested, chunks_written, files_quarantined, secrets_redacted),
            )
        return self.get(full_name)  # type: ignore[return-value]

    def delete(self, full_name: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM repo_crawls WHERE full_name = %s", (full_name,))
