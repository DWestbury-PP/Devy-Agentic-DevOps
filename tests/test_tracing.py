"""LangSmith waterfall tracing: span tree shape, full/metadata payload gating,
and tracer selection. Hermetic — a FakeRun stands in for the LangSmith RunTree,
so nothing touches the network or requires the langsmith package.
"""

from agentic_devops.config import LangSmithConfig, ModelTier, SecretsConfig, Settings
from agentic_devops.proxy.harness import run_turn
from agentic_devops.proxy.providers import ProviderResponse, ToolCall
from agentic_devops.proxy.tracing import (
    JsonlTracer,
    NoopTracer,
    _LangSmithSpan,
    get_tracer,
)
from agentic_devops.tools.base import ToolSpec
from agentic_devops.tools.router import ToolsRouter


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRun:
    """Mimics the slice of LangSmith's RunTree the tracer uses."""

    def __init__(self, name, run_type, inputs):
        self.name, self.run_type, self.inputs = name, run_type, inputs
        self.children, self.outputs, self.error = [], None, None
        self.posted = self.patched = False
        self.extra = {}

    def post(self):
        self.posted = True

    def create_child(self, name, run_type, inputs):
        child = FakeRun(name, run_type, inputs)
        self.children.append(child)
        return child

    def end(self, outputs=None, error=None):
        self.outputs, self.error = outputs, error

    def patch(self):
        self.patched = True


class FakeTracer:
    """Returns a real _LangSmithSpan wrapping a FakeRun — exercises the real span
    code with the harness, no network."""

    def __init__(self, full=True):
        self.full, self.root = full, None

    def event(self, *a):
        pass

    def turn(self, session_id, name, inputs):
        self.root = FakeRun(name, "chain", dict(inputs) if self.full else {})
        self.root.post()  # mirrors LangSmithTracer.turn
        return _LangSmithSpan(self.root, self.full)


class FakeProvider:
    def __init__(self, responses):
        self._responses = list(responses)

    def complete(self, messages, tier, tools=None):
        r = self._responses.pop(0)
        r.usage = {"total_tokens": 7}
        return r


def _router():
    router = ToolsRouter()
    router.register(ToolSpec(
        name="host_diagnostics", category="host-diagnostics", description="check host",
        when_to_use="check disk",
        input_schema={"type": "object", "properties": {"check": {"type": "string"}}},
        handler=lambda args: f"disk ok for {args['check']}",
    ))
    return router


def _run(tracer):
    provider = FakeProvider([
        ProviderResponse(tool_calls=[ToolCall(id="c1", name="find_tools", arguments={"intent": "disk"})]),
        ProviderResponse(tool_calls=[ToolCall(id="c2", name="host_diagnostics", arguments={"check": "disk"})]),
        ProviderResponse(text="Healthy."),
    ])
    return run_turn(
        provider, _router(), Settings(max_iterations=8),
        messages=[{"role": "user", "content": "is the disk ok?"}],
        tier=ModelTier(model="fake/model", label="Deep"),
        tool_context={"session_id": "s1"}, tracer=tracer,
    )


# --------------------------------------------------------------------------- #
# Span tree shape (the waterfall)
# --------------------------------------------------------------------------- #
def test_waterfall_tree_shape():
    tracer = FakeTracer(full=True)
    result = _run(tracer)
    assert result.text == "Healthy."
    root = tracer.root
    assert root.run_type == "chain" and root.posted and root.patched
    kinds = [(c.run_type, c.name) for c in root.children]
    # 3 LLM calls interleaved with find_tools + the real tool
    assert [k[0] for k in kinds] == ["llm", "tool", "llm", "tool", "llm"]
    assert kinds[1] == ("tool", "tool: find_tools")
    assert kinds[3] == ("tool", "tool: host_diagnostics")
    assert all(c.patched for c in root.children)


def test_root_records_usage_and_iteration_meta():
    tracer = FakeTracer(full=True)
    _run(tracer)
    root = tracer.root
    assert root.outputs["iterations"] == 3
    assert root.outputs["tools_used"] == ["host_diagnostics"]
    assert root.extra["metadata"]["usage"]["total_tokens"] == 21  # 3 calls × 7


# --------------------------------------------------------------------------- #
# Payload gating: full vs metadata
# --------------------------------------------------------------------------- #
def test_full_mode_captures_bodies():
    tracer = FakeTracer(full=True)
    _run(tracer)
    root = tracer.root
    assert root.inputs == {"input": "is the disk ok?"}          # prompt captured
    tool = root.children[3]                                      # host_diagnostics
    assert tool.inputs == {"check": "disk"}                     # args captured
    assert tool.outputs["result"] == "disk ok for disk"        # result captured
    assert tool.outputs["ok"] is True and tool.outputs["tool"] == "host_diagnostics"


def test_metadata_mode_omits_bodies_but_keeps_signal():
    tracer = FakeTracer(full=False)
    _run(tracer)
    root = tracer.root
    assert root.inputs == {}                                     # prompt withheld
    tool = root.children[3]
    assert tool.inputs == {}                                     # args withheld
    assert "result" not in (tool.outputs or {})                 # result body withheld
    assert tool.outputs["ok"] is True and tool.outputs["tool"] == "host_diagnostics"
    # token usage is non-sensitive → still recorded even in metadata mode
    assert root.extra["metadata"]["usage"]["total_tokens"] == 21


def test_span_never_raises_when_sink_fails():
    class Boom(FakeRun):
        def create_child(self, *a, **k):
            raise RuntimeError("langsmith down")

    span = _LangSmithSpan(Boom("t", "chain", {}), full=True)
    with span:
        child = span.tool("tool: x", {"a": 1})   # swallowed → no-op child
        child.outputs({"result": "r"}, ok=True)  # must not raise


# --------------------------------------------------------------------------- #
# Tracer selection / capture derivation
# --------------------------------------------------------------------------- #
def test_get_tracer_defaults_jsonl():
    assert isinstance(get_tracer(Settings(tracing="jsonl")), JsonlTracer)


def test_get_tracer_none():
    assert isinstance(get_tracer(Settings(tracing="none")), NoopTracer)


def test_langsmith_without_key_falls_back_to_jsonl(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert isinstance(get_tracer(Settings(tracing="langsmith")), JsonlTracer)


def test_capture_derives_from_mode(monkeypatch):
    from agentic_devops.proxy import tracing as t

    captured = {}

    class _Client:
        def __init__(self, **kw):
            pass

    def _fake_tracer(client, project, capture):
        captured["capture"] = capture
        return NoopTracer()

    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setattr(t, "LangSmithTracer", _fake_tracer)
    # langsmith.Client is imported inside _build_langsmith; stub the module attr
    import sys
    import types
    fake_mod = types.ModuleType("langsmith")
    fake_mod.Client = _Client
    monkeypatch.setitem(sys.modules, "langsmith", fake_mod)

    prod = Settings(tracing="langsmith", secrets=SecretsConfig(mode="prod"))
    t.get_tracer(prod)
    assert captured["capture"] == "metadata"

    dev = Settings(tracing="langsmith", secrets=SecretsConfig(mode="dev"))
    t.get_tracer(dev)
    assert captured["capture"] == "full"


def test_explicit_capture_overrides_mode(monkeypatch):
    from agentic_devops.proxy import tracing as t

    captured = {}
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setattr(t, "LangSmithTracer",
                        lambda client, project, capture: captured.update(capture=capture) or NoopTracer())
    import sys
    import types
    fake_mod = types.ModuleType("langsmith")
    fake_mod.Client = lambda **kw: object()
    monkeypatch.setitem(sys.modules, "langsmith", fake_mod)

    s = Settings(tracing="langsmith", secrets=SecretsConfig(mode="prod"),
                 langsmith=LangSmithConfig(capture="full"))
    t.get_tracer(s)
    assert captured["capture"] == "full"  # explicit config beats prod default
