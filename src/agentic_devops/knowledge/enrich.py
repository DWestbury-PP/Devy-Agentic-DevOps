"""Search-enhancement enrichment for chunks (Phase 9c-1).

Two cheap retrieval boosts, applied at ingest time and shared by every ingest
path (the ``ingest`` CLI today, the upload UI in 9c-2):

- **Contextual retrieval** — a ``fast``-tier LLM writes a 1–2 sentence blurb
  situating a chunk within its document (what system/component/scenario it's
  about). The blurb is prepended to the chunk *before embedding*, so an isolated
  chunk ("restart the pod") still embeds near the query that needs it ("checkout
  service crash-loop"). This is the single biggest lever (Anthropic's
  contextual-retrieval result), and it's best-effort: a failed call just yields
  no prefix, never a failed ingest.
- **Metadata** — document title + a coarse doc-type, stored as jsonb for richer
  citations and future filtering. (Full-text/keyword matching is handled by the
  generated ``tsv`` column in Postgres, not here.)

The LLM call goes through a ``complete_fn`` seam — exactly like
``embeddings.embed_fn`` and ``providers.completion_fn`` — so tests inject
deterministic context without a network call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

# (document_text, chunk_text) -> a short context blurb
ContextFn = Callable[[str, str], str]

_PROMPT = """\
You are indexing a document for search retrieval. Given the whole document and \
one chunk taken from it, write a SHORT context (1-2 sentences, max ~50 words) \
that situates the chunk within the document — the system, component, alert, or \
scenario it concerns — so the chunk is findable on its own.

Answer with ONLY the context sentence(s). No preamble, no quotes, no labels.

<document>
{document}
</document>

<chunk>
{chunk}
</chunk>"""

# Heuristic doc-type from path/content — coarse on purpose; refined in 9c-2.
_DOCTYPE_PATTERNS = [
    ("postmortem", re.compile(r"post[- ]?mortem|incident report|rca", re.I)),
    ("runbook", re.compile(r"runbook|playbook|on[- ]?call|mitigation", re.I)),
    ("architecture", re.compile(r"architecture|design doc|system design", re.I)),
    ("readme", re.compile(r"readme", re.I)),
]


def doc_title(document_text: str, fallback: str = "") -> str:
    """First Markdown H1 (``# Title``) if present, else the fallback (filename)."""
    for line in document_text.splitlines():
        m = re.match(r"^#\s+(.+)$", line.strip())
        if m:
            return m.group(1).strip()
    return fallback


def lineage_context(title: str, heading_path: str) -> str:
    """Deterministic structural context to prepend before embedding.

    Re-injects the section lineage that chunking moved into the heading path —
    "poor-man's contextual retrieval," free and exact. Avoids doubling the title
    when it's already the root of the heading path (split_level includes the H1).
    """
    if not title:
        return heading_path
    if heading_path:
        root = heading_path.split(" > ", 1)[0]
        return heading_path if root == title else f"{title} > {heading_path}"
    return title


def doc_type(source_path: str, document_text: str) -> str:
    """Best-effort document category from filename + a snippet of content."""
    hay = f"{source_path}\n{document_text[:400]}"
    for label, pat in _DOCTYPE_PATTERNS:
        if pat.search(hay):
            return label
    return "doc"


@dataclass
class Enricher:
    """Adds a contextual prefix (+ surfaces metadata) for chunks before embedding.

    ``enabled=False`` (or no ``context_fn``) makes :meth:`contextualize` a no-op
    returning ``""`` — plain ingest, no LLM calls.
    """

    context_fn: Optional[ContextFn] = None
    enabled: bool = True
    max_doc_chars: int = 8000

    @property
    def active(self) -> bool:
        return self.enabled and self.context_fn is not None

    def contextualize(self, document_text: str, chunk_text: str) -> str:
        """Return a short context blurb for ``chunk_text``; ``""`` on any failure."""
        if not self.active:
            return ""
        try:
            blurb = self.context_fn(document_text[: self.max_doc_chars], chunk_text)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — enrichment is best-effort, never blocks ingest
            return ""
        return " ".join((blurb or "").split()).strip()

    @staticmethod
    def metadata_for(title: str, dtype: str, heading_path: str) -> dict:
        meta: dict = {}
        if title:
            meta["title"] = title
        if dtype:
            meta["doc_type"] = dtype
        if heading_path:
            meta["headings"] = heading_path
        return meta


def make_context_fn(provider, tier) -> ContextFn:
    """Production seam: a ``ContextFn`` backed by a ``fast``-tier completion.

    ``provider`` is a ``ProviderClient``; ``tier`` the resolved ``fast`` ModelTier.
    Kept here (not in providers.py) so the knowledge package owns its prompt.
    """

    def fn(document_text: str, chunk_text: str) -> str:
        messages = [{"role": "user", "content": _PROMPT.format(document=document_text, chunk=chunk_text)}]
        resp = provider.complete(messages, tier)
        return resp.text or ""

    return fn
