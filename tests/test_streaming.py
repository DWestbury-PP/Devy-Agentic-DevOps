"""Streaming harness loop + provider stream-chunk accumulation (offline)."""

from types import SimpleNamespace

from agentic_devops.config import ModelTier, Settings
from agentic_devops.proxy.harness import run_turn_streaming
from agentic_devops.proxy.providers import ProviderClient, ProviderResponse, ToolCall
from agentic_devops.tools.base import ToolSpec
from agentic_devops.tools.router import ToolsRouter

TIER = ModelTier(model="fake/model")


def _drain(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


class FakeStreamProvider:
    def __init__(self, scripted):
        self._scripted = list(scripted)

    def stream(self, messages, tier, tools=None):
        deltas, response = self._scripted.pop(0)
        for piece in deltas:
            yield {"type": "delta", "text": piece}
        return response


def _router():
    router = ToolsRouter()
    router.register(
        ToolSpec(
            name="host_diagnostics",
            category="host-diagnostics",
            description="check host",
            when_to_use="check disk",
            input_schema={"type": "object", "properties": {"check": {"type": "string"}}},
            handler=lambda args: "disk ok",
        )
    )
    return router


def test_streaming_discovers_executes_and_streams_final_text():
    provider = FakeStreamProvider(
        [
            ([], ProviderResponse(tool_calls=[ToolCall(id="c1", name="find_tools", arguments={"intent": "disk"})])),
            ([], ProviderResponse(tool_calls=[ToolCall(id="c2", name="host_diagnostics", arguments={"check": "disk"})])),
            (["All ", "healthy."], ProviderResponse(text="All healthy.")),
        ]
    )
    gen = run_turn_streaming(provider, _router(), Settings(max_iterations=6),
                             [{"role": "user", "content": "disk ok?"}], TIER)
    events, result = _drain(gen)

    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert deltas == "All healthy."
    assert result.text == "All healthy."
    assert result.tools_used == ["host_diagnostics"]
    types = [e["type"] for e in events]
    assert "tools_found" in types and "tool_result" in types
    assert any(e["type"] == "done" for e in events)


def _chunk(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls))])


def test_provider_stream_accumulates_text():
    chunks = [_chunk("Hel"), _chunk("lo")]
    client = ProviderClient(completion_fn=lambda **kw: iter(chunks))
    gen = client.stream([{"role": "user", "content": "hi"}], tier=TIER)
    deltas, resp = _drain(gen)
    assert "".join(d["text"] for d in deltas) == "Hello"
    assert resp.text == "Hello"


def test_provider_stream_accumulates_tool_call_fragments():
    chunks = [
        _chunk(tool_calls=[SimpleNamespace(index=0, id="x", function=SimpleNamespace(name="host_diagnostics", arguments='{"check":'))]),
        _chunk(tool_calls=[SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments=' "disk"}'))]),
    ]
    client = ProviderClient(completion_fn=lambda **kw: iter(chunks))
    _, resp = _drain(client.stream([{"role": "user", "content": "hi"}], tier=TIER))
    assert resp.wants_tools
    assert resp.tool_calls[0].name == "host_diagnostics"
    assert resp.tool_calls[0].arguments == {"check": "disk"}
