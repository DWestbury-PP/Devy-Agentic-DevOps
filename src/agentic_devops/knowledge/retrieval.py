"""Retrieval as a tool — not a bolted-on pre-step.

``search_knowledge`` is registered into the tools-router like any other tool, so
``find_tools`` surfaces it by intent and the harness only reaches for it when a
query actually needs the knowledge base. Results carry source citations
(``corpus / path # heading``) so the agent can attribute its answer instead of
laundering retrieved text as its own.
"""

from __future__ import annotations

from typing import Any

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.store import VectorStore
from agentic_devops.tools.base import ToolSpec

_MAX_K = 10
_SNIPPET_CHARS = 1200


def _format_hits(hits: list, query: str) -> str:
    if not hits:
        return f"No knowledge-base entries matched {query!r}."
    blocks: list[str] = []
    for i, hit in enumerate(hits, 1):
        c = hit.chunk
        cite = f"{c.corpus} / {c.source_path}"
        if c.heading_path:
            cite += f" # {c.heading_path}"
        meta = getattr(c, "metadata", None) or {}
        if meta.get("doc_type") and meta["doc_type"] != "doc":
            cite += f"  ({meta['doc_type']})"
        matched = "+".join(getattr(hit, "sources", ()) or ("vector",))
        snippet = c.text if len(c.text) <= _SNIPPET_CHARS else c.text[:_SNIPPET_CHARS] + " …"
        blocks.append(f"[{i}] {cite}  (matched: {matched})\n{snippet}")
    return "\n\n".join(blocks)


def build_search_knowledge_tool(
    store: VectorStore, embedder: Embedder, default_k: int = 5
) -> ToolSpec:
    """Construct the ``search_knowledge`` ToolSpec bound to a store + embedder.

    Coverage (which corpora exist, how many chunks) is read **live** on each call,
    not snapshotted at registration — so documents added/removed through the
    control plane are reflected immediately, with no stale counts.
    """

    def handler(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: 'query' is required."
        corpus = args.get("corpus") or None
        try:
            k = int(args.get("k", default_k))
        except (TypeError, ValueError):
            k = default_k
        k = max(1, min(k, _MAX_K))

        try:
            qvec = embedder.embed_query(query)
        except Exception as exc:  # noqa: BLE001 — surface embedder/key errors to the agent
            return f"ERROR: embedding the query failed: {exc}"
        # Hybrid: semantic (vector) fused with exact-token (full-text) matching.
        hits = store.hybrid_search(query, qvec, k=k, corpus=corpus)
        if not hits:
            avail = ", ".join(f"{n} ({c})" for n, c in store.corpora().items())
            msg = f"No knowledge-base entries matched {query!r}."
            return f"{msg} Available corpora: {avail}." if avail else f"{msg} The knowledge base is empty."
        return _format_hits(hits, query)

    return ToolSpec(
        name="search_knowledge",
        category="knowledge",
        description=(
            "Search the indexed knowledge base (runbooks, docs, postmortems, repo "
            "documentation) for passages relevant to a question, returning the top "
            "matches with source citations. Hybrid search — semantic similarity "
            "fused with exact-keyword matching, so error codes, hostnames, and flags "
            "are found alongside paraphrases. Use the cited text to ground your answer."
        ),
        when_to_use=(
            "When a question may be answered by ingested documentation rather than "
            "general knowledge: runbooks, on-call playbooks, architecture, postmortems, "
            "or this project's own docs. Prefer this over guessing project- or "
            "org-specific facts."
        ),
        use_cases=[
            "what's the runbook for this alert",
            "how do we mitigate checkout latency",
            "find the last database failover postmortem",
            "what does this project's documentation say",
            "on-call escalation policy",
        ],
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The question or topic to search for, in natural language.",
                },
                "corpus": {
                    "type": "string",
                    "description": (
                        "Optional corpus name to restrict the search to. Omit to search all "
                        "corpora (recommended unless you know the exact corpus). The set of "
                        "corpora is dynamic; a missing match lists what's available."
                    ),
                },
                "k": {
                    "type": "integer",
                    "description": f"Number of passages to return (default {default_k}, max {_MAX_K}).",
                },
            },
            "required": ["query"],
        },
        handler=handler,
        safety_tier="read-only",
    )
