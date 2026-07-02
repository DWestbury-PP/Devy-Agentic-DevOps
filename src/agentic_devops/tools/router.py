"""The tools-router: registry + on-demand discovery.

Discovery shape (chosen in Phase 1): a single ``find_tools`` meta-tool with
auto-load. The model calls ``find_tools(intent=..., category=...)``; the router
returns matching tool summaries AND the harness injects those tools' full
schemas into the available set for subsequent iterations. One round-trip, no
separate "load" step.
"""

from __future__ import annotations

from typing import Any, Optional

from agentic_devops.tools.base import ToolSpec

FIND_TOOLS_NAME = "find_tools"


class ToolNotFoundError(KeyError):
    """Raised when the model tries to call a tool that isn't registered."""


class ToolsRouter:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # -- registration -------------------------------------------------------
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} is already registered")
        if spec.name == FIND_TOOLS_NAME:
            raise ValueError(f"{FIND_TOOLS_NAME!r} is reserved for discovery")
        self._tools[spec.name] = spec

    def register_or_replace(self, spec: ToolSpec) -> None:
        """Register, overwriting any existing tool with the same name (used when a
        dynamic source — e.g. an MCP server — is refreshed)."""
        if spec.name == FIND_TOOLS_NAME:
            raise ValueError(f"{FIND_TOOLS_NAME!r} is reserved for discovery")
        self._tools[spec.name] = spec

    def unregister(self, name: str) -> bool:
        """Withdraw a tool by name. Returns True if it existed."""
        return self._tools.pop(name, None) is not None

    def unregister_category(self, category: str) -> int:
        """Withdraw all tools in a category (a whole MCP server's tools on
        disable/delete/refresh). Returns how many were removed."""
        names = [n for n, s in self._tools.items() if s.category == category]
        for n in names:
            del self._tools[n]
        return len(names)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def categories(self) -> list[str]:
        return sorted({t.category for t in self._tools.values()})

    def all_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get_spec(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    # -- discovery ----------------------------------------------------------
    def find(self, intent: Optional[str] = None, category: Optional[str] = None) -> list[ToolSpec]:
        """Match registered tools by free-text intent and/or category.

        Phase 1 uses simple case-insensitive token matching over the metadata.
        Embedding-based discovery is a later upgrade. With neither argument, all
        tools are returned (useful for "what can you do?").
        """
        results: list[tuple[int, ToolSpec]] = []
        tokens = [t for t in (intent or "").lower().split() if t]

        for spec in self._tools.values():
            if category and spec.category.lower() != category.lower():
                continue
            if not tokens:
                results.append((0, spec))
                continue
            haystack = " ".join(
                [spec.name, spec.category, spec.description, spec.when_to_use, *spec.use_cases]
            ).lower()
            score = sum(1 for tok in tokens if tok in haystack)
            if score > 0 or category:
                results.append((score, spec))

        results.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [spec for _, spec in results]

    # -- the discovery meta-tool schema ------------------------------------
    def find_tools_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": FIND_TOOLS_NAME,
                "description": (
                    "Discover tools available for a task. Call this first to find "
                    "tools by intent before answering questions that need live data "
                    "or actions. The matching tools become callable immediately after."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "What you want to do, in plain language "
                            "(e.g. 'check host disk and memory health').",
                        },
                        "category": {
                            "type": "string",
                            "description": "Optional category filter. Known categories: "
                            + (", ".join(self.categories()) or "(none yet)"),
                        },
                    },
                },
            },
        }

    def schema_for(self, name: str) -> dict[str, Any]:
        if name not in self._tools:
            raise ToolNotFoundError(name)
        return self._tools[name].openai_schema()

    # -- execution ----------------------------------------------------------
    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Optional[dict[str, Any]] = None,
    ) -> Any:
        if name not in self._tools:
            raise ToolNotFoundError(name)
        spec = self._tools[name]
        if spec.wants_context:
            return spec.handler(arguments or {}, context or {})
        return spec.handler(arguments or {})
