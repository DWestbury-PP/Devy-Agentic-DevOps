"""`agentic-devops` command — runs the LLM-PROXY service."""

from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(add_completion=False, help="Agentic DevOps — the LLM-PROXY service.")


@app.callback()
def _main() -> None:
    """Agentic DevOps — the LLM-PROXY service.

    A callback is required so Typer keeps ``serve`` as a named subcommand
    (single-command Typer apps otherwise collapse and drop the name), which
    keeps the ``agentic-devops serve`` UX and the daemon spawn working.
    """


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, help="Bind host (default from config)."),
    port: Optional[int] = typer.Option(None, help="Bind port (default from config)."),
) -> None:
    """Start the proxy service."""
    import uvicorn

    from agentic_devops.config import load_settings
    from agentic_devops.proxy.app import create_app

    settings = load_settings()
    application = create_app(settings)
    typer.echo(
        f"Agentic DevOps proxy on http://{host or settings.host}:{port or settings.port} "
        f"(default tier: {settings.default_tier})"
    )
    uvicorn.run(
        application,
        host=host or settings.host,
        port=port or settings.port,
        log_level="info",
    )


@app.command()
def ingest(
    path: str = typer.Argument(..., help="File or directory of docs to ingest."),
    corpus: Optional[str] = typer.Option(
        None, "--corpus", "-c", help="Corpus name (default: the directory/file name)."
    ),
    ext: Optional[list[str]] = typer.Option(
        None, "--ext", help="Extra extensions to include, e.g. --ext .py (repeatable)."
    ),
    context: bool = typer.Option(
        False, "--context",
        help="Add a fast-tier LLM synopsis per chunk (default: deterministic title>heading context only)."
    ),
) -> None:
    """Ingest docs into the knowledge base (sweep → chunk → enrich → embed → store)."""
    from pathlib import Path

    from agentic_devops.config import load_settings
    from agentic_devops.knowledge.factory import (
        build_embedder,
        build_enricher,
        build_redactor,
        build_store,
    )
    from agentic_devops.knowledge.ingest import DEFAULT_EXTENSIONS, ingest_path

    settings = load_settings()
    if not settings.knowledge.enabled:
        typer.echo("Knowledge is disabled in config (knowledge.enabled: false).")
        raise typer.Exit(code=1)

    target = Path(path).expanduser()
    if not target.exists():
        typer.echo(f"Path not found: {target}")
        raise typer.Exit(code=1)

    from agentic_devops.db import apply_schema

    extensions = tuple(DEFAULT_EXTENSIONS) + tuple(e if e.startswith(".") else f".{e}" for e in (ext or []))
    apply_schema(settings.database.url)  # ensure tables exist (idempotent)
    store = build_store(settings.database)
    embedder = build_embedder(settings.knowledge)
    enricher = build_enricher(settings, force=context)

    kcfg = settings.knowledge.chunk
    ctx_note = (
        f"LLM synopsis: {settings.resolve_tier('fast').display()}"
        if (enricher and enricher.active) else "context: deterministic (title>heading)"
    )
    typer.echo(
        f"Ingesting {target} (embedding model: {settings.knowledge.embedding.model}; {ctx_note}) …"
    )
    redactor = build_redactor(settings.knowledge)
    stats = ingest_path(
        target, store, embedder, corpus=corpus,
        extensions=extensions, max_chars=kcfg.max_chars, overlap=kcfg.overlap,
        split_level=kcfg.split_level, enricher=enricher, redactor=redactor,
    )
    redaction_note = ""
    if redactor is not None:
        redaction_note = f", {stats.secrets_redacted} secrets redacted"
        if stats.files_quarantined:
            redaction_note += f", {stats.files_quarantined} QUARANTINED (suspected secret)"
    typer.echo(
        f"Corpus '{stats.corpus}': {stats.files_ingested} ingested, "
        f"{stats.files_skipped} unchanged, {stats.chunks_written} chunks written "
        f"({stats.chunks_contextualized} contextualized; {stats.files_seen} files seen"
        f"{redaction_note})."
    )
    typer.echo(f"Knowledge base now holds: {store.corpora()}")


@app.command("crawl-repo")
def crawl_repo(
    repo: str = typer.Argument(..., help="Repository as owner/name."),
    corpus: Optional[str] = typer.Option(None, "--corpus", "-c", help="Target corpus (default: the repo full name)."),
    token: Optional[str] = typer.Option(None, "--token", help="Read-only GitHub PAT (or set GITHUB_TOKEN)."),
    context: bool = typer.Option(False, "--context", help="Add a fast-tier LLM synopsis per chunk."),
) -> None:
    """Crawl a repo's existing markdown into the knowledge base (Phase D-1).

    Fetches markdown via the GitHub API, runs it through the same OKF + redaction
    ingest pipeline, and registers it so it shows in the Knowledge UI. Uses a
    read-only PAT from --token or the GITHUB_TOKEN env var.
    """
    import os

    from agentic_devops.config import load_settings
    from agentic_devops.db import apply_schema, get_pool
    from agentic_devops.knowledge.factory import (
        build_embedder, build_enricher, build_redactor, build_store,
    )
    from agentic_devops.proxy.documents import DocumentStore
    from agentic_devops.proxy.github import RepoCrawlStore
    from agentic_devops.proxy.github_client import GitHubClient, GitHubError
    from agentic_devops.proxy.github_crawl import crawl_repo_markdown

    settings = load_settings()
    if not settings.knowledge.enabled:
        typer.echo("Knowledge is disabled in config (knowledge.enabled: false).")
        raise typer.Exit(code=1)
    pat = token or os.environ.get("GITHUB_TOKEN")
    if not pat:
        typer.echo("A read-only GitHub PAT is required (--token or GITHUB_TOKEN).")
        raise typer.Exit(code=1)

    apply_schema(settings.database.url)
    kcfg = settings.knowledge.chunk
    typer.echo(f"Crawling {repo} markdown (embedding: {settings.knowledge.embedding.model}) …")
    pool = get_pool(settings.database.url)
    try:
        outcome = crawl_repo_markdown(
            GitHubClient(), pat, repo,
            store=build_store(settings.database), embedder=build_embedder(settings.knowledge),
            corpus=corpus, redactor=build_redactor(settings.knowledge),
            enricher=build_enricher(settings, force=context),
            document_store=DocumentStore(pool),
            max_chars=kcfg.max_chars, overlap=kcfg.overlap, split_level=kcfg.split_level,
        )
    except GitHubError as exc:
        typer.echo(f"GitHub error: {exc}")
        raise typer.Exit(code=1)
    stats = outcome.stats
    RepoCrawlStore(pool).record(
        repo, stats.corpus, commit_sha=outcome.commit_sha, default_branch=outcome.ref,
        files_ingested=stats.files_ingested, chunks_written=stats.chunks_written,
        files_quarantined=stats.files_quarantined, secrets_redacted=stats.secrets_redacted,
    )
    note = f", {stats.secrets_redacted} secrets redacted" if stats.secrets_redacted else ""
    if stats.files_quarantined:
        note += f", {stats.files_quarantined} QUARANTINED"
    sha = f" @ {outcome.commit_sha[:7]}" if outcome.commit_sha else ""
    typer.echo(
        f"Corpus '{stats.corpus}'{sha}: {stats.files_ingested} ingested, "
        f"{stats.files_skipped} unchanged, {stats.chunks_written} chunks{note}."
    )


@app.command("docgen")
def docgen(
    repo: str = typer.Argument(..., help="Repository as owner/name."),
    component: list[str] = typer.Option(None, "--component", help="Limit to these component path(s); repeatable."),
    brief: Optional[str] = typer.Option(None, "--brief", help="Scan-brief guidance to store + feed the generator."),
    token: Optional[str] = typer.Option(None, "--token", help="Read-only GitHub PAT (or set GITHUB_TOKEN)."),
    force: bool = typer.Option(False, "--force", help="Regenerate even if the repo is unchanged since last run."),
) -> None:
    """Generate OKF architecture docs from a repo's code (Phase D-2).

    Diff-driven: skips an unchanged repo, regenerates only touched components.
    Writes redacted OKF markdown under `knowledge.docgen_output_dir` and ingests it
    into a `gen:<repo>` corpus. Read-only PAT from --token or GITHUB_TOKEN.
    """
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    from agentic_devops.config import load_settings
    from agentic_devops.db import apply_schema, get_pool
    from agentic_devops.knowledge.factory import (
        build_embedder, build_enricher, build_redactor, build_store,
    )
    from agentic_devops.proxy.documents import DocumentStore
    from agentic_devops.proxy.docgen_run import run_docgen
    from agentic_devops.proxy.docgen_store import DocComponentStore, RepoDocgenStore
    from agentic_devops.proxy.github_client import GitHubClient, GitHubError
    from agentic_devops.proxy.providers import ProviderClient

    settings = load_settings()
    pat = token or os.environ.get("GITHUB_TOKEN")
    if not pat:
        typer.echo("A read-only GitHub PAT is required (--token or GITHUB_TOKEN).")
        raise typer.Exit(code=1)
    try:
        tier = settings.resolve_tier(settings.knowledge.docgen_tier)
    except KeyError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)

    apply_schema(settings.database.url)
    pool = get_pool(settings.database.url)
    out_dir = Path(settings.knowledge.docgen_output_dir)
    typer.echo(f"Generating docs for {repo} (tier: {tier.model}, → {out_dir}/) …")
    try:
        outcome = run_docgen(
            GitHubClient(), pat, repo,
            repo_store=RepoDocgenStore(pool), component_store=DocComponentStore(pool),
            kb_store=build_store(settings.database), embedder=build_embedder(settings.knowledge),
            provider=ProviderClient(request_timeout=settings.request_timeout), tier=tier,
            output_dir=out_dir, generated_at=datetime.now(timezone.utc).isoformat(),
            redactor=build_redactor(settings.knowledge), enricher=build_enricher(settings),
            document_store=DocumentStore(pool), scan_brief=brief,
            only=list(component) if component else None,
            max_files=settings.knowledge.docgen_max_files, force=force,
        )
    except GitHubError as exc:
        typer.echo(f"GitHub error: {exc}")
        raise typer.Exit(code=1)

    if outcome.skipped:
        typer.echo(f"Unchanged since last run ({(outcome.head_sha or '')[:7]}) — skipped. Use --force to regenerate.")
        return
    q = f", {len(outcome.components_quarantined)} quarantined" if outcome.components_quarantined else ""
    typer.echo(
        f"Corpus '{outcome.corpus}' @ {(outcome.head_sha or '')[:7]}: "
        f"{len(outcome.components_generated)}/{outcome.components_total} components generated, "
        f"{outcome.chunks_written} chunks{q}."
    )
    if outcome.components_generated:
        typer.echo("  generated: " + ", ".join(c or "(root)" for c in outcome.components_generated))


db_app = typer.Typer(add_completion=False, help="Database bootstrap (Postgres + pgvector).")
app.add_typer(db_app, name="db")


def _safe_dsn(url: str) -> str:
    """Mask the password in a DSN before echoing it."""
    import re

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


@db_app.command("init")
def db_init() -> None:
    """Apply the bootstrap schema (pgvector extension + tables) to the configured DB.

    Idempotent — safe to re-run. Use this to provision an existing or managed
    database (e.g. RDS/Aurora); the bundled compose Postgres bootstraps itself.
    """
    from agentic_devops.config import load_settings
    from agentic_devops.db import apply_schema

    settings = load_settings()
    typer.echo(f"Applying schema to {_safe_dsn(settings.database.url)} …")
    try:
        apply_schema(settings.database.url)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Schema bootstrap failed: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo("Schema applied: pgvector extension + chunks/sessions tables.")


admin_app = typer.Typer(add_completion=False, help="Admin control-plane helpers.")
app.add_typer(admin_app, name="admin")


@admin_app.command("set-password")
def admin_set_password(
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True,
        help="The admin password (prompted; not echoed).",
    ),
) -> None:
    """Hash an admin password for the control plane (paste the output into .env)."""
    import secrets

    import bcrypt

    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    typer.echo("\nAdd these to your .env (control-plane secrets — keep them out of git):\n")
    typer.echo(f"DEVY_ADMIN_PASSWORD_HASH={pw_hash}")
    typer.echo(f"DEVY_ADMIN_SECRET={secrets.token_hex(32)}")
    typer.echo("\nUntil both are set, the admin plane stays disabled (endpoints return 503).")


@admin_app.command("gen-key")
def admin_gen_key() -> None:
    """Generate a Fernet encryption key for per-host MCP tokens (host registry)."""
    from cryptography.fernet import Fernet

    typer.echo("Add this to your .env (encrypts per-host tokens at rest):\n")
    typer.echo(f"DEVY_ENCRYPTION_KEY={Fernet.generate_key().decode()}")


if __name__ == "__main__":
    app()
