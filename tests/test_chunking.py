"""Structural Markdown chunking."""

from agentic_devops.knowledge.chunking import chunk_markdown

DOC = """# Runbook

Intro line under H1.

## Triage

Look at the dashboard first.

## Mitigation

### Fail open

Set the flag.

### Roll back

Undo the deploy.
"""


def test_splits_on_h2_keeping_subsections_inline():
    # Default split_level=2: chunks anchor at H1/H2; H3 subsections stay inline
    # with their parent section (related content kept together).
    chunks = chunk_markdown(DOC)
    paths = [c.heading_path for c in chunks]

    assert "Runbook" in paths
    assert "Runbook > Triage" in paths
    assert "Runbook > Mitigation" in paths
    assert "Runbook > Mitigation > Fail open" not in paths  # H3 not its own chunk

    mitigation = next(c for c in chunks if c.heading_path == "Runbook > Mitigation")
    # Both H3 subsections and their bodies live in the one Mitigation chunk.
    assert "Set the flag." in mitigation.text and "Undo the deploy." in mitigation.text
    assert "### Fail open" in mitigation.text and "### Roll back" in mitigation.text


def test_split_level_3_separates_subsections():
    chunks = chunk_markdown(DOC, split_level=3)
    paths = [c.heading_path for c in chunks]
    assert "Runbook > Mitigation > Fail open" in paths
    assert "Runbook > Mitigation > Roll back" in paths


def test_indices_are_sequential():
    chunks = chunk_markdown(DOC)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_oversized_section_is_windowed():
    body = "para. " * 1000  # one long section, no headings
    doc = f"# Big\n\n{body}"
    chunks = chunk_markdown(doc, max_chars=500, overlap=50)
    assert len(chunks) > 1
    assert all(len(c.text) <= 500 + 50 for c in chunks)
    assert all(c.heading_path == "Big" for c in chunks)


def test_hash_comments_in_code_fences_are_not_headings():
    # A bash example with `#` comments must NOT split or pollute heading paths.
    doc = (
        "# Setup\n\n"
        "Run this:\n\n"
        "```bash\n"
        "# 1. configure the thing\n"
        "## not a heading either\n"
        "make build\n"
        "```\n\n"
        "Done.\n"
    )
    chunks = chunk_markdown(doc)
    paths = [c.heading_path for c in chunks]
    assert paths == ["Setup"]  # only the real H1; no chunk per `#` comment
    body = chunks[0].text
    assert "# 1. configure the thing" in body  # code kept inline, verbatim
    assert "```bash" in body


def test_empty_and_headingless():
    assert chunk_markdown("") == []
    plain = chunk_markdown("just some text\n\nmore text")
    assert len(plain) == 1
    assert plain[0].heading_path == ""
