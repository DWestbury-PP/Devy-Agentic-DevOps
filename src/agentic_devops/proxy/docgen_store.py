"""Doc-generation persistence (Phase D-2-1): the per-repo checkpoint and the
component registry.

``RepoDocgenStore`` holds ``last_doc_sha`` — the commit the docs were last generated
from — so the diff-driven engine can skip an unchanged repo. ``DocComponentStore``
is one row per discovered component, tracking its generated doc paths and review
status. Both mirror the ``RepoCrawlStore`` pattern (autocommit pool, upsert-on-PK).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from psycopg_pool import ConnectionPool

from agentic_devops.knowledge.docgen import Component

_DOCGEN_COLS = (
    "full_name, last_doc_sha, default_branch, scan_brief, components_doced, "
    "status, last_run_at, error"
)
_COMPONENT_COLS = (
    "id, full_name, component_path, component_name, kind, arch_doc_path, "
    "releases_doc_path, last_doc_sha, status"
)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


@dataclass
class RepoDocgen:
    full_name: str
    last_doc_sha: Optional[str] = None
    default_branch: Optional[str] = None
    scan_brief: str = ""
    components_doced: int = 0
    status: str = "idle"
    last_run_at: Optional[str] = None
    error: str = ""


@dataclass
class DocComponent:
    id: str
    full_name: str
    component_path: str
    component_name: str
    kind: str = "manifest"
    arch_doc_path: Optional[str] = None
    releases_doc_path: Optional[str] = None
    last_doc_sha: Optional[str] = None
    status: str = "draft"


def _row_to_docgen(r: tuple) -> RepoDocgen:
    return RepoDocgen(
        full_name=r[0], last_doc_sha=r[1], default_branch=r[2], scan_brief=r[3] or "",
        components_doced=r[4], status=r[5], last_run_at=_iso(r[6]), error=r[7] or "",
    )


def _row_to_component(r: tuple) -> DocComponent:
    return DocComponent(
        id=r[0], full_name=r[1], component_path=r[2], component_name=r[3], kind=r[4],
        arch_doc_path=r[5], releases_doc_path=r[6], last_doc_sha=r[7], status=r[8],
    )


class RepoDocgenStore:
    """The per-repo docgen checkpoint (``last_doc_sha``) + scan brief + run status."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def list(self) -> list[RepoDocgen]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_DOCGEN_COLS} FROM repo_docgen ORDER BY full_name"
            ).fetchall()
        return [_row_to_docgen(r) for r in rows]

    def get(self, full_name: str) -> Optional[RepoDocgen]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_DOCGEN_COLS} FROM repo_docgen WHERE full_name = %s", (full_name,)
            ).fetchone()
        return _row_to_docgen(row) if row else None

    def _ensure(self, conn: Any, full_name: str) -> None:
        conn.execute(
            "INSERT INTO repo_docgen (full_name) VALUES (%s) ON CONFLICT (full_name) DO NOTHING",
            (full_name,),
        )

    def set_brief(self, full_name: str, brief: str) -> RepoDocgen:
        with self._pool.connection() as conn:
            self._ensure(conn, full_name)
            conn.execute(
                "UPDATE repo_docgen SET scan_brief = %s, updated_at = now() WHERE full_name = %s",
                (brief, full_name),
            )
        return self.get(full_name)  # type: ignore[return-value]

    def set_status(self, full_name: str, status: str, error: str = "") -> None:
        with self._pool.connection() as conn:
            self._ensure(conn, full_name)
            conn.execute(
                "UPDATE repo_docgen SET status = %s, error = %s, updated_at = now() "
                "WHERE full_name = %s",
                (status, error, full_name),
            )

    def checkpoint(
        self, full_name: str, last_doc_sha: str, *, default_branch: Optional[str] = None,
        components_doced: int = 0,
    ) -> RepoDocgen:
        """Record a completed run: advance the checkpoint SHA and mark idle."""
        with self._pool.connection() as conn:
            self._ensure(conn, full_name)
            conn.execute(
                "UPDATE repo_docgen SET last_doc_sha = %s, default_branch = %s, "
                "components_doced = %s, status = 'idle', error = '', last_run_at = now(), "
                "updated_at = now() WHERE full_name = %s",
                (last_doc_sha, default_branch, components_doced, full_name),
            )
        return self.get(full_name)  # type: ignore[return-value]


class DocComponentStore:
    """The component registry — one row per (repo, component_path)."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def list(self, full_name: str) -> list[DocComponent]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COMPONENT_COLS} FROM doc_components WHERE full_name = %s "
                "ORDER BY component_path",
                (full_name,),
            ).fetchall()
        return [_row_to_component(r) for r in rows]

    def get(self, full_name: str, component_path: str) -> Optional[DocComponent]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COMPONENT_COLS} FROM doc_components "
                "WHERE full_name = %s AND component_path = %s",
                (full_name, component_path),
            ).fetchone()
        return _row_to_component(row) if row else None

    def upsert(
        self, full_name: str, component: Component, *,
        arch_doc_path: Optional[str] = None, releases_doc_path: Optional[str] = None,
        last_doc_sha: Optional[str] = None, status: Optional[str] = None,
    ) -> DocComponent:
        """Register/refresh a component. Doc paths / sha / status update only when
        provided (a discovery pass records identity; a generation pass fills the rest)."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO doc_components "
                "(id, full_name, component_path, component_name, kind, arch_doc_path, "
                " releases_doc_path, last_doc_sha, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, 'draft')) "
                "ON CONFLICT (full_name, component_path) DO UPDATE SET "
                "component_name = EXCLUDED.component_name, kind = EXCLUDED.kind, "
                "arch_doc_path = COALESCE(EXCLUDED.arch_doc_path, doc_components.arch_doc_path), "
                "releases_doc_path = COALESCE(EXCLUDED.releases_doc_path, doc_components.releases_doc_path), "
                "last_doc_sha = COALESCE(EXCLUDED.last_doc_sha, doc_components.last_doc_sha), "
                "status = COALESCE(%s, doc_components.status), updated_at = now()",
                (uuid.uuid4().hex[:12], full_name, component.path, component.label,
                 component.kind, arch_doc_path, releases_doc_path, last_doc_sha, status, status),
            )
        return self.get(full_name, component.path)  # type: ignore[return-value]

    def set_status(self, full_name: str, component_path: str, status: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE doc_components SET status = %s, updated_at = now() "
                "WHERE full_name = %s AND component_path = %s",
                (status, full_name, component_path),
            )
