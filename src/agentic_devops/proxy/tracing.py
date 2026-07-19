"""Pluggable tracing for the agent loop.

Default is a local JSONL trace (no external dependency) — enough to debug slow
or inefficient loops, which was one of the most useful capabilities in practice
(see docs/JOURNEY.md). **LangSmith** is the opt-in upgrade: a real *waterfall* of
each turn → its LLM calls → its tool calls, built with the LangSmith SDK's
``RunTree`` (explicit parent→child, so it survives our threadpool / SSE-worker
model without relying on contextvars).

Two orthogonal things live on a tracer:
  * ``event(session_id, record)`` — the flat event stream (what the JSONL tracer writes).
  * ``turn(...)`` — a span tree (what the LangSmith tracer builds). Non-LangSmith
    tracers return a no-op span, so the harness can always call it with zero cost.

Payload verbosity (``full`` vs ``metadata``) follows ``DEVY_MODE`` unless the
operator pins ``langsmith.capture`` — see :class:`LangSmithConfig`. In ``metadata``
mode no prompt/completion/tool bodies leave the process; only span names, timings,
success, and token usage do.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Protocol

from agentic_devops.config import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Span API (no-op by default; LangSmith gives it teeth)
# ---------------------------------------------------------------------------
class Span(Protocol):
    def __enter__(self) -> "Span": ...
    def __exit__(self, *exc: Any) -> bool: ...
    def llm(self, name: str, inputs: dict[str, Any]) -> "Span": ...
    def tool(self, name: str, inputs: dict[str, Any]) -> "Span": ...
    def outputs(
        self,
        body: Optional[dict[str, Any]] = None,
        *,
        ok: Optional[bool] = None,
        usage: Optional[dict[str, Any]] = None,
        meta: Optional[dict[str, Any]] = None,
        name: Optional[str] = None,
    ) -> None: ...


class _NoopSpan:
    """Does nothing; returned everywhere when tracing isn't LangSmith."""

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def llm(self, name: str, inputs: dict[str, Any]) -> "_NoopSpan":
        return self

    def tool(self, name: str, inputs: dict[str, Any]) -> "_NoopSpan":
        return self

    def outputs(self, body=None, *, ok=None, usage=None, meta=None, name=None) -> None:
        return None


NOOP_SPAN = _NoopSpan()


class Tracer(Protocol):
    def event(self, session_id: str, record: dict[str, Any]) -> None: ...
    def turn(self, session_id: str, name: str, inputs: dict[str, Any]) -> Span: ...


class NoopTracer:
    def event(self, session_id: str, record: dict[str, Any]) -> None:  # noqa: D401
        return None

    def turn(self, session_id: str, name: str, inputs: dict[str, Any]) -> Span:
        return NOOP_SPAN


class JsonlTracer:
    """Appends loop events to ``<trace_dir>/trace-<session>.jsonl``."""

    def __init__(self, trace_dir: Path) -> None:
        self._dir = trace_dir

    def event(self, session_id: str, record: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        line = {"ts": time.time(), "session": session_id, **record}
        with (self._dir / f"trace-{session_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")

    def turn(self, session_id: str, name: str, inputs: dict[str, Any]) -> Span:
        return NOOP_SPAN


# ---------------------------------------------------------------------------
# LangSmith
# ---------------------------------------------------------------------------
class _LangSmithSpan:
    """Wraps a LangSmith ``RunTree`` node. Every SDK call is best-effort — tracing
    must never crash a turn — so failures degrade to a no-op child, not an error.
    """

    def __init__(self, run: Any, full: bool) -> None:
        self._run = run          # a RunTree, or None once finished / on failure
        self._full = full
        self._outputs: dict[str, Any] = {}
        self._error: Optional[str] = None

    def __enter__(self) -> "_LangSmithSpan":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._finish(repr(exc) if exc is not None else None)
        return False

    def _finish(self, error: Optional[str]) -> None:
        if self._run is None:
            return
        try:
            self._run.end(outputs=self._outputs or None, error=error or self._error)
            self._run.patch()
        except Exception:  # noqa: BLE001 — never let tracing break the loop
            logger.debug("langsmith: failed to close span", exc_info=True)
        self._run = None

    def _child(self, name: str, run_type: str, inputs: dict[str, Any]) -> Span:
        if self._run is None:
            return NOOP_SPAN
        try:
            child = self._run.create_child(
                name=name, run_type=run_type, inputs=self._filter(inputs)
            )
            child.post()
            return _LangSmithSpan(child, self._full)
        except Exception:  # noqa: BLE001
            logger.debug("langsmith: failed to create child span", exc_info=True)
            return NOOP_SPAN

    def _filter(self, data: Optional[dict[str, Any]]) -> dict[str, Any]:
        # Bodies (prompts/completions/args/results) only leave the process in full mode.
        return dict(data or {}) if self._full else {}

    def llm(self, name: str, inputs: dict[str, Any]) -> Span:
        return self._child(name, "llm", inputs)

    def tool(self, name: str, inputs: dict[str, Any]) -> Span:
        return self._child(name, "tool", inputs)

    def outputs(self, body=None, *, ok=None, usage=None, meta=None, name=None) -> None:
        if self._run is None:
            return
        if name:  # non-sensitive (e.g. the concrete model that served) — rename the span
            try:
                self._run.name = name           # picked up by patch()'s update_run(name=...)
            except Exception:  # noqa: BLE001
                pass
        if self._full and body:
            self._outputs.update(body)          # sensitive bodies: full mode only
        if meta:
            self._outputs.update(meta)           # non-sensitive (names/counts): both modes
        if ok is not None:
            self._outputs["ok"] = ok
        if usage:
            try:                                 # token usage is non-sensitive → always
                self._run.extra.setdefault("metadata", {})["usage"] = usage
            except Exception:  # noqa: BLE001
                pass


class LangSmithTracer:
    def __init__(self, client: Any, project: str, capture: str) -> None:
        self._client = client
        self._project = project
        self._full = capture == "full"

    def event(self, session_id: str, record: dict[str, Any]) -> None:
        # Flat events aren't used for LangSmith; the span tree carries everything.
        return None

    def turn(self, session_id: str, name: str, inputs: dict[str, Any]) -> Span:
        try:
            from langsmith.run_trees import RunTree

            rt = RunTree(
                name=name,
                run_type="chain",
                inputs=dict(inputs or {}) if self._full else {},
                project_name=self._project,
                client=self._client,
                extra={"metadata": {
                    "session_id": session_id,
                    "capture": "full" if self._full else "metadata",
                }},
            )
            rt.post()
            return _LangSmithSpan(rt, self._full)
        except Exception:  # noqa: BLE001
            logger.debug("langsmith: failed to open turn span", exc_info=True)
            return NOOP_SPAN


def _build_langsmith(settings: Settings) -> Optional[Tracer]:
    key = os.environ.get("LANGSMITH_API_KEY")
    if not key:
        logger.warning(
            "tracing=langsmith but LANGSMITH_API_KEY is unset — set it on the admin "
            "Secrets tab (devy/provider/langsmith). Falling back to jsonl."
        )
        return None
    try:
        from langsmith import Client
    except ImportError:
        logger.warning(
            "tracing=langsmith but the 'langsmith' package isn't installed "
            "(pip install '.[langsmith]'). Falling back to jsonl."
        )
        return None
    cfg = settings.langsmith
    capture = cfg.capture or ("full" if settings.secrets.mode == "dev" else "metadata")
    endpoint = os.environ.get("LANGSMITH_ENDPOINT") or cfg.endpoint
    project = os.environ.get("LANGSMITH_PROJECT") or cfg.project
    try:
        client = Client(api_key=key, api_url=endpoint)
    except Exception:  # noqa: BLE001
        logger.warning("tracing=langsmith but the client failed to init; falling back to jsonl",
                       exc_info=True)
        return None
    logger.info("LangSmith tracing enabled (project=%s, capture=%s)", project, capture)
    return LangSmithTracer(client, project, capture)


def get_tracer(settings: Settings) -> Tracer:
    if settings.tracing == "langsmith":
        tracer = _build_langsmith(settings)
        if tracer is not None:
            return tracer
        return JsonlTracer(settings.trace_dir)  # graceful fallback
    if settings.tracing == "none":
        return NoopTracer()
    return JsonlTracer(settings.trace_dir)
