"""``recall_facts`` + ``memory_add`` — the evolving fact tier's tool seams (Phase A).

These are the read/write seams onto Knowledge Memory's structured-fact tier
(`FactStore`). Like ``search_knowledge`` and ``recall_history`` they are
find_tools-discovered, not per-turn pre-steps. They share ``category="knowledge"``
with ``search_knowledge`` so a category-scoped discovery returns the whole durable
knowledge surface as a set — while their ``when_to_use`` is carved to be
unambiguous against prose search (how/why) and conversation recall (what we said).

``memory_add`` is the write-back seam: Devy deposits a durable fact it (or the
user) decides is worth keeping. It is ``wants_context=True`` for **provenance
only** — the depositing user/session are stamped into ``source``/``metadata``; the
agent never supplies them, and they do NOT scope reads (knowledge memory is shared
across conversations, unlike user-scoped conversation history).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from agentic_devops.knowledge.facts import FactStore
from agentic_devops.tools.base import ToolSpec

_MAX_K = 10
_SNIPPET_CHARS = 600


def _parse_as_of(raw: Any) -> Optional[datetime]:
    """Parse an ISO-8601 date/datetime; attach UTC if naive. None on failure."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_hits(hits: list, query: str, as_of: Optional[datetime]) -> str:
    if not hits:
        when = f" as of {as_of.date().isoformat()}" if as_of else ""
        return f"No facts matched {query!r}{when}."
    blocks: list[str] = []
    for i, h in enumerate(hits, 1):
        f = h.fact
        slot = f"{f.subject} · {f.attribute}" if f.subject and f.attribute else (f.subject or "(slotless)")
        validity = "current" if f.is_current else f"valid {f.valid_from[:10]} – {(f.valid_to or '')[:10]}"
        matched = "+".join(h.sources or ("vector",))
        text = f.content if len(f.content) <= _SNIPPET_CHARS else f.content[:_SNIPPET_CHARS] + " …"
        cite = f"{slot}  [{validity}; source {f.source}; matched {matched}]"
        blocks.append(f"[{i}] {cite}\n{text}")
    return "\n\n".join(blocks)


def build_recall_facts_tool(store: FactStore) -> ToolSpec:
    """Construct the ``recall_facts`` ToolSpec bound to a fact store."""

    def handler(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: 'query' is required."
        try:
            k = int(args.get("k", 5))
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, _MAX_K))
        subject = (args.get("subject") or None)
        as_of = _parse_as_of(args.get("as_of"))
        if args.get("as_of") and as_of is None:
            return f"ERROR: could not parse as_of {args['as_of']!r} (use ISO-8601, e.g. 2026-03-01)."
        try:
            hits = store.search_facts(query, k=k, as_of=as_of, subject=subject)
        except Exception as exc:  # noqa: BLE001 — surface embedder/DB errors to the agent
            return f"ERROR: fact search failed: {exc}"
        return _format_hits(hits, query, as_of)

    return ToolSpec(
        name="recall_facts",
        category="knowledge",
        description=(
            "Look up durable, structured FACTS about systems/services/hosts — "
            "precise current (or historical) values that change over time: ports, "
            "endpoints, owners, versions, IPs, configuration. Facts are bi-temporal: "
            "by default you get what's true now; pass `as_of` to see what was true at "
            "a past date. Returns each fact with its subject/attribute, validity "
            "window, and provenance."
        ),
        when_to_use=(
            "When you need a SPECIFIC value rather than an explanation — 'what port "
            "does pricing expose', 'who owns the orders service', 'what region is X "
            "in', 'what did that used to be'. Prefer this over search_knowledge "
            "(which returns prose/how-to) when the answer is a discrete fact, and "
            "over guessing remembered values."
        ),
        use_cases=[
            "what port does the pricing service expose",
            "who owns the orders pipeline",
            "what region does this host run in",
            "what was the value before it changed (as_of)",
            "current config value for a service",
        ],
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What fact to look up, in natural language.",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional exact subject to scope to (e.g. 'svc:pricing', "
                    "'host:edge-gw-01'). Omit to search across all subjects.",
                },
                "as_of": {
                    "type": "string",
                    "description": "Optional ISO-8601 date/datetime to reconstruct the fact "
                    "believed at that instant (e.g. '2026-03-01'). Omit for current facts.",
                },
                "k": {
                    "type": "integer",
                    "description": f"Number of facts to return (default 5, max {_MAX_K}).",
                },
            },
            "required": ["query"],
        },
        handler=handler,
        safety_tier="read-only",
    )


def build_memory_add_tool(store: FactStore) -> ToolSpec:
    """Construct the ``memory_add`` write-back ToolSpec bound to a fact store."""

    def handler(args: dict[str, Any], context: dict[str, Any]) -> str:
        content = str(args.get("content", "")).strip()
        if not content:
            return "ERROR: 'content' is required (the fact to remember)."
        subject = (args.get("subject") or None)
        attribute = (args.get("attribute") or None)
        kind = str(args.get("kind", "semantic")).lower()
        if kind not in ("semantic", "episodic"):
            kind = "semantic"
        try:
            importance = float(args.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5

        # Provenance from the request context (never model-supplied).
        user_id = context.get("user_id")
        session_id = context.get("session_id")
        source = user_id or (f"session:{session_id}" if session_id else "conversation")
        metadata = {k: v for k, v in (("user_id", user_id), ("session_id", session_id)) if v}

        try:
            result = store.add_fact(
                content, kind=kind, source=source, subject=subject,
                attribute=attribute, importance=importance, metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: could not store the fact: {exc}"

        msg = f"Stored fact {result.memory_id[:8]}"
        if subject and attribute:
            msg += f" for {subject} · {attribute}"
            if result.superseded:
                msg += f" (superseded {len(result.superseded)} prior fact"
                msg += "s)" if len(result.superseded) > 1 else ")"
            else:
                msg += " (new slot)"
        else:
            msg += " (slotless — coexists with related facts)"
        return msg + "."

    return ToolSpec(
        name="memory_add",
        category="knowledge",
        description=(
            "Remember a durable FACT for future conversations — a discrete piece of "
            "knowledge worth keeping (a service's port, an owner, a config value, a "
            "decision's rationale). If you give a `subject` and `attribute`, the fact "
            "occupies a contradiction slot: a later fact for the same slot supersedes "
            "this one, with history preserved (use this for values that change). "
            "Without them, the fact simply coexists. This writes to shared knowledge "
            "memory, not to one conversation."
        ),
        when_to_use=(
            "When you learn (or the user states) something durable and reusable that "
            "should survive beyond this conversation — especially a specific value "
            "that may change later and that you'd want to look up with recall_facts. "
            "Do NOT use it for transient chit-chat or this-conversation-only context."
        ),
        use_cases=[
            "remember that pricing now exposes port 9090",
            "record who owns a service",
            "note a configuration value for later",
            "remember a decision and why it was made",
            "store a host's region or role",
        ],
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact to remember, as a clear standalone sentence.",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional entity the fact is about (e.g. 'svc:pricing', "
                    "'host:edge-gw-01'). Pair with `attribute` to form a contradiction slot "
                    "so future updates supersede this fact instead of duplicating it.",
                },
                "attribute": {
                    "type": "string",
                    "description": "Optional attribute being asserted (e.g. 'port', 'owner', "
                    "'region'). Only forms a slot when given together with `subject`.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["semantic", "episodic"],
                    "description": "'semantic' (a durable fact, default) or 'episodic' (a "
                    "specific event/observation).",
                },
                "importance": {
                    "type": "number",
                    "description": "Optional 0–1 salience hint (default 0.5).",
                },
            },
            "required": ["content"],
        },
        handler=handler,
        safety_tier="read-only",
        wants_context=True,
    )
