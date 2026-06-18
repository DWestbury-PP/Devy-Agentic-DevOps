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
from agentic_devops.proxy.providers import ProviderClient, ProviderResponse
from agentic_devops.tools.router import FIND_TOOLS_NAME, ToolNotFoundError, ToolsRouter

EventCallback = Callable[[dict[str, Any]], None]

_PREVIEW_CHARS = 500
_GUARD_MESSAGE = (
    "I reached the maximum number of reasoning steps before finishing. "
    "Here is what I gathered so far; please narrow the question if needed."
)


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


def _process_tool_calls(
    response: ProviderResponse,
    router: ToolsRouter,
    available_tools: list[dict[str, Any]],
    loaded: set[str],
    tools_used: list[str],
    tool_context: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Handle one round of tool calls.

    Mutates ``available_tools`` (find_tools auto-load), ``loaded``, and
    ``tools_used``. Returns ``(tool_result_messages, events, findings)`` in order.
    ``findings`` captures real tool evidence (not find_tools discovery) for the
    session's context channel.
    """
    messages: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    for tc in response.tool_calls:
        events.append({"type": "tool_call", "name": tc.name, "arguments": tc.arguments})

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
        else:
            tools_used.append(tc.name)
            try:
                content = str(router.execute(tc.name, tc.arguments, context=tool_context))
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
            events.append(
                {"type": "tool_result", "name": tc.name, "ok": ok, "preview": content[:_PREVIEW_CHARS]}
            )
            findings.append(
                {
                    "tool": tc.name,
                    "intent": json.dumps(tc.arguments, default=str)[:200],
                    "result": content,
                    "ok": ok,
                }
            )

        messages.append(
            {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content}
        )

    return messages, events, findings


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
    usage: dict[str, Any] = {}
    final_text = ""
    iterations = 0

    while iterations < settings.max_iterations:
        iterations += 1
        response = provider.complete(working, tier=tier, tools=available_tools)
        _accumulate_usage(usage, response.usage)

        if not response.wants_tools:
            final_text = response.text or ""
            working.append({"role": "assistant", "content": final_text})
            break

        working.append(_assistant_tool_call_message(response))
        tool_messages, events, findings = _process_tool_calls(
            response, router, available_tools, loaded, tools_used, tool_context
        )
        tool_findings.extend(findings)
        for event in events:
            emit(event)
        working.extend(tool_messages)
    else:
        final_text = _GUARD_MESSAGE
        working.append({"role": "assistant", "content": final_text})

    emit({"type": "done", "iterations": iterations, "usage": usage})
    return TurnResult(
        text=final_text, messages=working, tools_used=tools_used, iterations=iterations,
        usage=usage, tool_findings=tool_findings,
    )


def run_turn_streaming(
    provider: ProviderClient,
    router: ToolsRouter,
    settings: Settings,
    messages: list[dict[str, Any]],
    tier: ModelTier,
    tool_context: Optional[dict[str, Any]] = None,
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
    usage: dict[str, Any] = {}
    final_text = ""
    iterations = 0

    while iterations < settings.max_iterations:
        iterations += 1
        # Forward the provider's delta events; capture the assembled response.
        response: ProviderResponse = yield from provider.stream(
            working, tier=tier, tools=available_tools
        )
        _accumulate_usage(usage, response.usage)

        if not response.wants_tools:
            final_text = response.text or ""
            working.append({"role": "assistant", "content": final_text})
            break

        working.append(_assistant_tool_call_message(response))
        tool_messages, events, findings = _process_tool_calls(
            response, router, available_tools, loaded, tools_used, tool_context
        )
        tool_findings.extend(findings)
        for event in events:
            yield event
        working.extend(tool_messages)
    else:
        final_text = _GUARD_MESSAGE
        working.append({"role": "assistant", "content": final_text})

    yield {"type": "done", "iterations": iterations, "usage": usage, "text": final_text}
    return TurnResult(
        text=final_text, messages=working, tools_used=tools_used, iterations=iterations,
        usage=usage, tool_findings=tool_findings,
    )
