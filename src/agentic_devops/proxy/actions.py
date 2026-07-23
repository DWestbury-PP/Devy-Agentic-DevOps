"""Guarded mutating actions (G-2b): propose → approve → execute.

Devy never mutates directly. ``request_action`` writes a PROPOSED row; a human
approves out-of-band via the API; only then does ``ActionExecutor`` run the
reversible verb on the host MCP — a dedicated ``HostMCPClient`` call, separate
from Devy's tool surface (the mutating verbs are withheld from the assistant by
the G-2a ``readOnlyHint`` filter). The verbs are the curated Tier-A set mirrored
from the host allow-list; the host allow-list stays the authority on what runs.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class ActionVerb:
    verb: str
    label: str
    required: tuple[str, ...]          # required arg names (besides the target host)
    reversibility: str                 # note surfaced on the approval card
    target_hint: Optional[str] = None  # which arg names the target (for display)


# The curated, REVERSIBLE Tier-A verbs Devy may PROPOSE — mirrors the host MCP's
# mutating allow-list. No stop / rm / volume ops here (nor there): those are
# excluded by design. The host allow-list enforces the contract; a proposal for a
# verb it doesn't expose simply fails at execution, never silently.
ACTION_CATALOG: dict[str, ActionVerb] = {
    "restart_service": ActionVerb(
        "restart_service", "Restart service", ("name",),
        "Brief restart; the service comes back up.", "name"),
    "restart_container": ActionVerb(
        "restart_container", "Restart container", ("container",),
        "Container restarts; its state returns.", "container"),
    "reload_config": ActionVerb(
        "reload_config", "Reload config", ("name",),
        "Live config reload; no downtime.", "name"),
    "prune_images": ActionVerb(
        "prune_images", "Prune unused images", (),
        "Removes unused images (rebuildable / re-pullable); never touches volumes.", None),
}


def guarded_actions_status(*, enabled: bool, allow_insecure_dev: bool, auth_mode: str) -> tuple[bool, str]:
    """Fail-closed enable decision. A human-approval guardrail is only meaningful
    behind real identity, so guarded actions require ``auth.mode='jwt'`` unless an
    explicit insecure-dev override is set (local testing). Returns (enabled, reason)."""
    if not enabled:
        return False, "not enabled"
    if auth_mode == "jwt":
        return True, "jwt"
    if allow_insecure_dev:
        return True, "insecure-dev-override"
    return False, (
        "refused: auth.mode is not 'jwt' — the approve gate needs real identity. "
        "Set auth.mode=jwt, or actions.allow_insecure_dev=true for local testing."
    )


_COLS = (
    "id, session_id, user_id, host, verb, args, rationale, reversibility, status, "
    "decided_by, result, returncode, created_at, decided_at, executed_at, expires_at"
)


@dataclass
class StoredAction:
    id: str
    session_id: Optional[str]
    user_id: Optional[str]
    host: Optional[str]
    verb: str
    args: dict
    rationale: str
    reversibility: str
    status: str
    decided_by: Optional[str]
    result: Optional[str]
    returncode: Optional[int]
    created_at: str
    decided_at: Optional[str]
    executed_at: Optional[str]
    expires_at: str

    @property
    def label(self) -> str:
        v = ACTION_CATALOG.get(self.verb)
        return v.label if v else self.verb

    @property
    def target(self) -> Optional[str]:
        v = ACTION_CATALOG.get(self.verb)
        return self.args.get(v.target_hint) if v and v.target_hint else None


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _row(r: tuple) -> StoredAction:
    return StoredAction(
        id=r[0], session_id=r[1], user_id=r[2], host=r[3], verb=r[4],
        args=r[5] if isinstance(r[5], dict) else {},
        rationale=r[6] or "", reversibility=r[7] or "", status=r[8],
        decided_by=r[9], result=r[10], returncode=r[11],
        created_at=_iso(r[12]) or "", decided_at=_iso(r[13]),
        executed_at=_iso(r[14]), expires_at=_iso(r[15]) or "",
    )


class ActionStore:
    """Postgres-backed store for guarded-action proposals + their lifecycle."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def create(
        self, *, verb: str, args: dict, rationale: str, reversibility: str,
        host: Optional[str], session_id: Optional[str], user_id: Optional[str],
        ttl_seconds: int,
    ) -> StoredAction:
        action_id = uuid.uuid4().hex[:12]
        expires = datetime.now(timezone.utc) + timedelta(seconds=max(30, ttl_seconds))
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO pending_actions "
                "(id, session_id, user_id, host, verb, args, rationale, reversibility, "
                " status, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, 'proposed', %s) "
                f"RETURNING {_COLS}",
                (action_id, session_id, user_id, host, verb, json.dumps(args or {}),
                 rationale, reversibility, expires),
            ).fetchone()
        return _row(row)

    def get(self, action_id: str) -> Optional[StoredAction]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM pending_actions WHERE id = %s", (action_id,)
            ).fetchone()
        return _row(row) if row else None

    def list(
        self, *, session_id: Optional[str] = None, status: Optional[str] = None, limit: int = 50
    ) -> list[StoredAction]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = %s")
            params.append(session_id)
        if status:
            clauses.append("status = %s")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM pending_actions{where} ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            ).fetchall()
        return [_row(r) for r in rows]

    def deny(self, action_id: str, decided_by: Optional[str]) -> bool:
        """Reject a still-pending proposal. Compare-and-set on status so a
        decided/executed one is a no-op (returns False)."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE pending_actions SET status='denied', decided_by=%s, decided_at=now() "
                "WHERE id=%s AND status='proposed'",
                (decided_by, action_id),
            )
            return cur.rowcount > 0

    def claim_for_execution(self, action_id: str, decided_by: Optional[str]) -> Optional[StoredAction]:
        """Atomically move a proposal proposed→executing IFF it's still pending AND
        unexpired — the double-approve + TTL guard in one compare-and-set. Returns
        the claimed row (to execute) or None (already decided / expired / unknown).
        Only the caller that wins this CAS proceeds to execute."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE pending_actions SET status='executing', decided_by=%s, decided_at=now() "
                "WHERE id=%s AND status='proposed' AND expires_at > now() "
                f"RETURNING {_COLS}",
                (decided_by, action_id),
            ).fetchone()
        return _row(row) if row else None

    def record_result(
        self, action_id: str, *, status: str, result: str, returncode: Optional[int]
    ) -> Optional[StoredAction]:
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE pending_actions SET status=%s, result=%s, returncode=%s, executed_at=now() "
                f"WHERE id=%s RETURNING {_COLS}",
                (status, result, returncode, action_id),
            ).fetchone()
        return _row(row) if row else None


# resolver: host identifier (or None) -> (url, token, auth_header) | None
TargetResolver = Callable[[Optional[str]], Optional[tuple[str, Optional[str], Optional[str]]]]


class ActionExecutor:
    """Runs an approved action on the host MCP via a dedicated caller — a path the
    assistant never touches (the mutating verbs are withheld from its tool set).
    The host sidecar's own allow-list + profile + ``HOST_MCP_ALLOW_MUTATIONS`` gate
    remain the final authority on whether the verb actually executes."""

    def __init__(self, caller: Any, resolve_target: TargetResolver) -> None:
        self._caller = caller
        self._resolve = resolve_target

    def execute(self, action: StoredAction) -> tuple[str, Optional[int]]:
        target = self._resolve(action.host)
        if target is None:
            return (
                f"ERROR: could not resolve a host MCP target for "
                f"{action.host or '(default)'!r}. Is a host sidecar mounted with "
                "mutations enabled?"
            ), None
        url, token, auth_header = target
        result = self._caller.call_tool(url, token, action.verb, action.args, auth_header=auth_header)
        # host-mcp returns "$ <argv>\n\n<output>" on success, "ERROR: ..." on a
        # validation/connection failure — treat a leading ERROR as a non-zero result.
        rc = 1 if result.startswith("ERROR") else 0
        return result, rc
