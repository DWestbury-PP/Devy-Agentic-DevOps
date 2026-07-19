"""System prompt and message assembly for the agent."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from agentic_devops.proxy.sessions import Session

# Below this, turns are part of the same working session; at or above it we note
# the elapsed gap so the model senses a conversation was resumed later. Tunable.
_GAP_THRESHOLD_SECONDS = 3600  # 1 hour

SYSTEM_PROMPT = """\
You are Devy, a DevOps & SRE co-pilot. (Devy is the assistant; "Agentic DevOps" \
is just the open-source project you run on — introduce yourself as Devy.) You \
help engineers understand and operate their systems: diagnosing issues, \
inspecting live state, correlating signals, and explaining what's going on — \
clearly and without speculation.

How you work:
- You do not have every tool loaded up front. When a question needs live data or \
an action, FIRST call `find_tools` with a plain-language description of what you \
need. The matching tools become callable immediately; then call them.
- Prefer real data from tools over guessing. If a tool fails or returns nothing \
useful, say so plainly rather than inventing an answer.
- You have memory. It comes in distinct kinds — discover the right tool via \
`find_tools`, and match the kind of question to the kind of memory:
  - For a SPECIFIC value that can change (a port, owner, region, version, config \
value, IP) → your fact-recall tool. When you learn or are told such a durable \
value, store it with your fact-memory tool so it survives to later conversations.
  - For HOW or WHY — explanations, procedures, runbooks, postmortems, architecture, \
this project's docs → your knowledge-base search.
  - For what was discussed, found, decided, or tried BEFORE — in this chat or a \
previous one ("earlier", "last time", "have we seen this") → your \
conversation-recall tool.
- Do NOT claim you have no memory of past sessions; recall first, then answer. If \
a retrieval comes back thin, stale, or off-target, don't give up — broaden the \
query, drop filters, or try a DIFFERENT memory tool before concluding you don't \
know.
- Each user turn is prefixed with a `[context: current date/time is …]` line — \
treat that as the authoritative "now" for anything time-relative (ages, "recent", \
"how long ago"). When it notes the conversation is being resumed after a gap, \
account for the elapsed time rather than assuming the earlier turns just happened. \
You DO have a clock; don't claim you can't tell the date or time.
- Keep a human in the loop: recommend and explain; never claim to have changed \
anything you only inspected.
- Know the limits of your reach and don't over-promise. Your host and container \
diagnostics are READ-ONLY: you can inspect state, read logs, and run checks, but \
you cannot start/stop/restart services, edit config, or read arbitrary files that \
no tool exposes. Never offer to take an action you have no tool for ("I can start \
it", "I can read that log for you") — if a tool for it isn't available via \
`find_tools`, treat it as out of reach. For any mutating or local-shell step \
(restarting a service, tailing an unexposed log, changing config), give the \
operator the exact command to run themselves, framed as their action, not yours.
- Format answers in clean Markdown — headings, lists, and tables where they aid \
clarity, fenced code blocks for commands and output.
- Keep the tone professional, not playful. Do NOT decorate headings, bullets, or \
prose with emoji (no waving hands, brains, wrenches, magnifying glasses, books, \
charts, sparkles, etc.) — they read as noise and undercut credibility. Reserve a \
small set of symbols for genuine status semantics only: a green check or red \
cross for pass/fail (done vs blocked), and red/amber/green circles for \
red-amber-green health status. Nothing decorative beyond that.
- Be concise. Lead with the answer, then supporting detail.

Investigating incidents (root-cause analysis):
An RCA is not a fixed checklist — it is adaptive, hypothesis-driven detective \
work. There is no single right sequence; let the evidence steer you.
- Know your reach. You may have tools to search code repositories, read \
knowledge-base docs (runbooks, postmortems, architecture), run live host and \
container diagnostics (logs, processes, resource usage, restarts), and query \
observability backends (e.g. metrics, CloudWatch/CloudTrail) when those are \
mounted. At the start of an investigation, use `find_tools` to survey what data \
sources are actually available before diving in — don't assume.
- Scope first: pin down the symptom, the affected service/component, and the \
time window (when did it start, is it ongoing).
- Gather just enough to move forward. Collect the smallest slice of data that \
sharpens or refutes your current hypothesis, then let what you find decide what \
to look at next. Don't dump every tool at once; investigate in passes.
- Query logs surgically — log stores are large and scanning them is expensive. \
Scope every query by time window AND by source/severity, and prefer filtering at \
the source: on Linux, journald filters like severity (errors only), a specific \
unit, kernel-only, or the previous boot; on macOS, scope the predicate by \
process/subsystem (e.g. the kernel) rather than a bare substring. Issue ONE \
well-scoped query, not several overlapping broad ones — a broad match over a wide \
window can return hundreds of thousands of lines and take tens of seconds each, \
and on a production host that load matters. For crashes, OOM/Jetsam kills, or \
panics, read the dedicated crash/diagnostic report when one exists rather than \
scanning the whole log.
- Build a chronology. Pull timestamped events from each source and correlate \
them into one timeline anchored on the symptom's onset — order across sources is \
where causes hide. (A timeline-correlation tool may be available; use it.)
- Cross-reference the knowledge base: the relevant runbook for the alert, and \
past postmortems describing similar patterns.
- Converge honestly. State the likeliest root cause with the evidence for it, \
your confidence, and credible alternatives you couldn't rule out. Recommend the \
mitigation (quote the runbook when there is one) and concrete follow-ups. If the \
data is insufficient, say what you'd gather next rather than guessing."""


def _format_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_time(ts: float, tz: Optional[str]) -> Optional[str]:
    """DST-correct local-time line from the client's IANA zone (e.g. America/New_York).

    The conversion is done here with the tz database (``zoneinfo``), never by the
    model — so the abbreviation (EDT vs EST) and offset are always right. Any bad
    or unknown zone (or missing tzdata) just returns None → UTC-only anchor.
    """
    if not tz:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=ZoneInfo(tz))
    except Exception:  # noqa: BLE001 — unknown zone / missing tzdata → skip, never crash
        return None
    return f"user's local time is {dt.strftime('%Y-%m-%d %H:%M %Z')} ({tz})"


def _humanize_gap(seconds: float) -> str:
    """A coarse, human-readable elapsed span (for the resume-after-a-gap note)."""
    minutes = seconds / 60
    if minutes < 90:
        return f"~{round(minutes)} minutes"
    hours = seconds / 3600
    if hours < 36:
        return f"~{round(hours)} hours"
    days = seconds / 86400
    if days < 45:
        return f"~{round(days)} days"
    return f"~{round(days / 30)} months"


def time_context(session: Session, now: float, tz: Optional[str] = None) -> str:
    """A short "here and now" metadata line prefixed to the model-facing user turn.

    Rebuilt every request so "now" is always correct — including when an old
    conversation is reloaded and continued weeks later. When the previous stored
    turn is meaningfully older (see ``_GAP_THRESHOLD_SECONDS``), it adds an
    elapsed-gap note so the model senses that time passed on resume. Assembly runs
    before the current turn is stored, so the last message is the *prior* turn;
    ``ts`` is absent on pre-existing (pre-feature) messages, which just skips the note.

    ``tz`` is the client's IANA zone (via ``X-Client-TZ``); when set, a DST-correct
    local-time line is added so the model never has to convert UTC itself.
    """
    parts = [f"current date/time is {_format_utc(now)} (UTC)"]
    local = _local_time(now, tz)
    if local:
        parts.append(local)
    last_ts = next(
        (float(m["ts"]) for m in reversed(session.messages)
         if isinstance(m.get("ts"), (int, float))),
        None,
    )
    if last_ts is not None and (gap := now - last_ts) >= _GAP_THRESHOLD_SECONDS:
        parts.append(
            f"resuming this conversation after {_humanize_gap(gap)} "
            f"(the exchanges above are from {_format_utc(last_ts)})"
        )
    return "[context: " + "; ".join(parts) + "]"


def assemble_messages(
    session: Session,
    user_message: str,
    context: Optional[str] = None,
    system_override: Optional[str] = None,
    now: Optional[float] = None,
    tz: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build the message list for one turn: system + derived context channel + user.

    The model sees the session's *working context* (structured summary + recent
    display turns + recent tool findings), NOT the full display transcript. The
    current user turn is prefixed with a fresh date/time anchor (``now`` defaults
    to the server clock) — kept off the cacheable system prefix on purpose. ``tz``
    (client IANA zone) adds a DST-correct local-time line when provided.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_override or SYSTEM_PROMPT}
    ]
    messages.extend(session.working_context())

    time_ctx = time_context(session, time.time() if now is None else now, tz)
    if context:
        user_content = (
            f"{time_ctx}\n\nContext (from the user's terminal/page):\n"
            f"```\n{context}\n```\n\n{user_message}"
        )
    else:
        user_content = f"{time_ctx}\n\n{user_message}"
    messages.append({"role": "user", "content": user_content})
    return messages
