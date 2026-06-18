"""correlate_timeline helper: timestamp parsing, ordering, anchoring."""

from agentic_devops.tools.builtin.timeline import build_correlate_timeline_tool

TOOL = build_correlate_timeline_tool()


def run(args):
    return TOOL.handler(args)


def test_merges_and_sorts_across_sources_and_formats():
    out = run(
        {
            "events": [
                {"time": "2026-05-02T14:05:00Z", "source": "checkout", "message": "errors spike"},
                {"time": 1777730400, "source": "alert", "message": "alert fired"},  # 2026-05-02T14:00:00Z
                {"time": "2026-05-02T14:03:00+00:00", "source": "risk", "message": "risk slow"},
            ]
        }
    )
    # Sorted ascending: alert (14:00) → risk (14:03) → checkout (14:05).
    i_alert = out.index("alert fired")
    i_risk = out.index("risk slow")
    i_checkout = out.index("errors spike")
    assert i_alert < i_risk < i_checkout
    assert "3 events" in out


def test_anchor_offsets():
    out = run(
        {
            "anchor": "2026-05-02T14:00:00Z",
            "events": [
                {"time": "2026-05-02T14:00:00Z", "source": "alert", "message": "fired"},
                {"time": "2026-05-02T14:03:00Z", "source": "log", "message": "later"},
            ],
        }
    )
    assert "+0s" in out          # anchor itself
    assert "+3m00s" in out       # three minutes after the anchor


def test_epoch_milliseconds_parsed():
    out = run({"events": [{"time": 1777730400000, "source": "x", "message": "ms epoch"}]})
    assert "2026-05-02T14:00:00+00:00" in out


def test_unparseable_reported_not_crashing():
    out = run(
        {
            "events": [
                {"time": "2026-05-02T14:00:00Z", "source": "ok", "message": "good"},
                {"time": "last tuesday", "source": "bad", "message": "vague"},
            ]
        }
    )
    assert "good" in out
    assert "unparseable" in out.lower()


def test_all_unparseable_errors():
    assert run({"events": [{"time": "soon", "message": "x"}]}).startswith("ERROR")


def test_empty_or_missing_events_errors():
    assert run({}).startswith("ERROR")
    assert run({"events": []}).startswith("ERROR")


def test_discoverable_metadata():
    assert TOOL.name == "correlate_timeline"
    assert TOOL.category == "investigation"
    assert "events" in TOOL.input_schema["required"]
