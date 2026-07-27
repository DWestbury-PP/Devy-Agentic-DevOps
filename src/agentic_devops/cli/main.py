"""`agentic-devops` command — runs the LLM-PROXY service."""

from __future__ import annotations

import atexit
from typing import Optional

import typer

app = typer.Typer(add_completion=False, help="Agentic DevOps — the LLM-PROXY service.")


@atexit.register
def _close_pools() -> None:
    """Close any DB pool a one-shot command opened, while worker threads can still
    be joined — otherwise psycopg's pool finalizer errors at interpreter shutdown
    (PythonFinalizationError on 3.14). No-op if the DB module was never imported."""
    import sys

    mod = sys.modules.get("agentic_devops.db")
    if mod is not None:
        try:
            mod.close_all()
        except Exception:  # noqa: BLE001 — best-effort cleanup at exit
            pass


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

    from agentic_devops.db.migrate import migrate

    extensions = tuple(DEFAULT_EXTENSIONS) + tuple(e if e.startswith(".") else f".{e}" for e in (ext or []))
    migrate(settings.database.url)  # ensure the DB is at head (idempotent)
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
    from agentic_devops.db import get_pool
    from agentic_devops.db.migrate import migrate
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

    migrate(settings.database.url)
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
    from agentic_devops.db import get_pool
    from agentic_devops.db.migrate import migrate
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

    migrate(settings.database.url)
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


db_app = typer.Typer(add_completion=False, help="Database schema migrations (Postgres + pgvector).")
app.add_typer(db_app, name="db")


def _safe_dsn(url: str) -> str:
    """Mask the password in a DSN before echoing it."""
    import re

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


@db_app.command("migrate")
def db_migrate(
    status_only: bool = typer.Option(
        False, "--status", help="Show applied vs pending migrations; apply nothing."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be applied; apply nothing."
    ),
) -> None:
    """Apply pending schema migrations to the configured DB, in order.

    Versioned + gated: only not-yet-applied migrations run, each in its own
    transaction. A pre-migration DB (tables present, no ledger) is baseline-stamped
    at 001 without re-running it, then caught up. Idempotent — a DB at head is a
    no-op. Use this to provision or evolve any DB, including managed (RDS/Aurora).
    See docs/db-migrations.md.
    """
    from agentic_devops.config import load_settings
    from agentic_devops.db.migrate import migrate, status
    from agentic_devops.db.migrate import MigrationError

    settings = load_settings()
    dsn = _safe_dsn(settings.database.url)

    if status_only:
        applied, pending = status(settings.database.url)
        typer.echo(f"Migrations for {dsn}")
        if applied:
            typer.echo("  applied:")
            for version, name, applied_at, baseline in applied:
                tag = " (baseline-stamped)" if baseline else ""
                typer.echo(f"    ✓ {version}_{name}{tag} — {applied_at}")
        else:
            typer.echo("  applied: (none — ledger not initialized)")
        if pending:
            typer.echo("  pending:")
            for m in pending:
                typer.echo(f"    • {m.version}_{m.name}")
        else:
            typer.echo("  pending: (none — DB is at head)")
        return

    verb = "Planning (dry-run)" if dry_run else "Applying"
    typer.echo(f"{verb} migrations for {dsn} …")
    try:
        plan = migrate(settings.database.url, dry_run=dry_run)
    except MigrationError as exc:
        typer.echo(f"Migration error: {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Migration failed: {exc}")
        raise typer.Exit(code=1) from exc

    if not plan:
        typer.echo("Already at head — nothing to do.")
        return
    for item in plan:
        if item.action == "baseline-stamp":
            typer.echo(f"  stamped baseline {item.version}_{item.name} (existing schema, not executed)")
        else:
            typer.echo(f"  {'would apply' if dry_run else 'applied'} {item.version}_{item.name}")
    if not dry_run:
        typer.echo("Done.")


@db_app.command("init")
def db_init() -> None:
    """Alias for ``db migrate`` (kept for muscle memory / managed-DB provisioning)."""
    # Pass explicit booleans: calling the Typer command bare would forward the
    # OptionInfo defaults (which are truthy), landing in the wrong branch.
    db_migrate(status_only=False, dry_run=False)


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


# -- secrets (S-1): manage the unified secrets manager (LocalStack/AWS SM) --------
secrets_app = typer.Typer(add_completion=False, help="Secrets backend (dev=LocalStack / prod=AWS SM).")
app.add_typer(secrets_app, name="secrets")


def _known_refs(settings, secrets):
    """(refs, note) — the managed inventory + an optional 'registry unavailable' note.
    Shared enumeration so `list` and `sync` agree on which refs exist. The DB is
    optional: if the registry is unreachable, fall back to provider keys only.
    Wraps the store reads (not just get_pool) so a pool timeout also degrades."""
    from agentic_devops.db import get_pool
    from agentic_devops.proxy.secrets_sync import known_secret_refs

    try:
        pool = get_pool(settings.database.url)
        return known_secret_refs(settings, secrets, pool), None
    except Exception as exc:  # noqa: BLE001 — DB optional for a bare secrets check
        return known_secret_refs(settings, secrets, None), f"registry unavailable: {exc}"


@secrets_app.command("list")
def secrets_list() -> None:
    """List secret names known to Devy and whether each is loaded (never the value)."""
    from agentic_devops.config import load_settings
    from agentic_devops.proxy.secrets import build_secrets_provider

    import os as _os

    settings = load_settings()
    secrets = build_secrets_provider(settings)
    endpoint = settings.secrets.endpoint_url or _os.environ.get("AWS_ENDPOINT_URL") or "aws"
    typer.echo(f"mode={settings.secrets.mode}  endpoint={endpoint}  "
               f"writable={secrets.writable}  reachable={secrets.health()}\n")
    refs, note = _known_refs(settings, secrets)
    if note:
        typer.echo(f"({note})\n")
    for ref, kind in refs:
        mark = "✓ loaded" if secrets.exists(ref) else "· empty"
        typer.echo(f"  {mark:10} {ref:40} {kind}")


@secrets_app.command("sync")
def secrets_sync(
    region: Optional[str] = typer.Option(None, "--region", help="Target AWS region (default: secrets.region / AWS_DEFAULT_REGION)."),
    profile: Optional[str] = typer.Option(None, "--profile", help="AWS profile for the target (admin) credentials."),
    only: Optional[list[str]] = typer.Option(None, "--only", help="Sync only refs matching this token (repeatable)."),
    skip: Optional[list[str]] = typer.Option(None, "--skip", help="Skip refs matching this token (repeatable)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; write nothing."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Sync local (dev/LocalStack) secrets UP to real AWS Secrets Manager.

    Out-of-band bootstrap/rotation: reads your dev catalog and upserts the SAME refs
    into real AWS SM using your admin credentials. Idempotent + diff-aware — re-run
    it to push only what changed. Values never touch disk or logs; a ref present in
    AWS but absent in dev is left alone. Needs an admin principal allowed to
    Create/Put on the target; the deployed instance role only READS at runtime.
    """
    import os as _os

    from agentic_devops.config import load_settings
    from agentic_devops.proxy.secrets import build_secrets_provider
    from agentic_devops.proxy.secrets_sync import (
        apply_sync, build_aws_target_provider, mask, plan_sync, target_identity,
    )

    settings = load_settings()
    if settings.secrets.mode != "dev":
        typer.echo("Refused: run `secrets sync` from your DEV environment (source = LocalStack).")
        raise typer.Exit(code=1)

    region = region or settings.secrets.region or _os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        typer.echo("No target region: pass --region or set AWS_DEFAULT_REGION.")
        raise typer.Exit(code=1)

    source = build_secrets_provider(settings)          # dev / LocalStack
    refs, note = _known_refs(settings, source)
    if note:
        typer.echo(f"({note})\n")

    def keep(ref: str) -> bool:
        if only and not any(tok in ref for tok in only):
            return False
        if skip and any(tok in ref for tok in skip):
            return False
        return True

    refs = [(r, k) for (r, k) in refs if keep(r)]
    if not refs:
        typer.echo("No matching refs to sync.")
        return

    try:
        account, arn = target_identity(region, profile)
        target = build_aws_target_provider(region, profile)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Could not reach the target AWS account ({type(exc).__name__}: {exc}).")
        typer.echo("Check your AWS credentials/--profile and --region.")
        raise typer.Exit(code=1) from exc

    items = plan_sync(source, target, refs)
    typer.echo(f"Target: AWS account {account}  region {region}  (as {arn})")
    typer.echo(f"Source: dev/LocalStack  namespace {settings.secrets.namespace}\n")
    glyph = {"add": "+ add     ", "update": "~ update  ", "unchanged": "= same    ", "absent": "· skip    "}
    for it in items:
        detail = mask(it.src_len)
        if it.action == "update":
            detail = f"{mask(it.dst_len)} → {mask(it.src_len)}"
        elif it.action == "absent":
            detail = "not set in dev"
        typer.echo(f"  {glyph[it.action]} {it.ref:40} {detail}")

    adds = sum(1 for it in items if it.action == "add")
    updates = sum(1 for it in items if it.action == "update")
    if adds + updates == 0:
        typer.echo("\nAWS is already in sync — nothing to write.")
        return
    if dry_run:
        typer.echo(f"\n(dry-run) would write {adds} new + {updates} changed secret(s).")
        return

    if not yes:
        typer.echo(f"\nAbout to write {adds} new + {updates} changed secret(s) to AWS account {account} ({region}).")
        confirm = typer.prompt("Type 'yes' to proceed")
        if confirm != "yes":
            typer.echo("aborted")
            raise typer.Exit(code=1)

    applied, failed = apply_sync(source, target, items)
    typer.echo(f"\nWrote {len(applied)} secret(s) to AWS.")
    if failed:
        typer.echo(f"{len(failed)} FAILED:")
        for ref, err in failed:
            typer.echo(f"  ✗ {ref}: {err}")
        raise typer.Exit(code=1)


@secrets_app.command("set")
def secrets_set(ref: str = typer.Argument(...), value: str = typer.Argument(...)) -> None:
    """Set a secret by name (dev only — prod is provisioned out-of-band)."""
    from agentic_devops.config import load_settings
    from agentic_devops.proxy.secrets import build_secrets_provider

    secrets = build_secrets_provider(load_settings())
    if not secrets.writable:
        typer.echo("Refused: secrets are read-only in prod mode (provision via your IaC).")
        raise typer.Exit(code=1)
    secrets.set(ref, value)
    typer.echo(f"set {ref}")


if __name__ == "__main__":
    app()
