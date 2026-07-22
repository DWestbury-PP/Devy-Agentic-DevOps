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
from agentic_devops.proxy.errors import ProviderError, classify


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
    served_by: Optional[str] = None  # the model that actually answered (may be a fallback)
    fell_back: bool = False  # True when a backup model served this response

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


def _has_image_parts(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )


def _messages_for_model(model: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pass messages through untouched unless they carry images AND the target
    model can't do vision — in which case image parts are replaced with a short
    text note so a non-vision fallback degrades gracefully instead of erroring.
    Unknown models are assumed vision-capable (the primary here is Claude)."""
    if not _has_image_parts(messages):
        return messages
    try:
        import litellm

        if litellm.supports_vision(model=model):
            return messages
    except Exception:  # noqa: BLE001 — offline / unknown model → keep images (primary is vision)
        return messages
    out: list[dict[str, Any]] = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            text = " ".join(p.get("text", "") for p in c if p.get("type") == "text").strip()
            n_img = sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
            if n_img:
                text = (text + f" [{n_img} image(s) omitted — this model can't view images]").strip()
            out.append({**m, "content": text})
        else:
            out.append(m)
    return out


def _default_completion_fn(**kwargs: Any) -> Any:
    # Imported lazily so unit tests using the seam don't require litellm.
    import litellm

    return litellm.completion(**kwargs)


class ProviderClient:
    """Resolves tiers to models and performs (normalized) completions."""

    def __init__(
        self,
        completion_fn: Optional[Callable[..., Any]] = None,
        request_timeout: Optional[float] = None,
    ) -> None:
        self._completion_fn = completion_fn or _default_completion_fn
        # Bounds every provider call so a stalled (streaming) connection raises
        # instead of hanging the turn — and its worker thread — forever.
        self._request_timeout = request_timeout

    def _kwargs(
        self,
        tier: ModelTier,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        stream: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": tier.model,
            "messages": _messages_for_model(tier.model, messages),
            "max_tokens": tier.max_tokens,
        }
        if stream:
            kwargs["stream"] = True
        if tier.temperature is not None:
            kwargs["temperature"] = tier.temperature
        if tier.api_base:
            kwargs["api_base"] = tier.api_base
        if tools:
            kwargs["tools"] = tools
        if self._request_timeout is not None:
            kwargs["timeout"] = self._request_timeout
        return kwargs

    def complete(
        self,
        messages: list[dict[str, Any]],
        tier: ModelTier,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> ProviderResponse:
        """Complete one step, failing over to ``tier.fallbacks`` when worthwhile.

        The primary is tried first; on a *failoverable* error (billing/credit,
        auth, rate-limit, overload, timeout — see :func:`errors.classify`) the
        next backup model is tried. A non-failoverable error (context too large,
        content policy, malformed request) short-circuits — retrying elsewhere
        would fail identically — and surfaces as a :class:`ProviderError`.
        """
        attempts = [tier, *tier.fallbacks]
        for i, t in enumerate(attempts):
            try:
                raw = self._completion_fn(**self._kwargs(t, messages, tools))
                resp = self._normalize(raw)
                resp.served_by = t.model
                resp.fell_back = i > 0
                return resp
            except Exception as exc:  # noqa: BLE001 — classified, then re-raised as ProviderError
                failure = classify(exc)
                if i < len(attempts) - 1 and failure.failoverable:
                    continue
                raise ProviderError(failure, tried_backup=i > 0) from exc
        raise AssertionError("unreachable")  # pragma: no cover — attempts is never empty

    def stream(
        self,
        messages: list[dict[str, Any]],
        tier: ModelTier,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> Generator[dict[str, Any], None, ProviderResponse]:
        """Stream one model step, with pre-stream failover.

        Yields ``{"type": "delta", "text": ...}`` events as text arrives, and
        ``return``s the assembled :class:`ProviderResponse` (capture it with
        ``response = yield from provider.stream(...)``). Tool-call fragments are
        accumulated across chunks and surfaced only on the final response.

        Failover is attempted only while **nothing has streamed yet** — once a
        delta reaches the client we can't cleanly restart on another model, so a
        later failure surfaces as a :class:`ProviderError` instead. In practice
        the account/outage failures we care about (billing, auth, overload) fire
        on the first call, before any token, so this covers them.
        """
        attempts = [tier, *tier.fallbacks]
        for i, t in enumerate(attempts):
            text_parts: list[str] = []
            fragments: dict[int, dict[str, str]] = {}
            emitted = False
            try:
                for chunk in self._completion_fn(**self._kwargs(t, messages, tools, stream=True)):
                    choices = chunk.choices if hasattr(chunk, "choices") else chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.delta if hasattr(choice, "delta") else choice.get("delta", {})

                    content = getattr(delta, "content", None) if not isinstance(delta, dict) else delta.get("content")
                    if content:
                        text_parts.append(content)
                        emitted = True
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
            except Exception as exc:  # noqa: BLE001 — classified, then re-raised as ProviderError
                failure = classify(exc)
                # Only fail over if nothing has streamed yet (a mid-stream restart
                # would double-answer) and a backup remains worth trying.
                if not emitted and i < len(attempts) - 1 and failure.failoverable:
                    continue
                raise ProviderError(failure, tried_backup=i > 0) from exc

            tool_calls = [
                ToolCall(
                    id=frag["id"] or f"call_{idx}",
                    name=frag["name"],
                    arguments=_parse_arguments(frag["args"]),
                )
                for idx, frag in sorted(fragments.items())
                if frag["name"]
            ]
            return ProviderResponse(
                text="".join(text_parts) or None,
                tool_calls=tool_calls,
                served_by=t.model,
                fell_back=i > 0,
            )
        raise AssertionError("unreachable")  # pragma: no cover — attempts is never empty

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
