"""Built-in tools shipped with the framework."""

from __future__ import annotations

from typing import Optional
from pathlib import Path

from agentic_devops.tools.builtin.diagnostics import build_diagnostics_tool
from agentic_devops.tools.builtin.timeline import build_correlate_timeline_tool
from agentic_devops.tools.router import ToolsRouter


def register_builtin_tools(
    router: ToolsRouter,
    audit_path: Optional[Path] = None,
    *,
    container_scoped: bool = False,
) -> None:
    """Register the framework's built-in tools onto a router.

    ``container_scoped`` re-scopes the diagnostics builtin to the proxy's own
    container when a real host MCP is mounted (see ``build_diagnostics_tool``).
    """
    router.register(
        build_diagnostics_tool(audit_path=audit_path, container_scoped=container_scoped)
    )
    router.register(build_correlate_timeline_tool())


__all__ = [
    "register_builtin_tools",
    "build_diagnostics_tool",
    "build_correlate_timeline_tool",
]
