"""The host MCP server.

Tools are generated from the allow-list (one MCP tool per available check), so
the exposed surface is exactly what the active profile permits. Two transports:
stdio (local / proxy-spawned) and authenticated streamable-HTTP (remote).
"""

from __future__ import annotations

import contextlib
from typing import Any

import mcp.types as types
from mcp.server import Server

from host_mcp.allowlist import Allowlist
from host_mcp.config import ServerConfig


def build_server(allowlist: Allowlist) -> Server:
    server: Server = Server("agentic-devops-host-mcp")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=c.name, description=c.description, inputSchema=c.json_schema())
            for c in allowlist.available_checks()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        # allowlist.run never raises: validation failures come back as ERROR text.
        return [types.TextContent(type="text", text=allowlist.run(name, arguments or {}))]

    return server


async def run_stdio(server: Server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


async def run_http(server: Server, cfg: ServerConfig) -> None:
    """Serve over streamable-HTTP at /mcp, requiring a bearer token if configured."""
    try:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Mount

        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "HTTP transport needs extra deps: pip install 'agentic-devops-host-mcp[http]'"
        ) from exc

    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)

    async def handle(scope, receive, send) -> None:
        if cfg.token:
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {cfg.token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
    await uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="info")).serve()
