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
    from agentic_devops.knowledge.factory import build_embedder, build_enricher, build_store
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
    stats = ingest_path(
        target, store, embedder, corpus=corpus,
        extensions=extensions, max_chars=kcfg.max_chars, overlap=kcfg.overlap,
        split_level=kcfg.split_level, enricher=enricher,
    )
    typer.echo(
        f"Corpus '{stats.corpus}': {stats.files_ingested} ingested, "
        f"{stats.files_skipped} unchanged, {stats.chunks_written} chunks written "
        f"({stats.chunks_contextualized} contextualized; {stats.files_seen} files seen)."
    )
    typer.echo(f"Knowledge base now holds: {store.corpora()}")


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
