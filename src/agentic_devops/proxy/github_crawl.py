"""Crawl a repo's existing markdown into the knowledge base (Phase D-1).

Fetches markdown via the GitHub API (tree + contents) — no ``git`` binary, so it
works inside the proxy container — writes it to a temp dir, and runs the existing
``ingest_path`` over it. That reuses Phase B (OKF frontmatter → metadata) and
Phase C (secret redaction) for free; crawled docs land in the standard
``documents``/``chunks`` registries like any other corpus. The temp dir is always
removed.

D-1 ingests docs that already exist in the repo. Generating documentation for
*undocumented* code is Phase D-2.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agentic_devops.knowledge.ingest import IngestStats, ingest_path
from agentic_devops.proxy.github_client import GitHubClient

_MD_SUFFIXES = (".md", ".markdown")
_MAX_FILES = 500  # safety cap on a single crawl


@dataclass
class CrawlOutcome:
    """What a crawl produced: the ingest stats plus the commit it built from, so
    the caller can record crawl history (commit SHA + branch)."""

    stats: IngestStats
    ref: str
    commit_sha: Optional[str] = None


def _head_sha(client: GitHubClient, token: str, full_name: str, ref: str) -> Optional[str]:
    """Best-effort: the HEAD commit SHA at ``ref`` (so we record what was scanned)."""
    try:
        commits = client.list_commits(token, full_name, per_page=1)
        if commits:
            return commits[0].get("sha")
    except Exception:  # noqa: BLE001 — history is informational, never fail the crawl
        pass
    return None


def crawl_repo_markdown(
    client: GitHubClient,
    token: str,
    full_name: str,
    *,
    store: Any,
    embedder: Any,
    corpus: Optional[str] = None,
    ref: Optional[str] = None,
    redactor: Any = None,
    enricher: Any = None,
    document_store: Any = None,
    max_chars: int = 8000,
    overlap: int = 200,
    split_level: int = 2,
) -> CrawlOutcome:
    """Fetch ``full_name``'s markdown and ingest it into ``corpus`` (default: the
    repo's full name). Returns a :class:`CrawlOutcome` (stats + the commit/branch
    crawled) from the shared pipeline."""
    corpus = corpus or full_name
    if ref is None:
        ref = client.get_repo(token, full_name).get("default_branch") or "main"

    tree = client.get_tree(token, full_name, ref, recursive=True)
    md_paths = [
        t["path"] for t in tree
        if t.get("type") == "blob" and t.get("path", "").lower().endswith(_MD_SUFFIXES)
    ][:_MAX_FILES]

    commit_sha = _head_sha(client, token, full_name, ref)

    if not md_paths:
        # nothing to ingest (D-2 will generate docs) — still a recorded crawl
        return CrawlOutcome(IngestStats(corpus=corpus), ref, commit_sha)

    tmp = Path(tempfile.mkdtemp(prefix="devy-crawl-"))
    try:
        for path in md_paths:
            content = client.get_file(token, full_name, path, ref=ref)
            dest = tmp / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        stats = ingest_path(
            tmp, store, embedder, corpus=corpus,
            max_chars=max_chars, overlap=overlap, split_level=split_level,
            enricher=enricher, document_store=document_store, redactor=redactor,
        )
        return CrawlOutcome(stats, ref, commit_sha)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
