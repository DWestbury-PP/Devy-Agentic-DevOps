"""End-to-end test of the agent loop with a scripted fake provider.

Proves the differentiator: the model discovers a tool via find_tools, the tool
becomes callable, the harness executes it, and the loop terminates on a
tool-free answer.
"""

from agentic_devops.config import ModelTier, Settings
from agentic_devops.proxy.harness import run_turn
from agentic_devops.proxy.providers import ProviderResponse, ToolCall
from agentic_devops.tools.base import ToolSpec
from agentic_devops.tools.router import ToolsRouter


def test_tool_context_reaches_context_aware_tool():
    seen = {}

    def whoami(args, ctx):
        seen.update(ctx)
        return "ok"

    router = ToolsRouter()
    router.register(ToolSpec(
        name="whoami", category="memory", description="who am i",
        when_to_use="identity", input_schema={"type": "object", "properties": {}},
        handler=whoami, wants_context=True,
    ))
    provider = FakeProvider([
        ProviderResponse(tool_calls=[ToolCall(id="c1", name="whoami", arguments={})]),
        ProviderResponse(text="done"),
    ])
    run_turn(
        provider, router, Settings(max_iterations=4),
        messages=[{"role": "user", "content": "who am i?"}], tier=ModelTier(model="fake"),
        tool_context={"user_id": "alice", "session_id": "s1"},
    )
    assert seen == {"user_id": "alice", "session_id": "s1"}


class FakeProvider:
    """Returns queued ProviderResponses in order, recording the tool schemas
    it was offered on each call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.offered_tool_names = []

    def complete(self, messages, tier, tools=None):
        self.offered_tool_names.append([t["function"]["name"] for t in (tools or [])])
        return self._responses.pop(0)


def _router_with_echo():
    router = ToolsRouter()
    router.register(
        ToolSpec(
            name="host_diagnostics",
            category="host-diagnostics",
            description="check host",
            when_to_use="check disk",
            input_schema={"type": "object", "properties": {"check": {"type": "string"}}},
            handler=lambda args: f"disk ok for check={args['check']}",
        )
    )
    return router


def _settings():
    return Settings(max_iterations=8)


TIER = ModelTier(model="fake/model")


def test_discover_then_execute_then_finish():
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[ToolCall(id="c1", name="find_tools", arguments={"intent": "disk health"})]
            ),
            ProviderResponse(
                tool_calls=[ToolCall(id="c2", name="host_diagnostics", arguments={"check": "disk"})]
            ),
            ProviderResponse(text="Everything looks healthy."),
        ]
    )
    events = []
    result = run_turn(
        provider, _router_with_echo(), _settings(),
        messages=[{"role": "user", "content": "is the disk ok?"}],
        tier=TIER, on_event=events.append,
    )

    assert result.text == "Everything looks healthy."
    assert result.tools_used == ["host_diagnostics"]
    assert result.iterations == 3

    # First call only offered find_tools; after discovery, host_diagnostics was offered.
    assert provider.offered_tool_names[0] == ["find_tools"]
    assert "host_diagnostics" in provider.offered_tool_names[1]

    types = [e["type"] for e in events]
    assert "tools_found" in types
    assert "tool_result" in types
    assert types[-1] == "done"


def test_tool_findings_captured_for_context_channel():
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[ToolCall(id="c1", name="find_tools", arguments={"intent": "disk"})]
            ),
            ProviderResponse(
                tool_calls=[ToolCall(id="c2", name="host_diagnostics", arguments={"check": "disk"})]
            ),
            ProviderResponse(text="Healthy."),
        ]
    )
    result = run_turn(
        provider, _router_with_echo(), _settings(),
        messages=[{"role": "user", "content": "is the disk ok?"}], tier=TIER,
    )
    # find_tools discovery is NOT a finding; the real tool call IS.
    assert [f["tool"] for f in result.tool_findings] == ["host_diagnostics"]
    f = result.tool_findings[0]
    assert "disk ok for check=disk" in f["result"]
    assert f["ok"] is True


def test_image_tool_result_renders_and_reaches_vision_model():
    """A tool returning a ToolResult with an image: (1) the base64 stays OUT of the
    model's text tool-result and the stored finding, (2) it reaches the model as a
    real image message, (3) the tool_result event carries it for the UI."""
    from agentic_devops.tools.base import ToolImage, ToolResult

    router = ToolsRouter()
    router.register(ToolSpec(
        name="get_panel_image", category="grafana", description="render a panel",
        when_to_use="render panel image", input_schema={"type": "object", "properties": {}},
        handler=lambda args: ToolResult(text="", images=[ToolImage(data="BASE64PNG", mime="image/png")]),
    ))

    calls: list[list] = []

    class RecordingProvider:
        def __init__(self, responses):
            self._r = list(responses)

        def complete(self, messages, tier, tools=None):
            calls.append([dict(m) for m in messages])  # snapshot per call
            return self._r.pop(0)

    events: list[dict] = []
    provider = RecordingProvider([
        ProviderResponse(tool_calls=[ToolCall(id="c1", name="get_panel_image", arguments={})]),
        ProviderResponse(text="The panel reads 8.9%."),
    ])
    result = run_turn(
        provider, router, _settings(),
        messages=[{"role": "user", "content": "render the CPU panel"}],
        tier=ModelTier(model="anthropic/claude-sonnet-4-6"), on_event=events.append,
    )

    # (2) an image user-message reached the model on the 2nd call
    second = calls[1]
    img_msgs = [m for m in second if isinstance(m.get("content"), list)
                and any(p.get("type") == "image_url" for p in m["content"])]
    assert img_msgs, "no image message reached the model"
    assert img_msgs[0]["content"][-1]["image_url"]["url"] == "data:image/png;base64,BASE64PNG"

    # (1) base64 is NOT in the model's tool message, nor the stored finding
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs and all("BASE64PNG" not in (m.get("content") or "") for m in tool_msgs)
    f = [x for x in result.tool_findings if x["tool"] == "get_panel_image"][0]
    assert "BASE64PNG" not in f["result"] and "rendered" in f["result"].lower()

    # (3) the tool_result event carries the image for the web UI
    ev = [e for e in events if e.get("type") == "tool_result" and e["name"] == "get_panel_image"][0]
    assert ev["images"] == [{"mime": "image/png", "data": "BASE64PNG"}]


def test_unknown_tool_call_is_surfaced_not_fatal():
    provider = FakeProvider(
        [
            ProviderResponse(tool_calls=[ToolCall(id="c1", name="ghost_tool", arguments={})]),
            ProviderResponse(text="Recovered."),
        ]
    )
    result = run_turn(
        provider, _router_with_echo(), _settings(),
        messages=[{"role": "user", "content": "hi"}], tier=TIER,
    )
    assert result.text == "Recovered."
    # The tool error was fed back as a tool message rather than raising.
    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    assert any("not available" in m["content"] for m in tool_msgs)


def test_iteration_guard_trips():
    # Provider always asks for a tool -> never finishes -> guard message.
    forever = [
        ProviderResponse(tool_calls=[ToolCall(id=f"c{i}", name="find_tools", arguments={})])
        for i in range(20)
    ]
    result = run_turn(
        FakeProvider(forever), _router_with_echo(), Settings(max_iterations=3),
        messages=[{"role": "user", "content": "loop"}], tier=TIER,
    )
    assert result.iterations == 3
    assert "maximum number" in result.text


def test_fallback_emits_a_single_notice_event():
    # A backup model served the answer (fell_back=True) -> one subtle notice.
    provider = FakeProvider([ProviderResponse(text="answered by backup", fell_back=True)])
    events = []
    result = run_turn(
        provider, _router_with_echo(), _settings(),
        messages=[{"role": "user", "content": "hi"}], tier=TIER,
        on_event=events.append,
    )
    assert result.text == "answered by backup"
    notices = [e for e in events if e["type"] == "notice"]
    assert len(notices) == 1
    assert "backup model" in notices[0]["message"]


def test_no_notice_when_primary_serves():
    provider = FakeProvider([ProviderResponse(text="from primary")])
    events = []
    run_turn(
        provider, _router_with_echo(), _settings(),
        messages=[{"role": "user", "content": "hi"}], tier=TIER,
        on_event=events.append,
    )
    assert not any(e["type"] == "notice" for e in events)


def test_rendered_tool_image_is_persisted_and_returned():
    """A tool that renders an image → the harness persists it via image_sink and
    returns the ref in TurnResult.rendered_images (so the endpoint can attach it to
    the stored assistant turn). Base64 still never enters the model's text context."""
    from agentic_devops.tools.base import ToolImage, ToolResult

    router = ToolsRouter()
    router.register(ToolSpec(
        name="get_panel_image", category="grafana", description="render a panel",
        when_to_use="render", input_schema={"type": "object", "properties": {}},
        handler=lambda args: ToolResult(text="", images=[ToolImage(data="PNGBYTES", mime="image/png")]),
    ))
    stored = []

    def sink(im):
        stored.append((im.data, im.mime))
        return "hash-" + im.data  # pretend blob ref

    provider = FakeProvider([
        ProviderResponse(tool_calls=[ToolCall(id="c1", name="get_panel_image", arguments={})]),
        ProviderResponse(text="Here's the panel."),
    ])
    result = run_turn(
        provider, router, _settings(),
        messages=[{"role": "user", "content": "render it"}],
        tier=ModelTier(model="anthropic/claude-x"), image_sink=sink,
    )
    # persisted once, ref surfaced for the assistant turn
    assert stored == [("PNGBYTES", "image/png")]
    assert result.rendered_images == [{"ref": "hash-PNGBYTES", "mime": "image/png", "name": "get_panel_image"}]
    # base64 not in the stored finding (context stays clean)
    f = [x for x in result.tool_findings if x["tool"] == "get_panel_image"][0]
    assert "PNGBYTES" not in f["result"]
