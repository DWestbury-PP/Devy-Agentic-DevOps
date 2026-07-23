"""Tool specification: the metadata that powers on-demand discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Handlers are synchronous in Phase 1: they take validated arguments and return
# a string (or anything str()-able) that becomes the tool result — OR a
# ``ToolResult`` when the tool produces images (e.g. a rendered Grafana panel).
ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass
class ToolImage:
    """An image a tool produced — base64 payload + mime, never inlined into the
    model's TEXT context (that would waste tokens and be unreadable). The harness
    surfaces it to the UI and, for vision models, as an actual image block."""

    data: str            # base64-encoded bytes
    mime: str = "image/png"

    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{self.data}"


@dataclass
class ToolResult:
    """A richer tool result carrying text AND images. Handlers may return a plain
    string (the common case) or this when there's an image to render/analyze."""

    text: str
    images: list[ToolImage] = field(default_factory=list)
    # An optional out-of-band event the harness forwards to the UI stream verbatim
    # (e.g. ``{"type": "action_proposed", "action": {...}}`` from request_action) —
    # a clean seam for a tool to surface a UI signal without the harness knowing the
    # tool. Never enters the model's text context.
    event: Optional[dict[str, Any]] = None

    def placeholder(self) -> str:
        """Short text stand-in for storage/findings — the base64 never lands in
        the transcript, summary, or model text context."""
        if not self.images:
            return self.text
        tag = f"[rendered {len(self.images)} image(s): {', '.join(i.mime for i in self.images)}]"
        return f"{self.text}\n{tag}".strip() if self.text else tag


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
