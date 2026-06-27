"""``memory_index`` — an orientation map over Devy's durable knowledge (Phase B).

The third "never confused" mechanism from the memory architecture: before
choosing *what* to search, Devy can ask *what exists*. This returns a compact map
of the knowledge surface — the prose corpora and how many chunks each has, the
frontmatter facets you can filter by (doc/OKF types, tags), and the subjects the
fact tier currently knows about. It reads coverage live, so it never goes stale.

It does not retrieve content — it tells Devy where to point `search_knowledge`
(and which `filter` values are valid) or `recall_facts`.
"""

from __future__ import annotations

from typing import Any, Optional

from agentic_devops.knowledge.facts import FactStore
from agentic_devops.knowledge.store import VectorStore
from agentic_devops.tools.base import ToolSpec

_MAX_FACETS = 40


def _format(corpora: dict, facets: dict, subjects: list, fact_count: int) -> str:
    lines: list[str] = []
    if corpora:
        total = sum(corpora.values())
        lines.append(f"Knowledge base — {total} chunks across {len(corpora)} corpora:")
        for name, n in corpora.items():
            lines.append(f"  - {name} ({n})")
    else:
        lines.append("Knowledge base: empty (no documents ingested yet).")

    types = facets.get("doc_types") or []
    tags = facets.get("tags") or []
    if types:
        lines.append("Filterable doc types: " + ", ".join(types[:_MAX_FACETS]))
    if tags:
        shown = ", ".join(tags[:_MAX_FACETS])
        more = f" (+{len(tags) - _MAX_FACETS} more)" if len(tags) > _MAX_FACETS else ""
        lines.append(f"Tags: {shown}{more}")

    if fact_count:
        lines.append(f"\nFact tier — {fact_count} current facts.")
        if subjects:
            shown = ", ".join(subjects[:_MAX_FACETS])
            more = f" (+{len(subjects) - _MAX_FACETS} more)" if len(subjects) > _MAX_FACETS else ""
            lines.append(f"Known subjects: {shown}{more}")
    else:
        lines.append("\nFact tier: no facts stored yet.")

    lines.append(
        "\nUse search_knowledge (optionally with a `filter` on the types/tags above) "
        "for prose, or recall_facts (optionally scoped to a subject) for specific values."
    )
    return "\n".join(lines)


def build_memory_index_tool(
    store: VectorStore, fact_store: Optional[FactStore] = None
) -> ToolSpec:
    """Construct the ``memory_index`` orientation ToolSpec."""

    def handler(args: dict[str, Any]) -> str:
        try:
            corpora = store.corpora()
            facets = store.facets()
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: could not read the knowledge index: {exc}"
        subjects: list[str] = []
        fact_count = 0
        if fact_store is not None:
            try:
                fact_count = fact_store.count(current_only=True)
                subjects = fact_store.subjects()
            except Exception:  # noqa: BLE001 — facts are optional; show KB regardless
                pass
        return _format(corpora, facets, subjects, fact_count)

    return ToolSpec(
        name="memory_index",
        category="knowledge",
        description=(
            "Get a map of Devy's durable knowledge: which corpora exist (and their "
            "size), which frontmatter facets (doc types, tags) you can filter on, and "
            "which subjects the fact tier knows about. Orientation, not retrieval — "
            "call it when unsure where to look or which filter values are valid."
        ),
        when_to_use=(
            "Before searching when you're unsure what knowledge is available or how "
            "it's organized — e.g. to discover the corpus names, the doc types/tags "
            "you can pass as a search_knowledge `filter`, or whether the fact tier "
            "has anything on a subject. Then follow up with search_knowledge or "
            "recall_facts."
        ),
        use_cases=[
            "what do you know about / what's in your knowledge base",
            "what corpora or documents are available",
            "what tags or doc types can I filter by",
            "do you have any facts about this system",
            "where should I look for this",
        ],
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        safety_tier="read-only",
    )
