"""OKF frontmatter parsing (Knowledge Memory, Phase B).

The corpus format is Google's Open Knowledge Format v0.1: a directory tree of
markdown files, each with a YAML frontmatter block delimited by ``---``. This
module turns that block into queryable ``chunks.metadata`` and extracts the
body for chunking. It is deliberately **permissive** (OKF §9): an unparseable or
non-mapping block is warned about and treated as "no frontmatter", never an
error — legacy/non-OKF markdown still ingests.

The one OKF-required field is ``type``; ``title``/``description``/``resource``/
``tags``/``timestamp`` are recommended. We promote those to first-class metadata
keys but **preserve every key** the producer wrote (consumers must tolerate
unknown keys). Cross-links (markdown links to other concepts) are extracted so a
future graph tier has the edges without a re-ingest.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

import yaml

logger = logging.getLogger("agentic_devops")

# OKF's required + recommended keys, in priority order (SPEC §4.1). Promoted to
# first-class metadata; the rest of the frontmatter is preserved verbatim.
FIRST_CLASS_KEYS: tuple[str, ...] = (
    "type", "title", "description", "resource", "tags", "timestamp",
)

# Reserved filenames that are NOT concept documents (SPEC §3.1): a directory
# listing and a change log. Skipped as chunk sources at any level of the tree.
RESERVED_FILENAMES: frozenset[str] = frozenset({"index.md", "log.md"})

# A leading YAML frontmatter block: ``---`` on the first line, body after the
# closing ``---``. Anchored at the start so a horizontal rule mid-document isn't
# mistaken for frontmatter.
# ``\r?\n?`` before the closing fence (not ``\r?\n``) so an empty block
# (``---\n---``) parses too, not just blocks with at least one content line.
_FM_RE = re.compile(r"^﻿?---[ \t]*\r?\n(.*?)\r?\n?---[ \t]*\r?\n?(.*)$", re.DOTALL)

# Markdown links: ``[text](target)``. We keep cross-links (bundle-relative ``/…``
# or relative paths), not external citation URLs (which carry a scheme).
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _jsonify(value: Any) -> Any:
    """Coerce YAML-loaded values into JSON-serializable form.

    YAML turns ISO dates into ``date``/``datetime`` objects, which ``json.dumps``
    can't serialize — render those as ISO strings; recurse through lists/dicts.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # last resort: stringify exotic scalars


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split a markdown document into ``(frontmatter, body)``.

    Returns ``({}, raw)`` when there is no valid leading frontmatter block, when
    the YAML fails to parse, or when it isn't a mapping (each warned, never
    raised). The returned frontmatter is JSON-serializable.
    """
    m = _FM_RE.match(raw)
    if not m:
        return {}, raw
    block, body = m.group(1), m.group(2)
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:  # permissive: a bad block ≠ a bad document
        logger.warning("Ignoring unparseable YAML frontmatter: %s", exc)
        return {}, raw
    if data is None:
        return {}, body  # empty block (``---\n---``) — still strip it
    if not isinstance(data, dict):
        logger.warning("Frontmatter is not a mapping (got %s); ignoring.", type(data).__name__)
        return {}, raw
    return _jsonify(data), body


def extract_links(body: str) -> list[str]:
    """Internal cross-link targets in ``body`` (deduped, order-preserved).

    Keeps bundle-relative/relative links between concepts; drops external URLs
    (scheme-qualified) and pure ``#anchor`` fragments — those aren't graph edges.
    """
    seen: dict[str, None] = {}
    for target in _LINK_RE.findall(body):
        t = target.strip()
        if not t or t.startswith("#") or _SCHEME_RE.match(t) or t.startswith("mailto:"):
            continue
        seen.setdefault(t, None)
    return list(seen)


def frontmatter_metadata(fm: dict, body: str) -> dict:
    """Build the frontmatter-derived slice of a chunk's metadata.

    Every authored key is preserved; cross-links are added under ``links``. The
    ingest pipeline layers its own derived keys (title/doc_type/headings) on top.
    """
    meta = dict(fm)
    links = extract_links(body)
    if links:
        meta["links"] = links
    return meta
