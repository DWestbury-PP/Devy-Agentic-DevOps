"""Tool specification: the metadata that powers on-demand discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Handlers are synchronous in Phase 1: they take validated arguments and return
# a string (or anything str()-able) that becomes the tool result.
ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass
class ToolSpec:
    """A tool the agent can discover and call.

    The metadata fields (``category``, ``use_cases``, ``when_to_use``) are what
    the tools-router matches against during discovery — so the model can find a
    tool by intent without every schema living in its system prompt.
    """

    name: str
    category: str
    description: str
    when_to_use: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    use_cases: list[str] = field(default_factory=list)
    safety_tier: str = "read-only"  # read-only | diagnostic | elevated
    # When True the handler is called as handler(args, context) — context carries
    # request-scoped info (e.g. user_id, session_id) that the agent must not pass
    # as arguments. Used by tools like recall_history that scope to the caller.
    wants_context: bool = False

    def openai_schema(self) -> dict[str, Any]:
        """Full tool schema in the OpenAI/LiteLLM function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def summary(self) -> dict[str, Any]:
        """Compact form returned by discovery (cheap to put in context)."""
        return {
            "name": self.name,
            "category": self.category,
            "when_to_use": self.when_to_use,
            "description": self.description,
            "safety_tier": self.safety_tier,
        }
