"""``recall_history`` — retrieval over the user's past conversations (Phase 8).

Like ``search_knowledge``, this is a find_tools-discovered tool, not a per-turn
pre-step. Devy calls it when a question hinges on something said earlier (this
conversation) or in a prior one ("did we see this incident before?"). It is
request-scoped: the user/session come from the tool *context*, never from the
model's arguments (so the agent can't read another user's history).
"""

from __future__ import annotations

from typing import Any

from agentic_devops.knowledge.history import ConversationMemoryStore
from agentic_devops.tools.base import ToolSpec

_MAX_K = 10
_SNIPPET_CHARS = 600


def _format(hits: list, query: str) -> str:
    if not hits:
        return f"No earlier conversation matched {query!r}."
    blocks: list[str] = []
    for i, h in enumerate(hits, 1):
        when = (h.created_at or "")[:10]
        cite = f"conversation {h.session_id[:8]} · turn {h.turn}" + (f" · {when}" if when else "")
        snippet = h.text if len(h.text) <= _SNIPPET_CHARS else h.text[:_SNIPPET_CHARS] + " …"
        blocks.append(f"[{i}] {cite}  (score {h.score:.2f})\n{snippet}")
    return "\n\n".join(blocks)


def build_recall_history_tool(store: ConversationMemoryStore) -> ToolSpec:
    """Construct the ``recall_history`` ToolSpec bound to a memory store."""

    def handler(args: dict[str, Any], context: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: 'query' is required."
        try:
            k = int(args.get("k", 5))
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, _MAX_K))
        scope = str(args.get("scope", "all")).lower()

        user_id = context.get("user_id")
        session_id = context.get("session_id")

        if scope == "this":
            if not session_id:
                return "No current conversation to search yet."
            hits = store.search(query, session_id=session_id, k=k)
        elif user_id:
            hits = store.search(query, user_id=user_id, k=k)
        elif session_id:
            # Anonymous (no identity): cross-conversation recall isn't available;
            # fall back to this conversation only.
            hits = store.search(query, session_id=session_id, k=k)
        else:
            return "Conversation recall is unavailable without an identity for this session."
        return _format(hits, query)

    return ToolSpec(
        name="recall_history",
        category="memory",
        description=(
            "Search this user's earlier conversations (and earlier in the current "
            "one) for relevant prior context — what was discussed, found, decided, "
            "or tried. Returns the most relevant past exchanges with citations."
        ),
        when_to_use=(
            "When the current question depends on something established earlier that "
            "may have scrolled out of context, or when checking whether a similar "
            "issue/incident was investigated before. Prefer this over guessing at "
            "past conversation details."
        ),
        use_cases=[
            "what did we conclude about this last time",
            "have we seen this error before",
            "recall the host/values from earlier in this chat",
            "did we already try restarting that service",
            "what was the root cause of the previous incident",
        ],
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to recall, in natural language.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "this"],
                    "description": "'all' = across the user's conversations (default); "
                    "'this' = only the current conversation.",
                },
                "k": {
                    "type": "integer",
                    "description": f"Number of past exchanges to return (default 5, max {_MAX_K}).",
                },
            },
            "required": ["query"],
        },
        handler=handler,
        safety_tier="read-only",
        wants_context=True,
    )
