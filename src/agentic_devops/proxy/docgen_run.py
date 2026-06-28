"""Doc generation — the live run (Phase D-2-1).

Ties the deterministic spine (`knowledge/docgen.py`) to the GitHub read client, the
model provider, the redactor, and the ingest pipeline. Diff-driven: an unchanged
repo is skipped (zero model calls); a changed repo regenerates only its touched
components. Each generated doc is **redacted before it touches disk** (the docs-corpus
file is a persistence point), then ingested into a ``gen:<repo>`` corpus.

The synthesis is one provider call per component over its bounded signal files —
deliberately simple and grounded (the prompt forbids invention; quality + bounded
context are the hallucination controls, per the design).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agentic_devops.knowledge.docgen import (
    architecture_frontmatter,
    architecture_prompt,
    arch_doc_path,
    assemble_okf,
    discover_components,
    head_is_current,
    map_changes,
    select_signal_files,
)
from agentic_devops.knowledge.ingest import ingest_path
from agentic_devops.knowledge.redaction import apply_redaction
from agentic_devops.proxy.github_client import GitHubClient, GitHubError


@dataclass
class DocgenOutcome:
    full_name: str
    head_sha: Optional[str] = None
    corpus: str = ""
    skipped: bool = False                      # repo unchanged since checkpoint
    components_total: int = 0
    components_generated: list[str] = field(default_factory=list)
    components_quarantined: list[str] = field(default_factory=list)
    chunks_written: int = 0


def _head_sha(client: GitHubClient, token: str, full_name: str) -> Optional[str]:
    try:
        commits = client.list_commits(token, full_name, per_page=1)
        return commits[0].get("sha") if commits else None
    except GitHubError:
        return None


def run_docgen(
    client: GitHubClient,
    token: str,
    full_name: str,
    *,
    repo_store: Any,
    component_store: Any,
    kb_store: Any,
    embedder: Any,
    provider: Any,
    tier: Any,
    output_dir: Path,
    generated_at: str,
    redactor: Any = None,
    enricher: Any = None,
    document_store: Any = None,
    scan_brief: Optional[str] = None,
    only: Optional[list[str]] = None,
    max_files: int = 40,
    force: bool = False,
) -> DocgenOutcome:
    """Generate OKF architecture docs for ``full_name``'s changed components."""
    corpus = f"gen:{full_name}"
    repo_name = full_name.split("/")[-1]
    repo_info = client.get_repo(token, full_name)
    default_branch = repo_info.get("default_branch") or "main"
    head = _head_sha(client, token, full_name)

    record = repo_store.get(full_name)
    last = record.last_doc_sha if record else None
    brief = scan_brief if scan_brief is not None else (record.scan_brief if record else "")

    if head_is_current(last, head) and not force and not only:
        return DocgenOutcome(full_name=full_name, head_sha=head, corpus=corpus, skipped=True)

    repo_store.set_status(full_name, "running")
    try:
        tree = client.get_tree(token, full_name, head or default_branch, recursive=True)
        paths = [t["path"] for t in tree if t.get("type") == "blob" and t.get("path")]
        components = discover_components(paths, repo_name=repo_name)

        if only:
            targets = [c for c in components if c.path in only]
        elif last:
            changed = [f.get("filename") for f in client.compare(token, full_name, last, head).get("files", [])]
            targets = map_changes([c for c in changed if c], components).touched
        else:
            targets = components  # first run: document everything

        outcome = DocgenOutcome(
            full_name=full_name, head_sha=head, corpus=corpus, components_total=len(components),
        )
        for comp in targets:
            sigs = select_signal_files(comp, paths, components=components, max_files=max_files)
            if not sigs:
                continue
            contents: dict[str, str] = {}
            for p in sigs:
                try:
                    contents[p] = client.get_file(token, full_name, p, ref=head)
                except GitHubError:
                    continue
            if not contents:
                continue
            prompt = architecture_prompt(full_name, comp, contents, scan_brief=brief)
            resp = provider.complete([{"role": "user", "content": prompt}], tier=tier)
            body = (resp.text or "").strip()
            if not body:
                continue
            fm = architecture_frontmatter(
                full_name, comp, commit_sha=head or "", model=tier.model, generated_at=generated_at,
            )
            doc = assemble_okf(fm, body)
            # Redact BEFORE the doc touches disk (the corpus file is a persistence point).
            redacted, _ = apply_redaction(doc, redactor)
            if redacted is None:  # fail-closed: the model emitted something secret-shaped
                outcome.components_quarantined.append(comp.path)
                continue
            rel = arch_doc_path(full_name, comp)
            dest = output_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(redacted, encoding="utf-8")
            component_store.upsert(full_name, comp, arch_doc_path=rel, last_doc_sha=head)
            outcome.components_generated.append(comp.path)

        # Ingest just this repo's generated subtree into the gen:<repo> corpus.
        repo_doc_dir = output_dir / full_name
        if repo_doc_dir.is_dir():
            stats = ingest_path(
                repo_doc_dir, kb_store, embedder, corpus=corpus,
                redactor=redactor, enricher=enricher, document_store=document_store,
            )
            outcome.chunks_written = stats.chunks_written

        n_components = len(component_store.list(full_name))
        repo_store.checkpoint(
            full_name, head or "", default_branch=default_branch, components_doced=n_components,
        )
        return outcome
    except Exception as exc:  # noqa: BLE001 — record the failure, don't advance the checkpoint
        repo_store.set_status(full_name, "error", error=str(exc)[:300])
        raise
