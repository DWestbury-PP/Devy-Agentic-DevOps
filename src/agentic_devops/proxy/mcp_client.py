"""MCP source adapter: the proxy as an MCP client.

Mounts tools from any configured MCP server (stdio or streamable-HTTP) into the
tools-router, so `find_tools` surfaces them alongside native tools and the
harness can call them transparently. This is what lets tools live on the target
hosts (the Phase 2 host MCP) rather than in-process.

MCP's SDK is async; the proxy and tool handlers are sync. We run all MCP
sessions on one dedicated background event loop. Crucially, each server's
session is opened AND closed inside a single long-lived task (it waits on a stop
event), which avoids anyio's "cancel scope exited in a different task" error
that bites the naive enter-here / close-there approach.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from agentic_devops.config import MCPServerConfig
from agentic_devops.tools.base import ToolSpec

logger = logging.getLogger("agentic_devops.mcp")

_CONNECT_TIMEOUT = 30
_CALL_TIMEOUT = 60


def _sanitize(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)


def _is_write_tool(tool: Any) -> bool:
    """True only when a mounted tool EXPLICITLY declares itself non-read-only via
    the MCP ``readOnlyHint=False`` annotation. Absent or None hints are treated as
    read-only — most servers set no hints, and we must not withhold their tools —
    so this withholds only tools that self-identify as writes (e.g. the host MCP's
    guarded mutating verbs, reachable only via the proxy's approve-and-execute
    path, never as a directly-callable assistant tool)."""
    ann = getattr(tool, "annotations", None)
    return ann is not None and getattr(ann, "readOnlyHint", None) is False


def _render_result(result: Any) -> Any:
    """Render an MCP tool result. Text-only results return a ``str`` (the common
    case); a result carrying image content returns a ``ToolResult`` so the base64
    is handed off for rendering/vision instead of being stringified into the
    model's text context (unreadable + 10s of KB of token waste)."""
    from agentic_devops.tools.base import ToolImage, ToolResult

    parts: list[str] = []
    images: list[ToolImage] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
            continue
        # MCP ImageContent: type == "image", base64 in .data, mime in .mimeType
        data = getattr(block, "data", None)
        if getattr(block, "type", None) == "image" and data:
            images.append(ToolImage(data=data, mime=getattr(block, "mimeType", None) or "image/png"))
        else:
            parts.append(str(block))
    body = "\n".join(parts).strip()
    if getattr(result, "isError", False):
        return f"ERROR: {body}" if body else "ERROR: MCP tool call failed"
    if images:
        return ToolResult(text=body, images=images)
    return body


class _Server:
    def __init__(self, cfg: MCPServerConfig, session: Any, tools: list) -> None:
        self.cfg = cfg
        self.session = session
        self.tools = tools


class MCPManager:
    """Connects to configured MCP servers and exposes their tools as ToolSpecs."""

    def __init__(self, configs: list[MCPServerConfig]) -> None:
        self._configs = configs
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[asyncio.Event] = None
        self._servers: dict[str, _Server] = {}
        self._errors: list[str] = []
        self._excluded: list[str] = []  # mounted tools withheld as writes (readOnlyHint=false)
        self._ready = threading.Event()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not self._configs:
            self._ready.set()
            return
        self._thread = threading.Thread(target=self._run, name="mcp-manager", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=_CONNECT_TIMEOUT + 5)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._bootstrap())
        finally:
            self._ready.set()
        self._loop.run_forever()

    async def _bootstrap(self) -> None:
        self._stop = asyncio.Event()
        waiters = []
        for cfg in self._configs:
            ready = asyncio.Event()
            self._loop.create_task(self._serve(cfg, ready))  # type: ignore[union-attr]
            waiters.append(self._await_ready(cfg, ready))
        await asyncio.gather(*waiters)

    async def _await_ready(self, cfg: MCPServerConfig, ready: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(ready.wait(), timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            self._errors.append(f"{cfg.name}: connection timed out")

    async def _serve(self, cfg: MCPServerConfig, ready: asyncio.Event) -> None:
        """Open the session, register it, then hold the contexts open (in THIS
        task) until shutdown — so they're also closed in this task."""
        from mcp import StdioServerParameters

        try:
            if cfg.transport == "stdio":
                from mcp.client.stdio import stdio_client

                params = StdioServerParameters(
                    command=cfg.command or "", args=cfg.args, env=cfg.env or None
                )
                async with stdio_client(params) as (read, write):
                    await self._run_session(cfg, read, write, ready)
            else:
                from mcp.client.streamable_http import streamablehttp_client

                headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else None
                async with streamablehttp_client(cfg.url or "", headers=headers) as (read, write, _):
                    await self._run_session(cfg, read, write, ready)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the proxy
            self._errors.append(f"{cfg.name}: {exc}")
            logger.warning("MCP server %r failed to mount: %s", cfg.name, exc)
            ready.set()

    async def _run_session(self, cfg: MCPServerConfig, read: Any, write: Any, ready: asyncio.Event) -> None:
        from mcp import ClientSession

        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            self._servers[cfg.name] = _Server(cfg, session, tools)
            logger.info("Mounted MCP server %r (%d tools)", cfg.name, len(tools))
            ready.set()
            await self._stop.wait()  # type: ignore[union-attr]

    def shutdown(self) -> None:
        if self._loop is None or self._stop is None:
            return

        async def _drain() -> None:
            self._stop.set()  # type: ignore[union-attr]
            pending = [t for t in asyncio.all_tasks(self._loop) if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_drain(), self._loop).result(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # -- tool exposure ------------------------------------------------------
    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def excluded_write_tools(self) -> list[str]:
        """Mounted tools withheld from the assistant because they declared
        themselves writes (MCP ``readOnlyHint=False``). Populated by ``tool_specs``;
        surfaced for observability (e.g. guarded-actions reach these only via the
        proxy's approve-and-execute path, never as a directly-callable tool)."""
        return list(self._excluded)

    def tool_specs(self) -> list[ToolSpec]:
        self._excluded = []
        specs: list[ToolSpec] = []
        for server in self._servers.values():
            category = server.cfg.category or server.cfg.name
            for tool in server.tools:
                # Withhold any tool that EXPLICITLY declares itself non-read-only.
                # Absent/None hints are treated as read-only, so servers that set no
                # annotations (most) are unaffected — we only drop self-declared writes.
                if _is_write_tool(tool):
                    self._excluded.append(f"{server.cfg.name}:{tool.name}")
                    logger.info(
                        "Withholding non-read-only mounted tool %r from %r "
                        "(readOnlyHint=false); not exposed to the assistant.",
                        tool.name, server.cfg.name,
                    )
                    continue
                specs.append(self._make_spec(server, category, tool))
        return specs

    def _make_spec(self, server: _Server, category: str, tool: Any) -> ToolSpec:
        registered = _sanitize(f"{server.cfg.name}_{tool.name}")
        description = tool.description or f"The {tool.name} tool from the {server.cfg.name} MCP server."
        schema = tool.inputSchema or {"type": "object", "properties": {}}

        def handler(args: dict[str, Any], _session=server.session, _name=tool.name) -> str:
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(_session.call_tool(_name, args or {}), self._loop)
            try:
                result = future.result(timeout=_CALL_TIMEOUT)
            except TimeoutError:
                return f"ERROR: MCP tool {_name!r} timed out after {_CALL_TIMEOUT}s"
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: MCP tool {_name!r} failed: {exc}"
            return _render_result(result)

        return ToolSpec(
            name=registered,
            category=category,
            description=description,
            when_to_use=description,
            input_schema=schema,
            handler=handler,
            use_cases=[tool.name],
            safety_tier=server.cfg.safety_tier,
        )
