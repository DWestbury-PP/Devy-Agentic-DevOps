"""Provider-agnostic model access via LiteLLM.

The harness talks to this module, never to a vendor SDK directly. A model
*tier* (resolved from config) carries the concrete LiteLLM model string, so any
provider — OpenAI, Anthropic, Google, Ollama, ... — works through one path.

The ``completion_fn`` seam exists so tests can inject scripted responses without
a live model or network. In production it defaults to ``litellm.completion``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Optional

from agentic_devops.config import ModelTier


@dataclass
class ToolCall:
    """A single tool invocation requested by the model (provider-normalized)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderResponse:
    """Normalized, provider-independent result of one model call."""

    text: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _parse_arguments(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if not raw_args:
        return {}
    try:
        parsed = json.loads(raw_args)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (json.JSONDecodeError, TypeError):
        return {}


def _default_completion_fn(**kwargs: Any) -> Any:
    # Imported lazily so unit tests using the seam don't require litellm.
    import litellm

    return litellm.completion(**kwargs)


class ProviderClient:
    """Resolves tiers to models and performs (normalized) completions."""

    def __init__(self, completion_fn: Optional[Callable[..., Any]] = None) -> None:
        self._completion_fn = completion_fn or _default_completion_fn

    def complete(
        self,
        messages: list[dict[str, Any]],
        tier: ModelTier,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": tier.model,
            "messages": messages,
            "max_tokens": tier.max_tokens,
        }
        if tier.temperature is not None:
            kwargs["temperature"] = tier.temperature
        if tier.api_base:
            kwargs["api_base"] = tier.api_base
        if tools:
            kwargs["tools"] = tools

        raw = self._completion_fn(**kwargs)
        return self._normalize(raw)

    def stream(
        self,
        messages: list[dict[str, Any]],
        tier: ModelTier,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> Generator[dict[str, Any], None, ProviderResponse]:
        """Stream one model step.

        Yields ``{"type": "delta", "text": ...}`` events as text arrives, and
        ``return``s the assembled :class:`ProviderResponse` (capture it with
        ``response = yield from provider.stream(...)``). Tool-call fragments are
        accumulated across chunks and surfaced only on the final response.
        """
        kwargs: dict[str, Any] = {
            "model": tier.model,
            "messages": messages,
            "max_tokens": tier.max_tokens,
            "stream": True,
        }
        if tier.temperature is not None:
            kwargs["temperature"] = tier.temperature
        if tier.api_base:
            kwargs["api_base"] = tier.api_base
        if tools:
            kwargs["tools"] = tools

        text_parts: list[str] = []
        fragments: dict[int, dict[str, str]] = {}

        for chunk in self._completion_fn(**kwargs):
            choices = chunk.choices if hasattr(chunk, "choices") else chunk.get("choices", [])
            if not choices:
                continue
            choice = choices[0]
            delta = choice.delta if hasattr(choice, "delta") else choice.get("delta", {})

            content = getattr(delta, "content", None) if not isinstance(delta, dict) else delta.get("content")
            if content:
                text_parts.append(content)
                yield {"type": "delta", "text": content}

            raw_tcs = (
                getattr(delta, "tool_calls", None) if not isinstance(delta, dict) else delta.get("tool_calls")
            ) or []
            for tc in raw_tcs:
                idx = (getattr(tc, "index", None) if not isinstance(tc, dict) else tc.get("index")) or 0
                frag = fragments.setdefault(idx, {"id": "", "name": "", "args": ""})
                tc_id = getattr(tc, "id", None) if not isinstance(tc, dict) else tc.get("id")
                if tc_id:
                    frag["id"] = tc_id
                fn = getattr(tc, "function", None) if not isinstance(tc, dict) else tc.get("function")
                if fn:
                    name = getattr(fn, "name", None) if not isinstance(fn, dict) else fn.get("name")
                    args = getattr(fn, "arguments", None) if not isinstance(fn, dict) else fn.get("arguments")
                    if name:
                        frag["name"] = name
                    if args:
                        frag["args"] += args

        tool_calls = [
            ToolCall(
                id=frag["id"] or f"call_{idx}",
                name=frag["name"],
                arguments=_parse_arguments(frag["args"]),
            )
            for idx, frag in sorted(fragments.items())
            if frag["name"]
        ]
        return ProviderResponse(text="".join(text_parts) or None, tool_calls=tool_calls)

    @staticmethod
    def _normalize(raw: Any) -> ProviderResponse:
        # LiteLLM returns an OpenAI-shaped response object (or a compatible dict).
        choice = raw.choices[0] if hasattr(raw, "choices") else raw["choices"][0]
        message = choice.message if hasattr(choice, "message") else choice["message"]

        text = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")

        raw_tool_calls = (
            getattr(message, "tool_calls", None)
            if not isinstance(message, dict)
            else message.get("tool_calls")
        ) or []

        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(raw_tool_calls):
            fn = tc.function if hasattr(tc, "function") else tc["function"]
            name = fn.name if hasattr(fn, "name") else fn["name"]
            args = fn.arguments if hasattr(fn, "arguments") else fn["arguments"]
            tc_id = (getattr(tc, "id", None) if not isinstance(tc, dict) else tc.get("id")) or f"call_{i}"
            tool_calls.append(ToolCall(id=tc_id, name=name, arguments=_parse_arguments(args)))

        usage_obj = getattr(raw, "usage", None) if not isinstance(raw, dict) else raw.get("usage")
        usage: dict[str, Any] = {}
        if usage_obj is not None:
            if hasattr(usage_obj, "model_dump"):
                usage = usage_obj.model_dump()
            elif isinstance(usage_obj, dict):
                usage = usage_obj

        return ProviderResponse(text=text, tool_calls=tool_calls, usage=usage, raw=raw)
