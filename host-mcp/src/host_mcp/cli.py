"""`agentic-devops-host-mcp` — run the host MCP server."""

from __future__ import annotations

import asyncio
import sys

from host_mcp.config import load
from host_mcp.server import build_server, run_http, run_stdio


def main() -> None:
    cfg = load()
    server = build_server(cfg.allowlist)
    checks = [c.name for c in cfg.allowlist.available_checks()]
    mutations = "ENABLED" if cfg.allowlist.allow_mutations else "disabled"
    print(
        f"host MCP — profile={cfg.allowlist.active_profile} mutations={mutations} "
        f"transport={cfg.transport} checks={checks}",
        file=sys.stderr,
    )
    if cfg.transport == "http":
        asyncio.run(run_http(server, cfg))
    else:
        asyncio.run(run_stdio(server))


if __name__ == "__main__":
    main()
