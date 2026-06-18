"""correlate_timeline — a deterministic chronology builder for investigations.

Not "the RCA tool" — RCA is adaptive reasoning the agent does. This is one
helper in the belt: hand it timestamped events gathered from any source (docker
logs, journal, a postmortem, an alert), and it normalizes the mixed timestamp
formats to UTC, sorts them, and renders one merged timeline with offsets from an
anchor (e.g. the symptom's onset). Correct cross-source ordering is where causes
hide, and doing it by hand is error-prone — so we make it honest and testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from agentic_devops.tools.base import ToolSpec


def _parse_time(raw: Any) -> Optional[datetime]:
    """Parse common timestamp shapes to an aware UTC datetime, or None."""
    if raw is None:
        return None
    # Numeric epoch (seconds or milliseconds), as number or digit-string.
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        val = float(raw)
        if val > 1e12:  # milliseconds
            val /= 1000.0
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # ISO-8601, including a trailing 'Z' and fractional seconds/offsets.
    iso = s.replace("Z", "+00:00")
    # datetime.fromisoformat handles nanoseconds poorly; clip to microseconds.
    if "." in iso:
        head, _, tail = iso.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                frac += ch
            else:
                rest = tail[i:]
                break
        iso = f"{head}.{frac[:6]}{rest}" if frac else head + rest
    try:
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _fmt_offset(delta_seconds: float) -> str:
    sign = "+" if delta_seconds >= 0 else "-"
    s = abs(int(round(delta_seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{sign}{h}h{m:02d}m"
    if m:
        return f"{sign}{m}m{sec:02d}s"
    return f"{sign}{sec}s"


def _handler(args: dict[str, Any]) -> str:
    events = args.get("events")
    if not isinstance(events, list) or not events:
        return "ERROR: 'events' must be a non-empty list of {time, source, message} objects."

    anchor_dt = _parse_time(args.get("anchor")) if args.get("anchor") else None

    parsed: list[tuple[datetime, dict]] = []
    unparsed: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        dt = _parse_time(ev.get("time"))
        (parsed.append((dt, ev)) if dt else unparsed.append(ev))

    if not parsed:
        return (
            "ERROR: none of the event timestamps could be parsed. Provide ISO-8601 "
            "(e.g. 2026-05-02T14:03:00Z) or epoch seconds/milliseconds."
        )

    parsed.sort(key=lambda p: p[0])
    if anchor_dt is None:
        anchor_dt = parsed[0][0]

    lines = [f"Correlated timeline ({len(parsed)} events, UTC; anchor = {anchor_dt.isoformat()}):", ""]
    for dt, ev in parsed:
        offset = _fmt_offset((dt - anchor_dt).total_seconds())
        source = str(ev.get("source", "?"))
        sev = str(ev.get("severity", "")).strip()
        sev_tag = f"[{sev.upper()}] " if sev else ""
        msg = str(ev.get("message", "")).strip()
        lines.append(f"{dt.isoformat()}  ({offset:>7})  {source:<14} {sev_tag}{msg}")

    if unparsed:
        lines.append("")
        lines.append(f"⚠ {len(unparsed)} event(s) had unparseable timestamps and were omitted:")
        for ev in unparsed[:10]:
            lines.append(f"  - time={ev.get('time')!r} source={ev.get('source')!r}")
    return "\n".join(lines)


def build_correlate_timeline_tool() -> ToolSpec:
    return ToolSpec(
        name="correlate_timeline",
        category="investigation",
        description=(
            "Merge timestamped events from multiple sources into one chronological "
            "timeline (normalized to UTC, sorted, with offsets from an anchor time). "
            "Use during an investigation after gathering events from logs, the "
            "knowledge base, alerts, etc., to see cross-source ordering."
        ),
        when_to_use=(
            "When building the chronology for a root-cause analysis: you have "
            "timestamped events from several sources (container logs, journal, a "
            "postmortem, an alert) and need them ordered together against the "
            "symptom's onset."
        ),
        use_cases=[
            "build an incident timeline",
            "correlate log lines with an alert time",
            "order events across services to find what happened first",
            "root cause analysis chronology",
        ],
        input_schema={
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "description": "Events to correlate.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "time": {
                                "type": "string",
                                "description": "Timestamp: ISO-8601 (e.g. 2026-05-02T14:03:00Z) or epoch seconds/ms.",
                            },
                            "source": {
                                "type": "string",
                                "description": "Where it came from, e.g. 'acme-checkout logs', 'postmortem', 'alert'.",
                            },
                            "message": {"type": "string", "description": "What happened."},
                            "severity": {
                                "type": "string",
                                "description": "Optional: info | warn | error | critical.",
                            },
                        },
                        "required": ["time", "message"],
                    },
                },
                "anchor": {
                    "type": "string",
                    "description": "Optional anchor time (e.g. the alert/symptom onset) to show offsets from. Defaults to the earliest event.",
                },
            },
            "required": ["events"],
        },
        handler=_handler,
        safety_tier="read-only",
    )
