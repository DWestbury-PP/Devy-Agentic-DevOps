"""OKF frontmatter parsing (Phase B) — pure-logic, no DB."""

from agentic_devops.knowledge.frontmatter import (
    RESERVED_FILENAMES,
    extract_links,
    frontmatter_metadata,
    parse_frontmatter,
)

_DOC = """\
---
type: Playbook
title: Incident response
tags: [oncall, incident]
timestamp: 2026-04-12T09:00:00Z
resource: https://example.com/runbooks/orders
custom_key: keep-me
---

# Trigger

A freshness alert fires. See the [orders table](/tables/orders.md) and
[neighbor](./other.md). External [docs](https://cloud.example.com/x) and an
[anchor](#steps) are not cross-links.
"""


def test_parse_extracts_mapping_and_strips_body():
    fm, body = parse_frontmatter(_DOC)
    assert fm["type"] == "Playbook"
    assert fm["title"] == "Incident response"
    assert fm["tags"] == ["oncall", "incident"]
    assert fm["custom_key"] == "keep-me"  # unknown keys preserved
    assert body.lstrip().startswith("# Trigger")
    assert "type: Playbook" not in body  # frontmatter stripped from the body


def test_timestamp_is_jsonified_to_string():
    fm, _ = parse_frontmatter(_DOC)
    # YAML would load an ISO datetime as a datetime object; we coerce to a string
    # so the metadata is JSON/JSONB-serializable.
    assert isinstance(fm["timestamp"], str)
    assert fm["timestamp"].startswith("2026-04-12")


def test_no_frontmatter_returns_raw():
    raw = "# Just a doc\n\nNo frontmatter here.\n"
    fm, body = parse_frontmatter(raw)
    assert fm == {} and body == raw


def test_horizontal_rule_not_mistaken_for_frontmatter():
    raw = "# Title\n\nText\n\n---\n\nMore text after a rule.\n"
    fm, body = parse_frontmatter(raw)
    assert fm == {} and body == raw


def test_unparseable_yaml_is_permissive():
    raw = "---\n: : bad: [unclosed\n---\n\nBody.\n"
    fm, body = parse_frontmatter(raw)
    assert fm == {} and body == raw  # warned, never raised


def test_non_mapping_frontmatter_ignored():
    raw = "---\n- just\n- a\n- list\n---\n\nBody.\n"
    fm, body = parse_frontmatter(raw)
    assert fm == {} and body == raw


def test_empty_block_strips_but_yields_no_metadata():
    raw = "---\n---\n\n# Body\n"
    fm, body = parse_frontmatter(raw)
    assert fm == {} and "# Body" in body and "---" not in body


def test_extract_links_keeps_internal_drops_external_and_anchors():
    _, body = parse_frontmatter(_DOC)
    links = extract_links(body)
    assert "/tables/orders.md" in links
    assert "./other.md" in links
    assert all(not l.startswith("http") for l in links)
    assert "#steps" not in links


def test_frontmatter_metadata_adds_links():
    fm, body = parse_frontmatter(_DOC)
    meta = frontmatter_metadata(fm, body)
    assert meta["type"] == "Playbook"
    assert "links" in meta and "/tables/orders.md" in meta["links"]


def test_reserved_filenames():
    assert "index.md" in RESERVED_FILENAMES and "log.md" in RESERVED_FILENAMES
