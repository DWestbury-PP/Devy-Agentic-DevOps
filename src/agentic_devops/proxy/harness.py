"""The agent harness: the single loop that drives one capable agent.

assemble context -> model call -> (discover/execute tools) -> repeat -> answer.

This is deliberately small and owned, not inherited from a heavyweight
framework (see docs/JOURNEY.md, Pivot 4). Both the non-streaming path
(:func:`run_turn`, used by ``/v1/complete`` and tests) and the streaming path
(:func:`run_turn_streaming`, used by the ``/v1/chat`` SSE route) share the same
tool-handling core.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Optional

from agentic_devops.config import ModelTier, Settings
from agentic_devops.proxy.auth import tier_allows
from agentic_devops.proxy.providers import ProviderClient, ProviderResponse
from agentic_devops.proxy.tracing import NOOP_SPAN, Span, Tracer
from agentic_devops.tools.base import ToolImage, ToolResult
from agentic_devops.tools.router import FIND_TOOLS_NAME, ToolNotFoundError, ToolsRouter

EventCallback = Callable[[dict[str, Any]], None]

_PREVIEW_CHARS = 500
_GUARD_MESSAGE = (
    "I reached the maximum number of reasoning steps before finishing. "
    "Here is what I gathered so far; please narrow the question if needed."
)
# Subtle trail note when a backup model served the turn. Deliberately does NOT
# name the concrete model — the tier abstraction hides that from users (the
# concrete model lands in the trace/audit instead).
_FALLBACK_NOTICE = "Primary model unavailable — answered with a backup model."


@dataclass
class TurnResult:
    text: str
    messages: list[dict[str, Any]]  # full message list after the turn (incl. tool scaffolding)
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    # Distilled tool evidence for the context channel: {tool, intent, result, ok}.
    # The display transcript stays clean; these feed Devy's working memory.
    tool_findings: list[dict[str, Any]] = field(default_factory=list)
    # Images a tool RENDERED this turn (e.g. Grafana panels), persisted to the blob
    # store: {ref, mime, name}. The endpoint attaches these to the stored assistant
    # turn so they survive in the transcript (history reload), like user attachments.
    rendered_images: list[dict[str, Any]] = field(default_factory=list)


def _assistant_tool_call_message(response: ProviderResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.text or None,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in response.tool_calls
        ],
    }


def _llm_span_name(tier: ModelTier) -> str:
    # Pre-call name = the primary model we're about to try (accurate if the call
    # errors out entirely). On success it's renamed to the model that actually
    # served — which may be a fallback (see run_turn / run_turn_streaming).
    return f"llm: {tier.model}"


def _llm_meta(tier: ModelTier, response: ProviderResponse) -> dict[str, Any]:
    """Non-sensitive trace metadata pinning which concrete model served this call."""
    return {
        "model": response.served_by or tier.model,
        "tier": tier.label or tier.model,
        "fell_back": response.fell_back,
    }


def _turn_inputs(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The user's prompt for the turn's root span (last message in the assembled list)."""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content") or ""
            if isinstance(content, list):  # multimodal turn → text parts + image count
                texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                n_img = sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
                content = " ".join(t for t in texts if t) + (f" [+{n_img} image(s)]" if n_img else "")
            return {"input": content}
    return {}


def _image_tool_content(raw: ToolResult, refs: list[str]) -> str:
    """The tool-result text a model sees when a tool rendered image(s). When the
    images were persisted (``refs``), it hands the model an embeddable Markdown URL
    per image so it can place each one INLINE in its answer where it's relevant —
    not appended in a lump. No base64 (the pixels arrive as a separate vision
    message)."""
    if not refs:
        return raw.placeholder()
    n = len(refs)
    it = "them" if n > 1 else "it"
    embeds = "  ".join(f"![caption](/v1/blobs/{r})" for r in refs)
    base = (raw.text + "\n") if raw.text else ""
    return (
        f"{base}Rendered {n} image(s) and shown you {it} above. To DISPLAY {it} to the "
        f"user, embed the image inline in your written answer at the point where it's "
        f"relevant, using Markdown (replace 'caption' with a short label): {embeds}"
    )


def _image_message(images: list[ToolImage]) -> dict[str, Any]:
    """A user-role multimodal message carrying rendered images for a vision model
    (portable ``image_url`` data-URI parts — LiteLLM maps these per provider)."""
    content: list[dict[str, Any]] = [
        {"type": "text", "text": "Rendered image(s) from the tool call above — "
         "shown to the user and provided here for your visual analysis."}
    ]
    for im in images:
        content.append({"type": "image_url", "image_url": {"url": im.data_uri()}})
    return {"role": "user", "content": content}


def _process_tool_calls(
    response: ProviderResponse,
    router: ToolsRouter,
    available_tools: list[dict[str, Any]],
    loaded: set[str],
    tools_used: list[str],
    tool_context: Optional[dict[str, Any]] = None,
    turn_span: Span = NOOP_SPAN,
    image_sink: Optional[Any] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Handle one round of tool calls.

    Mutates ``available_tools`` (find_tools auto-load), ``loaded``, and
    ``tools_used``. Returns ``(tool_result_messages, events, findings, rendered)``.
    ``findings`` captures real tool evidence; ``rendered`` are images a tool
    produced, persisted via ``image_sink(ToolImage) -> ref`` (blob store) so they
    can be attached to the stored assistant turn.
    """
    messages: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    rendered: list[dict[str, Any]] = []
    collected_images: list[ToolImage] = []

    for tc in response.tool_calls:
        events.append({"type": "tool_call", "name": tc.name, "arguments": tc.arguments})

        with turn_span.tool(f"tool: {tc.name}", tc.arguments) as span:
            if tc.name == FIND_TOOLS_NAME:
                specs = router.find(tc.arguments.get("intent"), tc.arguments.get("category"))
                for spec in specs:
                    if spec.name not in loaded:
                        available_tools.append(spec.openai_schema())
                        loaded.add(spec.name)
                content = json.dumps(
                    {
                        "found": [s.summary() for s in specs],
                        "note": "These tools are now callable. Call them directly to proceed.",
                    }
                )
                events.append({"type": "tools_found", "names": [s.name for s in specs]})
                span.outputs({"found": [s.name for s in specs]},
                             ok=True, meta={"tool": tc.name, "n_found": len(specs)})
            else:
                tools_used.append(tc.name)
                # RBAC-2: gate by the caller's permitted tier (from tool_context). Absent
                # → 'elevated' (unrestricted) so non-chat callers / tests aren't limited.
                _spec = router.get_spec(tc.name)
                _allowed = (tool_context or {}).get("allowed_tier", "elevated")
                if _spec is not None and not tier_allows(_allowed, _spec.safety_tier):
                    content = (
                        f"ERROR: tool {tc.name!r} requires the {_spec.safety_tier!r} permission "
                        f"tier; your role allows up to {_allowed!r}. Ask an admin if you need it."
                    )
                    events.append({"type": "tool_denied", "name": tc.name, "required": _spec.safety_tier})
                    span.outputs(ok=False, meta={"tool": tc.name, "denied": _spec.safety_tier})
                else:
                    tc_images: list[ToolImage] = []
                    try:
                        raw = router.execute(tc.name, tc.arguments, context=tool_context)
                        if isinstance(raw, ToolResult):
                            tc_images = raw.images
                            # Persist first (base64 never enters the model's text
                            # context) so we can hand the model an embeddable URL for
                            # each image — it places them INLINE in its answer.
                            tc_refs: list[str] = []
                            if image_sink is not None:
                                for im in tc_images:
                                    try:
                                        ref = image_sink(im)
                                    except Exception:  # noqa: BLE001 — never fail a turn on a blob write
                                        ref = None
                                    if ref:
                                        tc_refs.append(ref)
                                        rendered.append({"ref": ref, "mime": im.mime, "name": tc.name})
                            content = _image_tool_content(raw, tc_refs)
                        else:
                            content = str(raw)
                        ok = True
                    except ToolNotFoundError:
                        content = (
                            f"ERROR: tool {tc.name!r} is not available. "
                            f"Use {FIND_TOOLS_NAME} to discover valid tools first."
                        )
                        ok = False
                    except Exception as exc:  # surface tool failures to the model, don't crash
                        content = f"ERROR: tool {tc.name!r} failed: {exc}"
                        ok = False
                    event = {"type": "tool_result", "name": tc.name, "ok": ok, "preview": content[:_PREVIEW_CHARS]}
                    if tc_images:
                        event["images"] = [{"mime": im.mime, "data": im.data} for im in tc_images]
                        collected_images.extend(tc_images)
                    events.append(event)
                    findings.append(
                        {
                            "tool": tc.name,
                            "intent": json.dumps(tc.arguments, default=str)[:200],
                            "result": content,
                            "ok": ok,
                        }
                    )
                    span.outputs({"result": content}, ok=ok, meta={"tool": tc.name, "images": len(tc_images)})

        messages.append(
            {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content}
        )

    # Vision hand-off: append the rendered image(s) as a user image message so a
    # vision model can actually SEE them. Context-channel only (never persisted to
    # the display transcript); the provider strips it for a non-vision fallback.
    if collected_images:
        messages.append(_image_message(collected_images))

    return messages, events, findings, rendered


def _accumulate_usage(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in (source or {}).items():
        if isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value


def run_turn(
    provider: ProviderClient,
    router: ToolsRouter,
    settings: Settings,
    messages: list[dict[str, Any]],
    tier: ModelTier,
    on_event: Optional[EventCallback] = None,
    tool_context: Optional[dict[str, Any]] = None,
    tracer: Optional[Tracer] = None,
    image_sink: Optional[Any] = None,
) -> TurnResult:
    """Run one user turn to completion (non-streaming)."""

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    working = list(messages)
    available_tools = [router.find_tools_schema()]
    loaded: set[str] = set()
    tools_used: list[str] = []
    tool_findings: list[dict[str, Any]] = []
    rendered_images: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    final_text = ""
    iterations = 0
    notified_fallback = False
    models_used: list[str] = []

    session_id = (tool_context or {}).get("session_id") or ""
    turn = tracer.turn(session_id, "devy.turn", _turn_inputs(messages)) if tracer else NOOP_SPAN
    with turn:
        while iterations < settings.max_iterations:
            iterations += 1
            with turn.llm(_llm_span_name(tier), {"messages": working}) as llm:
                response = provider.complete(working, tier=tier, tools=available_tools)
                llm.outputs({"completion": response.text,
                             "tool_calls": [t.name for t in response.tool_calls]},
                            usage=response.usage, meta=_llm_meta(tier, response),
                            name=f"llm: {response.served_by}" if response.served_by else None)
            _accumulate_usage(usage, response.usage)
            if response.served_by and response.served_by not in models_used:
                models_used.append(response.served_by)
            if response.fell_back and not notified_fallback:
                notified_fallback = True
                emit({"type": "notice", "message": _FALLBACK_NOTICE})

            if not response.wants_tools:
                final_text = response.text or ""
                working.append({"role": "assistant", "content": final_text})
                break

            working.append(_assistant_tool_call_message(response))
            tool_messages, events, findings, rendered = _process_tool_calls(
                response, router, available_tools, loaded, tools_used, tool_context, turn,
                image_sink,
            )
            tool_findings.extend(findings)
            rendered_images.extend(rendered)
            for event in events:
                emit(event)
            working.extend(tool_messages)
        else:
            final_text = _GUARD_MESSAGE
            working.append({"role": "assistant", "content": final_text})

        turn.outputs({"output": final_text}, usage=usage,
                     meta={"iterations": iterations, "tools_used": tools_used,
                           "models": models_used, "fell_back": notified_fallback})

    emit({"type": "done", "iterations": iterations, "usage": usage})
    return TurnResult(
        text=final_text, messages=working, tools_used=tools_used, iterations=iterations,
        usage=usage, tool_findings=tool_findings, rendered_images=rendered_images,
    )


def run_turn_streaming(
    provider: ProviderClient,
    router: ToolsRouter,
    settings: Settings,
    messages: list[dict[str, Any]],
    tier: ModelTier,
    tool_context: Optional[dict[str, Any]] = None,
    tracer: Optional[Tracer] = None,
    image_sink: Optional[Any] = None,
) -> Generator[dict[str, Any], None, TurnResult]:
    """Run one user turn, yielding events as they happen.

    Yields the same event dicts as :func:`run_turn`'s ``on_event`` callback,
    plus ``{"type": "delta", "text": ...}`` for streamed text. ``return``s the
    final :class:`TurnResult` (capture via ``yield from``).
    """
    working = list(messages)
    available_tools = [router.find_tools_schema()]
    loaded: set[str] = set()
    tools_used: list[str] = []
    tool_findings: list[dict[str, Any]] = []
    rendered_images: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    final_text = ""
    iterations = 0
    notified_fallback = False
    models_used: list[str] = []

    session_id = (tool_context or {}).get("session_id") or ""
    turn = tracer.turn(session_id, "devy.turn", _turn_inputs(messages)) if tracer else NOOP_SPAN
    with turn:
        while iterations < settings.max_iterations:
            iterations += 1
            # Forward the provider's delta events; capture the assembled response.
            with turn.llm(_llm_span_name(tier), {"messages": working}) as llm:
                response: ProviderResponse = yield from provider.stream(
                    working, tier=tier, tools=available_tools
                )
                llm.outputs({"completion": response.text,
                             "tool_calls": [t.name for t in response.tool_calls]},
                            usage=response.usage, meta=_llm_meta(tier, response),
                            name=f"llm: {response.served_by}" if response.served_by else None)
            _accumulate_usage(usage, response.usage)
            if response.served_by and response.served_by not in models_used:
                models_used.append(response.served_by)
            if response.fell_back and not notified_fallback:
                notified_fallback = True
                yield {"type": "notice", "message": _FALLBACK_NOTICE}

            if not response.wants_tools:
                final_text = response.text or ""
                working.append({"role": "assistant", "content": final_text})
                break

            working.append(_assistant_tool_call_message(response))
            tool_messages, events, findings, rendered = _process_tool_calls(
                response, router, available_tools, loaded, tools_used, tool_context, turn,
                image_sink,
            )
            tool_findings.extend(findings)
            rendered_images.extend(rendered)
            for event in events:
                yield event
            working.extend(tool_messages)
        else:
            final_text = _GUARD_MESSAGE
            working.append({"role": "assistant", "content": final_text})

        turn.outputs({"output": final_text}, usage=usage,
                     meta={"iterations": iterations, "tools_used": tools_used,
                           "models": models_used, "fell_back": notified_fallback})

    yield {"type": "done", "iterations": iterations, "usage": usage, "text": final_text}
    return TurnResult(
        text=final_text, messages=working, tools_used=tools_used, iterations=iterations,
        usage=usage, tool_findings=tool_findings, rendered_images=rendered_images,
    )
