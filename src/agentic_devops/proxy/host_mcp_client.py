"""On-demand MCP client for the host registry (Phase 9b).

Unlike ``MCPManager`` (which holds long-lived sessions to *configured* servers),
this dials a host's MCP endpoint **per call** — the endpoint + token come from the
registry at call time. Each call opens, initializes, calls, and closes a session
inside a single coroutine (one task), avoiding anyio's cancel-scope pitfalls, all
on one dedicated background event loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional

from agentic_devops.proxy.mcp_client import _render_result

_CALL_TIMEOUT = 45
_LIST_TIMEOUT = 20


def _auth_headers(token: Optional[str], auth_header: Optional[str] = None) -> Optional[dict[str, str]]:
    """Build the auth headers for an MCP endpoint. By default the token goes in
    ``Authorization: Bearer <token>``; a server that wants a non-standard header
    (e.g. the Grafana MCP's ``X-Grafana-Api-Key``) sets ``auth_header`` and the raw
    token is sent under that name instead."""
    if not token:
        return None
    if auth_header:
        return {auth_header: token}
    return {"Authorization": f"Bearer {token}"}


class HostMCPClient:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="host-mcp", daemon=True).start()
                self._loop = loop
            return self._loop

    def _run(self, coro: Any, timeout: float) -> Any:
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    # -- public (sync) API --------------------------------------------------
    # ``auth_header`` (optional) forwards the token in a non-standard header (e.g.
    # the Grafana MCP's X-Grafana-Api-Key); default is Authorization: Bearer.
    def call_tool(self, url: str, token: Optional[str], name: str, args: dict[str, Any],
                  auth_header: Optional[str] = None) -> str:
        try:
            return self._run(self._call(url, token, name, args or {}, auth_header), _CALL_TIMEOUT + 10)
        except Exception as exc:  # noqa: BLE001 — connection/timeout failures → readable error
            return f"ERROR: host check {name!r} could not run ({exc})"

    def list_tools(self, url: str, token: Optional[str], auth_header: Optional[str] = None) -> list[str]:
        try:
            return self._run(self._list(url, token, auth_header), _LIST_TIMEOUT + 5)
        except Exception:  # noqa: BLE001
            return []

    def list_tools_detail(self, url: str, token: Optional[str],
                          auth_header: Optional[str] = None) -> list[dict[str, Any]]:
        """Full tool schemas (name/description/input_schema/annotations) — used by
        the MCP Servers registry to normalize a server's tools into ToolSpecs."""
        try:
            return self._run(self._list_detail(url, token, auth_header), _LIST_TIMEOUT + 5)
        except Exception:  # noqa: BLE001
            return []

    # -- async internals ----------------------------------------------------
    # The session is opened AND closed inline within one coroutine (one task) —
    # a generator + early-return would close the anyio cancel scope in a
    # different task ("Attempted to exit cancel scope in a different task").
    async def _call(self, url: str, token: Optional[str], name: str, args: dict[str, Any],
                    auth_header: Optional[str] = None) -> str:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = _auth_headers(token, auth_header)
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await asyncio.wait_for(session.call_tool(name, args), _CALL_TIMEOUT)
                return _render_result(result)

    async def _list(self, url: str, token: Optional[str], auth_header: Optional[str] = None) -> list[str]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = _auth_headers(token, auth_header)
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await asyncio.wait_for(session.list_tools(), _LIST_TIMEOUT)
                return [t.name for t in tools.tools]

    async def _list_detail(self, url: str, token: Optional[str],
                           auth_header: Optional[str] = None) -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = _auth_headers(token, auth_header)
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await asyncio.wait_for(session.list_tools(), _LIST_TIMEOUT)
                out: list[dict[str, Any]] = []
                for t in tools.tools:
                    ann = getattr(t, "annotations", None)
                    out.append({
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                        "read_only_hint": getattr(ann, "readOnlyHint", None) if ann else None,
                        "destructive_hint": getattr(ann, "destructiveHint", None) if ann else None,
                    })
                return out
