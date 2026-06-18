"""Enrichment: contextual blurb seam, doc metadata heuristics, best-effort."""

from agentic_devops.knowledge.enrich import Enricher, doc_title, doc_type, lineage_context


def test_lineage_context_avoids_doubling_title():
    # Title is already the root of the heading path → no duplication.
    assert lineage_context("Runbook", "Runbook > Mitigation") == "Runbook > Mitigation"
    # Title not in the path (e.g. doc with no H1) → prepend it.
    assert lineage_context("file", "Triage") == "file > Triage"
    # Degenerate cases.
    assert lineage_context("", "A > B") == "A > B"
    assert lineage_context("Title", "") == "Title"


def test_doc_title_uses_first_h1():
    assert doc_title("# Checkout Runbook\n\nbody") == "Checkout Runbook"
    assert doc_title("no heading here", fallback="file.md") == "file.md"


def test_doc_type_heuristics():
    assert doc_type("incidents/2024-failover.md", "# Postmortem\nRCA of ...") == "postmortem"
    assert doc_type("runbooks/db.md", "On-call mitigation steps") == "runbook"
    assert doc_type("docs/arch.md", "System design and architecture") == "architecture"
    assert doc_type("notes.md", "just some notes") == "doc"


def test_enricher_inactive_is_noop():
    # No context_fn → inactive → empty prefix, no calls.
    assert Enricher(context_fn=None).active is False
    assert Enricher(context_fn=None).contextualize("doc", "chunk") == ""
    # Explicitly disabled even with a fn.
    e = Enricher(context_fn=lambda d, c: "x", enabled=False)
    assert e.active is False
    assert e.contextualize("doc", "chunk") == ""


def test_enricher_active_normalizes_blurb():
    calls = []

    def ctx(doc, chunk):
        calls.append((doc, chunk))
        return "  This chunk\n covers the   checkout service.  "

    e = Enricher(context_fn=ctx)
    assert e.active is True
    assert e.contextualize("the whole doc", "a chunk") == "This chunk covers the checkout service."
    assert calls == [("the whole doc", "a chunk")]


def test_enricher_caps_document_context():
    seen = {}

    def ctx(doc, chunk):
        seen["doc_len"] = len(doc)
        return "ok"

    Enricher(context_fn=ctx, max_doc_chars=10).contextualize("x" * 500, "chunk")
    assert seen["doc_len"] == 10


def test_enricher_is_best_effort_on_failure():
    def boom(doc, chunk):
        raise RuntimeError("LLM down")

    assert Enricher(context_fn=boom).contextualize("doc", "chunk") == ""


def test_metadata_for_skips_empty():
    assert Enricher.metadata_for("Title", "runbook", "A > B") == {
        "title": "Title", "doc_type": "runbook", "headings": "A > B",
    }
    assert Enricher.metadata_for("", "", "") == {}
