"""Pluggable tracing for the agent loop.

Default is a local JSONL trace (no external dependency) — enough to debug slow
or inefficient loops, which was one of the most useful capabilities in practice
(see docs/JOURNEY.md). LangSmith is an opt-in upgrade.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

from agentic_devops.config import Settings


class Tracer(Protocol):
    def event(self, session_id: str, record: dict[str, Any]) -> None: ...


class NoopTracer:
    def event(self, session_id: str, record: dict[str, Any]) -> None:  # noqa: D401
        return None


class JsonlTracer:
    """Appends loop events to ``<trace_dir>/trace-<session>.jsonl``."""

    def __init__(self, trace_dir: Path) -> None:
        self._dir = trace_dir

    def event(self, session_id: str, record: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        line = {"ts": time.time(), "session": session_id, **record}
        with (self._dir / f"trace-{session_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")


def get_tracer(settings: Settings) -> Tracer:
    if settings.tracing == "jsonl":
        return JsonlTracer(settings.trace_dir)
    # "langsmith" integration is wired in a later step; fall back to no-op for now.
    return NoopTracer()
