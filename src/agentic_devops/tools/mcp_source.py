"""Normalize a registered MCP server's tools into ToolSpecs (Phase S-4).

The discovery consistency layer: an MCP server's ``list_tools`` gives thin schemas
(name + description + inputSchema). We map each to a ToolSpec so ``find_tools`` can
surface it JIT — building a discovery-friendly ``when_to_use`` from the description
+ parameter names (index-first; LLM enrichment is a later opt-in), and detecting
mutating tools so read-only stays the default (writes require the server's opt-in).
Execution dials the server on demand (url + token resolved at call time).
"""

from __future__ import annotations

import re
from typing import Any

from agentic_devops.tools.base import ToolSpec

# Fallback write-detection when a tool carries no MCP annotations. Conservative
# toward "write" (a mis-flag just means it needs allow_writes to be callable).
_WRITE_RE = re.compile(
    r"(create|delete|update|remove|write|\bset\b|put|post|patch|insert|drop|modify|deploy|restart|kill|terminate|revoke)",
    re.I,
)
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _is_write(t: dict[str, Any]) -> bool:
    if t.get("read_only_hint") is True:
        return False
    if t.get("destructive_hint") is True:
        return True
    if t.get("read_only_hint") is False:
        return True
    return bool(_WRITE_RE.search(t.get("name", "")))


def _spec_name(server_name: str, tool_name: str) -> str:
    """A globally-unique, function-name-safe id (^[a-zA-Z0-9_-]{1,64}$)."""
    raw = f"{server_name}_{tool_name}"
    return _NAME_RE.sub("_", raw)[:64]


def _when_to_use(server_name: str, t: dict[str, Any]) -> str:
    """Index-first discovery text: description + parameter names/descriptions,
    prefixed with the server so intent-by-domain queries surface it."""
    parts = [t.get("description") or ""]
    props = (t.get("input_schema") or {}).get("properties", {}) or {}
    if props:
        parts.append("Parameters: " + ", ".join(props.keys()) + ".")
        for k, v in props.items():
            d = (v or {}).get("description")
            if d:
                parts.append(f"{k}: {d}")
    return f"[{server_name}] " + " ".join(p for p in parts if p).strip()


def build_server_tools(server: Any, tools_detail: list[dict[str, Any]], *, store: Any, caller: Any):
    """Return (specs, write_tool_count). Write tools are counted always but only
    registered when ``server.allow_writes`` is set."""
    specs: list[ToolSpec] = []
    write_count = 0
    for t in tools_detail:
        is_write = _is_write(t)
        if is_write:
            write_count += 1
            if not server.allow_writes:
                continue
        specs.append(_make_spec(server, t, is_write, store, caller))
    return specs, write_count


def _make_spec(server: Any, t: dict[str, Any], is_write: bool, store: Any, caller: Any) -> ToolSpec:
    tool_name = t["name"]
    server_id = server.id
    server_name = server.name

    def handler(args: dict[str, Any]) -> str:
        resolved = store.resolve(server_id)
        if resolved is None:
            return f"ERROR: MCP server {server_name!r} is no longer registered"
        return caller.call_tool(
            resolved.url, resolved.token, tool_name, args or {},
            auth_header=getattr(resolved, "auth_header", None),
        )

    return ToolSpec(
        name=_spec_name(server_name, tool_name),
        category=server.tool_category,
        description=t.get("description") or f"The {tool_name} tool from the {server_name} MCP server.",
        when_to_use=_when_to_use(server_name, t),
        input_schema=t.get("input_schema") or {"type": "object", "properties": {}},
        handler=handler,
        safety_tier="elevated" if is_write else "read-only",
    )
