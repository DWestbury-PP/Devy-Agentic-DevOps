"""Web search via Tavily (native tool — extended retrieval).

A single read-only REST call, so it's a native ToolSpec rather than a mounted MCP:
the API key already lives in the secrets manager / Secrets tab (`devy/provider/tavily`,
hydrated to ``TAVILY_API_KEY``), and a curated ``when_to_use`` lets ``find_tools``
surface it well. Discovered on demand like every other tool — no context bloat.
"""

from __future__ import annotations

import os
from typing import Any

from agentic_devops.tools.base import ToolSpec

_TAVILY_URL = "https://api.tavily.com/search"


def build_web_search_tool() -> ToolSpec:
    def handler(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: 'query' is required."
        key = os.environ.get("TAVILY_API_KEY")
        if not key:
            return ("ERROR: web search is not configured — set the Tavily API key on the "
                    "admin Secrets tab (devy/provider/tavily).")

        import httpx

        payload = {
            "api_key": key,
            "query": query,
            "max_results": max(1, min(int(args.get("max_results", 5) or 5), 10)),
            "search_depth": "advanced" if args.get("depth") == "advanced" else "basic",
            "include_answer": True,
        }
        try:
            r = httpx.post(_TAVILY_URL, json=payload, timeout=20.0)
        except Exception as exc:  # noqa: BLE001 — network/timeout → readable error for the model
            return f"ERROR: web search request failed ({type(exc).__name__})."
        if r.status_code in (401, 403):
            return "ERROR: the Tavily API key was rejected — check it on the Secrets tab."
        if r.status_code != 200:
            return f"ERROR: web search returned HTTP {r.status_code}."

        data = r.json()
        lines: list[str] = []
        if data.get("answer"):
            lines.append(f"Answer: {data['answer']}\n")
        for i, res in enumerate(data.get("results", []), 1):
            title = res.get("title") or "(untitled)"
            url = res.get("url") or ""
            content = (res.get("content") or "").strip()
            lines.append(f"{i}. {title}\n   {url}\n   {content[:500]}")
        return "\n".join(lines) if lines else "No results."

    return ToolSpec(
        name="web_search",
        category="web",
        description="Search the public web (via Tavily) and return ranked results with snippets and a synthesized answer.",
        when_to_use=(
            "Look up current or external information not in the knowledge base or host data — "
            "documentation, error messages, CVEs, product/version facts, general questions. "
            "Args: query (required), max_results (1-10), depth (basic|advanced)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "the search query"},
                "max_results": {"type": "integer", "description": "how many results (1-10, default 5)"},
                "depth": {"type": "string", "enum": ["basic", "advanced"],
                          "description": "search depth (default basic)"},
            },
            "required": ["query"],
        },
        handler=handler,
        safety_tier="read-only",
    )
