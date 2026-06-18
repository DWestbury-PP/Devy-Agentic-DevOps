"""Provider normalization, exercised via the completion_fn seam (no litellm,
no network)."""

import json
from types import SimpleNamespace

from agentic_devops.config import ModelTier
from agentic_devops.proxy.providers import ProviderClient

TIER = ModelTier(model="fake/model", max_tokens=128, temperature=0.2, api_base="http://x")


def test_normalizes_plain_text_from_dict_shape():
    raw = {"choices": [{"message": {"content": "hello", "tool_calls": None}}], "usage": {"total_tokens": 5}}
    client = ProviderClient(completion_fn=lambda **kw: raw)
    resp = client.complete([{"role": "user", "content": "hi"}], tier=TIER)
    assert resp.text == "hello"
    assert not resp.wants_tools
    assert resp.usage["total_tokens"] == 5


def test_normalizes_tool_calls_from_object_shape():
    message = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="abc",
                function=SimpleNamespace(name="host_diagnostics", arguments=json.dumps({"check": "disk"})),
            )
        ],
    )
    raw = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
    client = ProviderClient(completion_fn=lambda **kw: raw)
    resp = client.complete([{"role": "user", "content": "hi"}], tier=TIER)
    assert resp.wants_tools
    assert resp.tool_calls[0].name == "host_diagnostics"
    assert resp.tool_calls[0].arguments == {"check": "disk"}


def test_completion_fn_receives_tier_settings():
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    ProviderClient(completion_fn=fake).complete([{"role": "user", "content": "hi"}], tier=TIER)
    assert captured["model"] == "fake/model"
    assert captured["max_tokens"] == 128
    assert captured["temperature"] == 0.2
    assert captured["api_base"] == "http://x"
