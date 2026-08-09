"""`agentic-devops-host-mcp` — run the host MCP server."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional, Sequence

from host_mcp.config import load
from host_mcp.server import build_server, run_http, run_stdio


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agentic-devops-host-mcp",
        description=(
            "Run the Agentic DevOps host MCP server — safe, allow-listed host "
            "diagnostics (no shell, no arbitrary execution)."
        ),
    )
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help=(
            "Enable the gated, reversible MUTATING checks (restart_service, "
            "reload_config, restart_container, prune_images). OFF by default — "
            "immutability is the default posture. This is the restart-gated switch "
            "for 'enhanced mode': it is read only at startup and self-reverts on "
            "the next normal restart. Over http the server still refuses to start "
            "without a bearer token (fail-closed). The OS must also grant the "
            "service user the privilege for the underlying verb (e.g. a scoped "
            "polkit/sudoers rule for systemctl) — the flag alone is not enough."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=["read-only", "diagnostic", "elevated"],
        default=None,
        help=(
            "Active read profile (overrides HOST_MCP_PROFILE and the allow-list "
            "file). Higher profiles expose more READ checks; 'enhanced mode' for "
            "reads is likewise a restart-gated choice."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = _parse_args(argv)
    # A supplied flag is an explicit override; when absent we pass None so the
    # systemd EnvironmentFile / container env (HOST_MCP_*) still drives config
    # unchanged. Both paths are read once, here, at startup.
    cfg = load(
        allow_mutations=True if args.allow_mutations else None,
        profile=args.profile,
    )
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
