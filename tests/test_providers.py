"""Provider normalization, exercised via the completion_fn seam (no litellm,
no network)."""

import json
from types import SimpleNamespace

import pytest

from agentic_devops.config import ModelTier
from agentic_devops.proxy.providers import ProviderClient
from agentic_devops.proxy.errors import ProviderError

TIER = ModelTier(model="fake/model", max_tokens=128, temperature=0.2, api_base="http://x")


def _billing_error():
    e = type("BadRequestError", (Exception,), {})("Your credit balance is too low")
    return e


def _bad_request():
    e = type("BadRequestError", (Exception,), {})("tools[0].name: invalid")
    e.status_code = 400
    return e


def _text(msg):
    return {"choices": [{"message": {"content": msg}}]}


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
    assert "timeout" not in captured  # no timeout configured → not passed


def test_request_timeout_passed_to_complete_and_stream():
    captured = {}

    def fake(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    client = ProviderClient(completion_fn=fake, request_timeout=90.0)
    client.complete([{"role": "user", "content": "hi"}], tier=TIER)
    assert captured["timeout"] == 90.0

    def fake_stream(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return iter(())  # no chunks; we only assert the kwargs

    client_s = ProviderClient(completion_fn=fake_stream, request_timeout=90.0)
    list(client_s.stream([{"role": "user", "content": "hi"}], tier=TIER))
    assert captured["timeout"] == 90.0 and captured["stream"] is True


# --- failover (tier.fallbacks) ---------------------------------------------

PRIMARY = ModelTier(model="anthropic/primary", max_tokens=128)
FALLBACK_TIER = ModelTier(
    model="anthropic/primary",
    max_tokens=128,
    fallbacks=[ModelTier(model="openai/backup", max_tokens=256)],
)


def test_complete_fails_over_to_backup_on_billing_error():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "anthropic/primary":
            raise _billing_error()
        return _text("from backup")

    resp = ProviderClient(completion_fn=fake).complete(
        [{"role": "user", "content": "hi"}], tier=FALLBACK_TIER
    )
    assert resp.text == "from backup"
    assert resp.served_by == "openai/backup"
    assert resp.fell_back is True
    assert calls == ["anthropic/primary", "openai/backup"]  # primary tried first


def test_complete_backup_uses_its_own_profile():
    captured = {}

    def fake(**kwargs):
        if kwargs["model"] == "anthropic/primary":
            raise _billing_error()
        captured.update(kwargs)
        return _text("ok")

    ProviderClient(completion_fn=fake).complete(
        [{"role": "user", "content": "hi"}], tier=FALLBACK_TIER
    )
    assert captured["max_tokens"] == 256  # the backup's max_tokens, not the primary's


def test_complete_does_not_fail_over_on_non_failoverable_error():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs["model"])
        raise _bad_request()  # malformed request → fails identically everywhere

    with pytest.raises(ProviderError) as ei:
        ProviderClient(completion_fn=fake).complete(
            [{"role": "user", "content": "hi"}], tier=FALLBACK_TIER
        )
    assert ei.value.category == "bad_request"
    assert calls == ["anthropic/primary"]  # backup NOT tried — no wasted call


def test_complete_raises_provider_error_when_all_fail():
    def fake(**kwargs):
        raise _billing_error()

    with pytest.raises(ProviderError) as ei:
        ProviderClient(completion_fn=fake).complete(
            [{"role": "user", "content": "hi"}], tier=FALLBACK_TIER
        )
    assert ei.value.category == "credit_exhausted"
    assert "backup model" in ei.value.user_message  # tried_backup suffix


def test_complete_no_fallback_configured_raises_friendly_error():
    def fake(**kwargs):
        raise _billing_error()

    with pytest.raises(ProviderError) as ei:
        ProviderClient(completion_fn=fake).complete(
            [{"role": "user", "content": "hi"}], tier=PRIMARY
        )
    assert ei.value.category == "credit_exhausted"
    assert "backup model" not in ei.value.user_message  # none was tried


def _stream_chunks(texts):
    for t in texts:
        yield {"choices": [{"delta": {"content": t}}]}


def test_stream_fails_over_before_any_delta():
    def fake(**kwargs):
        if kwargs["model"] == "anthropic/primary":
            raise _billing_error()  # fires before any chunk
        return _stream_chunks(["hi ", "there"])

    client = ProviderClient(completion_fn=fake)
    events = []
    resp = yield_from_stream(client, events)
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "hi there"
    assert resp.fell_back is True and resp.served_by == "openai/backup"


def test_stream_does_not_fail_over_after_a_delta_emitted():
    def fake(**kwargs):
        # Emit one chunk, THEN blow up — too late to fail over cleanly.
        def gen():
            yield {"choices": [{"delta": {"content": "partial"}}]}
            raise _billing_error()
        return gen()

    client = ProviderClient(completion_fn=fake)
    events = []
    with pytest.raises(ProviderError):
        list(_drain(client.stream([{"role": "user", "content": "hi"}], tier=FALLBACK_TIER), events))
    assert any(e["type"] == "delta" for e in events)  # the partial text did stream


def _drain(gen, events):
    """Iterate a stream generator, collecting yielded events; re-raise its return."""
    while True:
        try:
            events.append(next(gen))
        except StopIteration:
            return


def yield_from_stream(client, events):
    gen = client.stream([{"role": "user", "content": "hi"}], tier=FALLBACK_TIER)
    result = None
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            result = stop.value
            break
    return result
