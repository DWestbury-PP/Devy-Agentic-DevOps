"""Built-in tools shipped with the framework."""

from __future__ import annotations

from typing import Optional
from pathlib import Path

from agentic_devops.tools.builtin.diagnostics import build_diagnostics_tool
from agentic_devops.tools.builtin.timeline import build_correlate_timeline_tool
from agentic_devops.tools.router import ToolsRouter


def register_builtin_tools(router: ToolsRouter, audit_path: Optional[Path] = None) -> None:
    """Register the framework's built-in tools onto a router."""
    router.register(build_diagnostics_tool(audit_path=audit_path))
    router.register(build_correlate_timeline_tool())


__all__ = [
    "register_builtin_tools",
    "build_diagnostics_tool",
    "build_correlate_timeline_tool",
]
