"""Time/date metadata injection: the 'now' anchor + resume-after-a-gap note.

Hermetic — constructs a Session directly and passes an explicit ``now`` so the
assertions don't depend on the wall clock.
"""

from datetime import datetime, timezone

from agentic_devops.proxy.prompts import (
    _GAP_THRESHOLD_SECONDS,
    assemble_messages,
    time_context,
)
from agentic_devops.proxy.sessions import Session

NOW = 1_750_000_000.0  # a fixed reference instant (2025-06-15T15:06:40Z)


def _utc_epoch(y, mo, d, h):
    return datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()


def _session(*ts_offsets):
    """A session whose stored turns sit ``offset`` seconds before NOW."""
    s = Session(id="t")
    for i, off in enumerate(ts_offsets):
        role = "user" if i % 2 == 0 else "assistant"
        s.messages.append({"role": role, "content": f"m{i}", "ts": NOW - off})
    return s


def test_anchor_always_present_and_utc():
    ctx = time_context(Session(id="t"), NOW)
    assert ctx == "[context: current date/time is 2025-06-15T15:06:40Z (UTC)]"


def test_no_gap_note_for_fresh_session():
    assert "resuming" not in time_context(Session(id="t"), NOW)


def test_no_gap_note_when_last_turn_is_recent():
    # previous turn 5 minutes ago — same working session, no note
    ctx = time_context(_session(300), NOW)
    assert "current date/time is" in ctx and "resuming" not in ctx


def test_gap_note_appears_after_threshold():
    # previous turn ~26 days ago — a reloaded historical conversation
    ctx = time_context(_session(26 * 86400), NOW)
    assert "resuming this conversation after ~26 days" in ctx
    assert "the exchanges above are from 2025-05-20" in ctx


def test_gap_uses_most_recent_stored_turn():
    # older user turn + newer assistant turn: the note keys off the newest ts
    ctx = time_context(_session(40 * 86400, 2 * 3600), NOW)
    assert "~2 hours" in ctx


def test_pre_feature_messages_without_ts_skip_the_note():
    s = Session(id="t")
    s.messages.append({"role": "user", "content": "old"})  # no ts (legacy)
    s.messages.append({"role": "assistant", "content": "older"})
    ctx = time_context(s, NOW)
    assert "current date/time is" in ctx and "resuming" not in ctx


def test_threshold_boundary():
    just_under = time_context(_session(_GAP_THRESHOLD_SECONDS - 1), NOW)
    at = time_context(_session(_GAP_THRESHOLD_SECONDS), NOW)
    assert "resuming" not in just_under and "resuming" in at


def test_assemble_prefixes_user_turn_without_touching_raw_text():
    s = Session(id="t")
    msgs = assemble_messages(s, "why is the pod crashing?", now=NOW)
    user = msgs[-1]
    assert user["role"] == "user"
    assert user["content"].startswith("[context: current date/time is 2025-06-15T15:06:40Z (UTC)]")
    assert user["content"].endswith("why is the pod crashing?")
    # the volatile timestamp value never lands in the cacheable system prefix
    assert msgs[0]["role"] == "system" and "2025-06-15T15:06:40Z" not in msgs[0]["content"]


def test_assemble_keeps_terminal_context_block():
    s = Session(id="t")
    msgs = assemble_messages(s, "explain this", context="$ df -h\n/ 98%", now=NOW)
    content = msgs[-1]["content"]
    assert content.startswith("[context: current date/time is")
    assert "Context (from the user's terminal/page):" in content
    assert "df -h" in content and content.endswith("explain this")


def test_assemble_current_turn_attachments_become_image_url_parts():
    s = Session(id="t")
    msgs = assemble_messages(
        s, "what does this panel show?", now=NOW,
        attachments=[{"mime": "image/png", "data": "BASE64PNG"}],
    )
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    # text part carries the time anchor + the raw question
    assert content[0]["type"] == "text" and content[0]["text"].endswith("what does this panel show?")
    # image part is a data-URI the provider forwards to a vision model
    assert content[1] == {"type": "image_url",
                          "image_url": {"url": "data:image/png;base64,BASE64PNG"}}


def test_past_turn_images_flatten_to_placeholder_not_pixels():
    # A stored user turn that carried an image → working_context sends TEXT only
    # (the process-once invariant: past images are never re-sent as pixels).
    s = Session(id="t")
    s.add_user([{"type": "text", "text": "look at this"},
                {"type": "image_ref", "ref": "a" * 64, "mime": "image/png", "name": "panel.png"}])
    s.add_assistant("It shows 10%.")
    ctx = s.working_context()
    user_msg = next(m for m in ctx if m["role"] == "user")
    assert isinstance(user_msg["content"], str)
    assert "look at this" in user_msg["content"] and "panel.png" in user_msg["content"]
    assert "a" * 64 not in user_msg["content"]  # the ref/hash isn't leaked as content
    # no image_url parts anywhere in the working context
    assert all(not isinstance(m.get("content"), list) for m in ctx)


def test_no_local_line_without_tz():
    assert "local time" not in time_context(Session(id="t"), NOW)


def test_local_line_present_with_tz():
    ctx = time_context(Session(id="t"), NOW, tz="America/New_York")
    assert "current date/time is" in ctx  # UTC anchor still there
    assert "user's local time is" in ctx and "(America/New_York)" in ctx


def test_dst_summer_is_edt():
    # 2025-07-01 12:00Z → New York is on daylight time (EDT), UTC-4 → 08:00
    ctx = time_context(Session(id="t"), _utc_epoch(2025, 7, 1, 12), tz="America/New_York")
    assert "2025-07-01 08:00 EDT" in ctx


def test_dst_winter_is_est():
    # 2025-01-01 12:00Z → New York is on standard time (EST), UTC-5 → 07:00
    ctx = time_context(Session(id="t"), _utc_epoch(2025, 1, 1, 12), tz="America/New_York")
    assert "2025-01-01 07:00 EST" in ctx


def test_invalid_tz_falls_back_to_utc_only():
    ctx = time_context(Session(id="t"), NOW, tz="Not/AZone")
    assert "current date/time is 2025-06-15T15:06:40Z (UTC)" in ctx
    assert "local time" not in ctx


def test_assemble_passes_tz_through():
    msgs = assemble_messages(Session(id="t"), "hi", now=NOW, tz="Europe/London")
    assert "(Europe/London)" in msgs[-1]["content"]


def test_working_context_strips_ts_from_model_payload():
    s = _session(10, 5)  # one full exchange
    for m in s.working_context():
        assert "ts" not in m  # provider must never see the display-only annotation
        assert m["role"] in ("system", "user", "assistant")
