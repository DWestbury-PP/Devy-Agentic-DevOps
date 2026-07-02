"""The LLM-PROXY FastAPI service.

One brain, exposed over one API; every front-end surface is a thin client of
these endpoints:

- ``POST /v1/chat``     — multi-turn, SSE-streamed
- ``POST /v1/complete`` — one-shot, non-streaming (constrained Markdown)
- ``GET  /v1/tiers``    — model tiers (labels only; concrete models stay hidden)
- ``GET  /v1/tools``    — registered tool metadata
- ``GET  /healthz``     — readiness
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import asdict
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sse_starlette.sse import EventSourceResponse

from agentic_devops import __version__
from agentic_devops.config import Settings, load_settings
from agentic_devops.db import apply_schema, get_pool
from agentic_devops.proxy.harness import run_turn, run_turn_streaming
from agentic_devops.proxy.prompts import assemble_messages
from agentic_devops.proxy.providers import ProviderClient
from agentic_devops.proxy.schemas import (
    AdminLogin,
    AdminToken,
    ChatRequest,
    CompleteRequest,
    CompleteResponse,
    CorpusInfo,
    DocumentInfo,
    GitHubAccountCreate,
    GitHubAccountInfo,
    GitHubAccountUpdate,
    HealthInfo,
    HostCreate,
    HostInfo,
    HostUpdate,
    MCPServerCreate,
    MCPServerInfo,
    MCPServerUpdate,
    DocgenBrief,
    DocgenInfo,
    DocgenComponentInfo,
    DocgenTriggerRequest,
    JobInfo,
    RepoCrawlInfo,
    RepoCrawlRequest,
    RepoCrawlResult,
    SecretCatalog,
    SecretEntry,
    SecretRefBody,
    SecretSet,
    SecretTestResult,
    SessionDetail,
    SessionInfo,
    SessionRename,
    TierInfo,
    ToolInfo,
    UploadResult,
)
from agentic_devops.proxy.auth import admin_auth_from_env
from agentic_devops.proxy.documents import DocumentStore, JobStore
from agentic_devops.proxy.secrets import build_secrets_provider, provider_key_refs
from agentic_devops.proxy.docgen_store import DocComponentStore, RepoDocgenStore
from agentic_devops.proxy.github import GitHubAccountStore, RepoCrawlStore
from agentic_devops.proxy.github_client import GitHubClient, GitHubError
from agentic_devops.proxy.host_mcp_client import HostMCPClient
from agentic_devops.proxy.hosts import HostStore
from agentic_devops.proxy.ingest_worker import IngestWorker
from agentic_devops.proxy.mcp_client import MCPManager
from agentic_devops.proxy.sessions import PgSessionStore, generate_title
from agentic_devops.tools.builtin.hosts import build_host_tools
from agentic_devops.tools.builtin.repos import build_repo_tools
from agentic_devops.proxy.tracing import get_tracer
from agentic_devops.tools.builtin import register_builtin_tools
from agentic_devops.tools.router import ToolsRouter

logger = logging.getLogger("agentic_devops")

_STREAM_SENTINEL = object()


def _register_knowledge_tool(router: ToolsRouter, settings: Settings) -> None:
    """Register ``search_knowledge`` whenever knowledge is enabled.

    Always registered (not gated on the store having chunks at startup) so a
    document uploaded through the control plane is immediately searchable without
    a restart. Coverage is computed live in the handler, so an empty KB just
    returns 'no matches' until something is ingested."""
    if not settings.knowledge.enabled:
        return
    try:
        from agentic_devops.knowledge.factory import build_embedder, build_store
        from agentic_devops.knowledge.retrieval import build_search_knowledge_tool

        store = build_store(settings.database)
        embedder = build_embedder(settings.knowledge)
        router.register(build_search_knowledge_tool(store, embedder))
        logger.info("Knowledge retrieval enabled (%s).", store.corpora())
    except Exception as exc:  # noqa: BLE001 — never let knowledge wiring crash the proxy
        logger.warning("Knowledge retrieval not registered: %s", exc)


def _register_recall_tool(router: ToolsRouter, settings: Settings, pool) -> Any:
    """Register ``recall_history`` (retrieval over conversation history) and return
    the memory store (or None if disabled). Registered unconditionally when
    enabled — memories accrue during a session, so recall is useful immediately."""
    if not (settings.knowledge.enabled and settings.knowledge.history_enabled):
        return None
    try:
        from agentic_devops.knowledge.factory import build_embedder
        from agentic_devops.knowledge.history import ConversationMemoryStore
        from agentic_devops.tools.builtin.recall import build_recall_history_tool

        store = ConversationMemoryStore(pool, build_embedder(settings.knowledge))
        router.register(build_recall_history_tool(store))
        logger.info("Conversation recall enabled (recall_history).")
        return store
    except Exception as exc:  # noqa: BLE001
        logger.warning("Recall tool not registered: %s", exc)
        return None


def _register_fact_tools(router: ToolsRouter, settings: Settings) -> None:
    """Register the evolving fact tier's tools — ``recall_facts`` (read) and
    ``memory_add`` (write-back). Always registered when enabled; facts accrue via
    memory_add and are searchable immediately, so no chunks-at-boot gate."""
    if not (settings.knowledge.enabled and settings.knowledge.facts_enabled):
        return
    try:
        from agentic_devops.knowledge.factory import build_fact_store
        from agentic_devops.tools.builtin.facts import (
            build_memory_add_tool,
            build_recall_facts_tool,
        )

        store = build_fact_store(settings)
        router.register(build_recall_facts_tool(store))
        router.register(build_memory_add_tool(store))
        logger.info("Knowledge fact tier enabled (recall_facts, memory_add).")
    except Exception as exc:  # noqa: BLE001 — never let fact wiring crash the proxy
        logger.warning("Fact tools not registered: %s", exc)


def _register_memory_index(router: ToolsRouter, settings: Settings) -> None:
    """Register ``memory_index`` — the orientation map over the KB + fact tier.
    Reads coverage live, so it stays accurate as documents/facts come and go."""
    if not settings.knowledge.enabled:
        return
    try:
        from agentic_devops.knowledge.factory import build_fact_store, build_store
        from agentic_devops.tools.builtin.memory_index import build_memory_index_tool

        store = build_store(settings.database)
        fact_store = build_fact_store(settings) if settings.knowledge.facts_enabled else None
        router.register(build_memory_index_tool(store, fact_store))
        logger.info("Knowledge orientation enabled (memory_index).")
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_index not registered: %s", exc)


def _remember(mem_store, session, user_id, prompt, answer, findings) -> None:
    """Embed one exchange into conversation memory (best-effort)."""
    if mem_store is None:
        return
    turn = max(0, len(session.messages) // 2 - 1)
    text = f"User: {prompt}\nDevy: {answer}"
    if findings:
        evidence = "\n".join(
            f"- {f.get('tool', 'tool')}: {(f.get('result') or '')[:200]}" for f in findings[:5]
        )
        text += f"\n\nEvidence gathered:\n{evidence}"
    try:
        mem_store.add_exchange(session.id, user_id, turn, text)
    except Exception as exc:  # noqa: BLE001 — recall is best-effort, never break a turn
        logger.warning("Conversation memory embed failed: %s", exc)


def create_app(
    settings: Optional[Settings] = None,
    provider: Optional[ProviderClient] = None,
    router: Optional[ToolsRouter] = None,
) -> FastAPI:
    """Build the app. ``provider``/``router`` are injectable for testing."""
    settings = settings or load_settings()

    # Postgres bootstrap. Apply the schema best-effort (a managed DB may need
    # `agentic-devops db init` run by an admin first), then open the shared pool —
    # Postgres is required, so a failure to connect here is fatal by design.
    try:
        apply_schema(settings.database.url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Schema bootstrap skipped (%s); run `agentic-devops db init` if tables are missing.",
            exc,
        )
    pool = get_pool(settings.database.url)

    # Secrets backend (Phase S-1): one AWS SM API surface (LocalStack in dev, real
    # AWS SM in prod). Connector tokens + provider keys resolve through it; nothing
    # secret lives in our DB. Writable only in dev (prod is provisioned out-of-band).
    secrets = build_secrets_provider(settings)
    secrets_writable = secrets.writable

    # Provider/LLM keys (Anthropic, OpenAI) live in the same store; hydrate them into
    # os.environ so LiteLLM finds them. Non-clobbering: a key already in the env
    # (e.g. from a transitional .env) wins, so this never regresses a working stack.
    for _ref, _env_var in provider_key_refs(settings.secrets.namespace).items():
        if not os.environ.get(_env_var):
            _val = secrets.get(_ref)
            if _val:
                os.environ[_env_var] = _val
                logger.info("hydrated %s from the secrets manager", _env_var)

    # Host registry (control plane): secrets-backed token store + on-demand MCP caller.
    host_store = HostStore(pool, secrets)
    host_mcp = HostMCPClient()

    # GitHub connector (Phase D-1): account registry (secrets-backed PAT) + client.
    github_store = GitHubAccountStore(pool, secrets)

    # MCP Servers registry (Phase S-4): general HTTP MCP tool sources, called
    # on-demand via the same client as hosts; tools normalized into the router.
    from agentic_devops.proxy.mcp_registry import MCPServerStore
    from agentic_devops.tools.mcp_source import build_server_tools

    mcp_server_store = MCPServerStore(pool, secrets, namespace=settings.secrets.namespace)
    repo_crawl_store = RepoCrawlStore(pool)
    repo_docgen_store = RepoDocgenStore(pool)
    doc_component_store = DocComponentStore(pool)
    github_client = GitHubClient()

    # Document import (control plane): registry + jobs + the in-process ingest
    # worker. Built from the shared pool; the worker shares the knowledge store /
    # embedder / (optional) enricher with the search path.
    from agentic_devops.knowledge.factory import build_embedder, build_enricher, build_redactor
    from agentic_devops.knowledge.store import PgVectorStore

    document_store = DocumentStore(pool)
    job_store = JobStore(pool)
    kb_store = PgVectorStore(pool)
    # Secret redaction (Phase C): applied to uploads before content is persisted.
    redactor = build_redactor(settings.knowledge)
    kcfg = settings.knowledge.chunk
    ingest_worker = IngestWorker(
        document_store, job_store, kb_store, build_embedder(settings.knowledge),
        build_enricher(settings), split_level=kcfg.split_level,
        max_chars=kcfg.max_chars, overlap=kcfg.overlap,
    )

    mcp_manager: Optional[MCPManager] = None
    if router is None:
        router = ToolsRouter()
        register_builtin_tools(router, audit_path=settings.trace_dir / "diagnostics-audit.jsonl")
        if settings.mcp_servers:
            mcp_manager = MCPManager(settings.mcp_servers)
            mcp_manager.start()
            for spec in mcp_manager.tool_specs():
                try:
                    router.register(spec)
                except ValueError:
                    logger.warning("MCP tool name clash, skipping: %s", spec.name)
            for err in mcp_manager.errors:
                logger.warning("MCP mount issue: %s", err)

        _register_knowledge_tool(router, settings)
        _register_fact_tools(router, settings)
        _register_memory_index(router, settings)
        mem_store = _register_recall_tool(router, settings, pool)
        for spec in build_host_tools(host_store, host_mcp):
            try:
                router.register(spec)
            except ValueError:
                logger.warning("host tool name clash, skipping: %s", spec.name)
        for spec in build_repo_tools(github_store, github_client):
            try:
                router.register(spec)
            except ValueError:
                logger.warning("repo tool name clash, skipping: %s", spec.name)
    else:
        mem_store = None

    # MCP Servers registry (Phase S-4): mount/refresh a registered server's tools
    # into the live router. Names can't collide with built-in categories.
    RESERVED_CATEGORIES = {"knowledge", "memory", "diagnostics", "hosts", "repos", "mcp"}

    def _mount_mcp_server(server: Any) -> tuple[str, int, int]:
        """(status, tool_count, write_count). Withdraws the server's existing tools
        first, so this doubles as refresh; best-effort on an unreachable server."""
        router.unregister_category(server.tool_category)
        resolved = mcp_server_store.resolve(server.id)
        if resolved is None:
            return "unknown", 0, 0
        detail = host_mcp.list_tools_detail(resolved.url, resolved.token)
        if not detail:
            return "unreachable", 0, 0
        specs, write_count = build_server_tools(server, detail, store=mcp_server_store, caller=host_mcp)
        for spec in specs:
            router.register_or_replace(spec)
        return "reachable", len(detail), write_count

    provider = provider or ProviderClient(request_timeout=settings.request_timeout)
    sessions = PgSessionStore(pool)
    tracer = get_tracer(settings)
    admin = admin_auth_from_env()

    app = FastAPI(title="Agentic DevOps — LLM-PROXY", version=__version__)

    if mcp_manager is not None:
        @app.on_event("shutdown")
        def _shutdown_mcp() -> None:
            mcp_manager.shutdown()

    # Ingest worker: fail anything orphaned mid-flight by a crashed run, then
    # start the background drain loop. Stored on app.state for tests.
    app.state.ingest_worker = ingest_worker
    app.state.document_store = document_store
    app.state.job_store = job_store

    @app.on_event("startup")
    def _start_ingest_worker() -> None:
        try:
            document_store.reconcile()
            job_store.reconcile()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ingest reconcile skipped (%s)", exc)
        ingest_worker.start()

    @app.on_event("startup")
    def _mount_registered_mcp_servers() -> None:
        try:
            for s in mcp_server_store.list(enabled_only=True):
                status, tc, wc = _mount_mcp_server(s)
                mcp_server_store.set_health(s.id, status, tc, wc)
        except Exception as exc:  # noqa: BLE001 — never block startup on MCP mounts
            logger.warning("MCP registry mount skipped (%s)", exc)

    @app.on_event("shutdown")
    def _stop_ingest_worker() -> None:
        ingest_worker.stop()

    @app.get("/healthz", response_model=HealthInfo)
    def healthz() -> HealthInfo:
        return HealthInfo(status="ok", version=__version__, default_tier=settings.default_tier)

    @app.get("/v1/tiers", response_model=list[TierInfo])
    def tiers() -> list[TierInfo]:
        # Labels only — the concrete model behind each tier is intentionally hidden.
        return [TierInfo(name=name, label=t.display()) for name, t in settings.tiers.items()]

    @app.get("/v1/tools", response_model=list[ToolInfo])
    def tools() -> list[ToolInfo]:
        return [
            ToolInfo(
                name=s.name,
                category=s.category,
                when_to_use=s.when_to_use,
                safety_tier=s.safety_tier,
            )
            for s in router.all_specs()
        ]

    def _resolve_tier(name: Optional[str]):
        try:
            return settings.resolve_tier(name)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- admin control plane (Phase 9) ----------------------------------------
    def require_admin(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
        """Dependency guarding /v1/admin/* — the seam where SSO drops in later."""
        if not admin.enabled:
            raise HTTPException(status_code=503, detail="admin plane is not configured")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing admin token")
        try:
            return admin.verify_token(authorization.split(" ", 1)[1].strip())
        except Exception as exc:  # noqa: BLE001 — any decode/expiry failure is a 401
            raise HTTPException(status_code=401, detail="invalid or expired admin token") from exc

    @app.post("/v1/admin/login", response_model=AdminToken)
    def admin_login(body: AdminLogin) -> AdminToken:
        if not admin.enabled:
            raise HTTPException(status_code=503, detail="admin plane is not configured")
        if not admin.verify_password(body.password):
            raise HTTPException(status_code=401, detail="invalid credentials")
        token, ttl = admin.issue_token()
        return AdminToken(token=token, expires_in=ttl)

    @app.get("/v1/admin/me")
    def admin_me(principal: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
        return {"authenticated": True, "sub": principal.get("sub"), "scope": principal.get("scope")}

    # ---- host registry (admin) ----
    @app.get("/v1/admin/hosts", response_model=list[HostInfo])
    def list_hosts(_: dict = Depends(require_admin)) -> list[HostInfo]:
        return [HostInfo(**asdict(h)) for h in host_store.list()]

    @app.post("/v1/admin/hosts", response_model=HostInfo, status_code=201)
    def create_host(body: HostCreate, _: dict = Depends(require_admin)) -> HostInfo:
        if body.token and not secrets_writable:
            raise HTTPException(
                status_code=403,
                detail="secrets are read-only in prod mode; provision the token out-of-band",
            )
        try:
            host = host_store.create(body.model_dump(exclude={"token"}), token=body.token)
        except Exception as exc:  # noqa: BLE001 — most likely a duplicate FQDN
            raise HTTPException(status_code=409, detail=f"could not create host: {exc}") from exc
        return HostInfo(**asdict(host))

    @app.get("/v1/admin/hosts/{host_id}", response_model=HostInfo)
    def get_host(host_id: str, _: dict = Depends(require_admin)) -> HostInfo:
        host = host_store.get(host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        return HostInfo(**asdict(host))

    @app.patch("/v1/admin/hosts/{host_id}", response_model=HostInfo)
    def update_host(host_id: str, body: HostUpdate, _: dict = Depends(require_admin)) -> HostInfo:
        set_token = "token" in body.model_fields_set
        if set_token and not secrets_writable:
            raise HTTPException(
                status_code=403,
                detail="secrets are read-only in prod mode; provision the token out-of-band",
            )
        data = body.model_dump(exclude={"token"}, exclude_unset=True)
        host = host_store.update(host_id, data, token=body.token, set_token=set_token)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        return HostInfo(**asdict(host))

    @app.delete("/v1/admin/hosts/{host_id}")
    def delete_host(host_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        host_store.delete(host_id)
        return {"id": host_id, "deleted": True}

    @app.post("/v1/admin/hosts/{host_id}/check")
    async def check_host(host_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        host = host_store.get(host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        resolved = host_store.resolve(host.id, active_only=False)
        checks = await run_in_threadpool(host_mcp.list_tools, resolved.url, resolved.token)
        status = "reachable" if checks else "unreachable"
        await run_in_threadpool(host_store.set_status, host_id, status)
        return {"status": status, "checks": checks}

    @app.get("/v1/admin/mcp-mounts")
    async def list_mounted_hosts(_: dict = Depends(require_admin)) -> list[dict[str, Any]]:
        """Statically-mounted MCP servers from config (`mcp_servers`) — read-only,
        non-removable 'built-in' hosts. Always includes the local host MCP that ships
        on Devy's own Docker network (a guaranteed host to test against), shown as a
        reference example alongside the editable DB-registered hosts."""
        from urllib.parse import urlparse

        out: list[dict[str, Any]] = []
        for s in settings.mcp_servers:
            address, reachable, checks = "", None, 0
            if s.transport == "http" and s.url:
                address = urlparse(s.url).netloc or s.url
                try:
                    found = await run_in_threadpool(host_mcp.list_tools, s.url, s.token)
                    reachable, checks = bool(found), len(found)
                except Exception:  # noqa: BLE001 — unreachable is a state, not an error
                    reachable = False
            elif s.transport == "stdio":
                address = s.command or "(stdio)"
            out.append({
                "name": s.name, "transport": s.transport, "address": address,
                "url": s.url, "reachable": reachable, "checks": checks,
            })
        return out

    # ---- MCP Servers registry (admin, Phase S-4) ----
    @app.get("/v1/admin/mcp-servers", response_model=list[MCPServerInfo])
    def list_mcp_servers(_: dict = Depends(require_admin)) -> list[MCPServerInfo]:
        return [MCPServerInfo(**asdict(s)) for s in mcp_server_store.list()]

    @app.post("/v1/admin/mcp-servers", response_model=MCPServerInfo, status_code=201)
    async def create_mcp_server(body: MCPServerCreate, _: dict = Depends(require_admin)) -> MCPServerInfo:
        if body.name.lower() in RESERVED_CATEGORIES or (body.category or "").lower() in RESERVED_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"name/category collides with a built-in ({', '.join(sorted(RESERVED_CATEGORIES))})")
        try:
            server = mcp_server_store.create(body.model_dump())
        except Exception as exc:  # noqa: BLE001 — most likely a duplicate name
            raise HTTPException(status_code=409, detail=f"could not create MCP server: {exc}") from exc
        if server.enabled:
            status, tc, wc = await run_in_threadpool(_mount_mcp_server, server)
            mcp_server_store.set_health(server.id, status, tc, wc)
            server = mcp_server_store.get(server.id)
        return MCPServerInfo(**asdict(server))

    @app.patch("/v1/admin/mcp-servers/{server_id}", response_model=MCPServerInfo)
    async def update_mcp_server(server_id: str, body: MCPServerUpdate, _: dict = Depends(require_admin)) -> MCPServerInfo:
        if (body.category or "").lower() in RESERVED_CATEGORIES:
            raise HTTPException(status_code=400, detail="category collides with a built-in")
        prev = mcp_server_store.get(server_id)
        if prev is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        server = mcp_server_store.update(server_id, body.model_dump(exclude_unset=True))
        # Re-mount if enabled (tools may have changed); withdraw if disabled.
        if server.enabled:
            status, tc, wc = await run_in_threadpool(_mount_mcp_server, server)
            mcp_server_store.set_health(server.id, status, tc, wc)
        else:
            await run_in_threadpool(router.unregister_category, prev.tool_category)
            mcp_server_store.set_health(server.id, "disabled", 0, server.write_tool_count)
        return MCPServerInfo(**asdict(mcp_server_store.get(server_id)))

    @app.delete("/v1/admin/mcp-servers/{server_id}")
    async def delete_mcp_server(server_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        removed = mcp_server_store.delete(server_id)
        if removed is not None:
            await run_in_threadpool(router.unregister_category, removed.tool_category)
        return {"id": server_id, "deleted": True}

    @app.post("/v1/admin/mcp-servers/{server_id}/test")
    async def test_mcp_server(server_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        resolved = await run_in_threadpool(mcp_server_store.resolve, server_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        checks = await run_in_threadpool(host_mcp.list_tools, resolved.url, resolved.token)
        status = "reachable" if checks else "unreachable"
        return {"status": status, "checks": checks}

    @app.post("/v1/admin/mcp-servers/{server_id}/refresh", response_model=MCPServerInfo)
    async def refresh_mcp_server(server_id: str, _: dict = Depends(require_admin)) -> MCPServerInfo:
        server = mcp_server_store.get(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        status, tc, wc = await run_in_threadpool(_mount_mcp_server, server)
        mcp_server_store.set_health(server.id, status, tc, wc)
        return MCPServerInfo(**asdict(mcp_server_store.get(server_id)))

    # ---- GitHub connector (admin, Phase D-1) ----
    @app.get("/v1/admin/github/accounts", response_model=list[GitHubAccountInfo])
    def list_github_accounts(_: dict = Depends(require_admin)) -> list[GitHubAccountInfo]:
        return [GitHubAccountInfo(**asdict(a)) for a in github_store.list()]

    @app.post("/v1/admin/github/accounts", response_model=GitHubAccountInfo, status_code=201)
    def create_github_account(body: GitHubAccountCreate, _: dict = Depends(require_admin)) -> GitHubAccountInfo:
        if body.token and not secrets_writable:
            raise HTTPException(
                status_code=403,
                detail="secrets are read-only in prod mode; provision the PAT out-of-band",
            )
        try:
            account = github_store.create(body.model_dump(exclude={"token"}), token=body.token)
        except Exception as exc:  # noqa: BLE001 — most likely a duplicate label
            raise HTTPException(status_code=409, detail=f"could not create account: {exc}") from exc
        return GitHubAccountInfo(**asdict(account))

    @app.patch("/v1/admin/github/accounts/{account_id}", response_model=GitHubAccountInfo)
    def update_github_account(
        account_id: str, body: GitHubAccountUpdate, _: dict = Depends(require_admin)
    ) -> GitHubAccountInfo:
        set_token = "token" in body.model_fields_set
        if set_token and not secrets_writable:
            raise HTTPException(
                status_code=403,
                detail="secrets are read-only in prod mode; provision the PAT out-of-band",
            )
        data = body.model_dump(exclude={"token"}, exclude_unset=True)
        account = github_store.update(account_id, data, token=body.token, set_token=set_token)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        return GitHubAccountInfo(**asdict(account))

    @app.delete("/v1/admin/github/accounts/{account_id}")
    def delete_github_account(account_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        github_store.delete(account_id)
        return {"id": account_id, "deleted": True}

    @app.post("/v1/admin/github/accounts/{account_id}/test")
    async def test_github_account(account_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        resolved = await run_in_threadpool(github_store.resolve, account_id, False)
        if resolved is None or not resolved.token:
            raise HTTPException(status_code=404, detail="account not found or has no token")
        try:
            user = await run_in_threadpool(github_client.whoami, resolved.token)
        except GitHubError as exc:
            await run_in_threadpool(github_store.touch, account_id, "unauthorized")
            return {"ok": False, "error": str(exc)}
        login = user.get("login")
        await run_in_threadpool(github_store.touch, account_id, "ok")
        if login:
            await run_in_threadpool(
                github_store.update, account_id, {"login": login}, None, False
            )
        return {"ok": True, "login": login}

    # ---- Secrets / Connections (Phase S-2) ----
    from agentic_devops.proxy import secrets_catalog as _sc

    _ns = settings.secrets.namespace
    _env_by_ref = provider_key_refs(_ns)

    def _secret_category(ref: str) -> str:
        if ref in _sc.settable_refs(_ns):
            return "provider"
        if ref.startswith(f"{_ns}/github/"):
            return "github"
        if ref.startswith(f"{_ns}/host/"):
            return "host"
        if ref.startswith(f"{_ns}/mcp/"):
            return "mcp"
        return "unknown"

    def _known_refs() -> set[str]:
        """Refs the Secrets tab may write: provider keys + any connector's secret_ref
        (so we never write an arbitrary/unknown name)."""
        refs = set(_sc.settable_refs(_ns))
        refs |= {a.secret_ref for a in github_store.list() if a.secret_ref}
        refs |= {h.secret_ref for h in host_store.list() if h.secret_ref}
        refs |= {m.secret_ref for m in mcp_server_store.list() if m.secret_ref}
        return refs

    def _entry_for(ref: str) -> SecretEntry:
        for e in _sc.build_catalog(_ns, secrets, github_store, host_store, mcp_server_store):
            if e["ref"] == ref:
                return SecretEntry(**e)
        raise HTTPException(status_code=404, detail="unknown secret")

    @app.get("/v1/admin/secrets", response_model=SecretCatalog)
    def list_secrets(_: dict = Depends(require_admin)) -> SecretCatalog:
        entries = _sc.build_catalog(_ns, secrets, github_store, host_store, mcp_server_store)
        return SecretCatalog(
            mode=settings.secrets.mode, writable=secrets.writable,
            reachable=secrets.health(),
            secrets=[SecretEntry(**e) for e in entries],
        )

    @app.put("/v1/admin/secrets", response_model=SecretEntry)
    def set_secret(body: SecretSet, _: dict = Depends(require_admin)) -> SecretEntry:
        if not secrets.writable:
            raise HTTPException(status_code=403, detail="secrets are read-only in prod mode")
        if body.ref not in _known_refs():
            raise HTTPException(
                status_code=400,
                detail="unknown secret ref (register the account/host first on its tab)",
            )
        if not body.value.strip():
            raise HTTPException(status_code=400, detail="value is required")
        secrets.set(body.ref, body.value)
        # Provider keys hydrate an env var so the change takes effect without a
        # restart; connector tokens are resolved on-demand (no env var).
        env = _env_by_ref.get(body.ref)
        if env:
            os.environ[env] = body.value
        return _entry_for(body.ref)

    @app.delete("/v1/admin/secrets")
    def delete_secret(ref: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        if not secrets.writable:
            raise HTTPException(status_code=403, detail="secrets are read-only in prod mode")
        if ref not in _known_refs():
            raise HTTPException(status_code=400, detail="unknown secret ref")
        secrets.delete(ref)
        env = _env_by_ref.get(ref)
        if env:
            os.environ.pop(env, None)
        return {"ref": ref, "deleted": True}

    @app.post("/v1/admin/secrets/test", response_model=SecretTestResult)
    async def test_secret(body: SecretRefBody, _: dict = Depends(require_admin)) -> SecretTestResult:
        ref = body.ref
        category = _secret_category(ref)
        value = await run_in_threadpool(secrets.get, ref)
        if not value:
            return SecretTestResult(ok=False, detail="not set")
        if category == "provider":
            service = ref.rsplit("/", 1)[-1]
            ok, detail = await run_in_threadpool(_sc.probe_provider, service, value)
        elif category == "github":
            ok, detail = await run_in_threadpool(_sc.probe_github, github_client, value)
        elif category == "host":
            host = next((h for h in host_store.list() if h.secret_ref == ref), None)
            if host is None:
                return SecretTestResult(ok=False, detail="no host bound to this secret")
            resolved = await run_in_threadpool(host_store.resolve, host.id, False)
            checks = await run_in_threadpool(host_mcp.list_tools, resolved.url, resolved.token)
            ok, detail = (bool(checks), f"{len(checks)} checks available" if checks else "unreachable")
        elif category == "mcp":
            server = next((m for m in mcp_server_store.list() if m.secret_ref == ref), None)
            if server is None:
                return SecretTestResult(ok=False, detail="no MCP server bound to this secret")
            checks = await run_in_threadpool(host_mcp.list_tools, server.url, value)
            ok, detail = (bool(checks), f"{len(checks)} tools available" if checks else "unreachable")
        else:
            return SecretTestResult(ok=False, detail="unknown secret category")
        secrets.audit("test", ref, ok, actor="admin", detail=detail[:120])
        return SecretTestResult(ok=ok, detail=detail)

    @app.get("/v1/admin/github/repos")
    async def list_github_repos(
        account: Optional[str] = None, _: dict = Depends(require_admin)
    ) -> list[dict[str, Any]]:
        resolved = await run_in_threadpool(github_store.resolve, account, True)
        if resolved is None or not resolved.token:
            raise HTTPException(status_code=404, detail="no matching account (name one if several)")
        try:
            repos = await run_in_threadpool(github_client.list_repos, resolved.token)
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [
            {"full_name": r.get("full_name"), "private": r.get("private"),
             "language": r.get("language"), "description": r.get("description"),
             "pushed_at": r.get("pushed_at")}
            for r in repos
        ]

    @app.post("/v1/admin/github/crawl", response_model=RepoCrawlResult)
    async def crawl_github_repo(body: RepoCrawlRequest, _: dict = Depends(require_admin)) -> RepoCrawlResult:
        from agentic_devops.proxy.github_crawl import crawl_repo_markdown

        resolved = await run_in_threadpool(
            github_store.resolve_for_repo, body.repo
        ) if not body.account else await run_in_threadpool(github_store.resolve, body.account, True)
        if resolved is None or not resolved.token:
            raise HTTPException(status_code=404, detail="no matching GitHub account for this repo")
        corpus = body.corpus or resolved.account.default_corpus or body.repo
        try:
            outcome = await run_in_threadpool(
                lambda: crawl_repo_markdown(
                    github_client, resolved.token, body.repo,
                    store=kb_store, embedder=build_embedder(settings.knowledge),
                    corpus=corpus, redactor=redactor, enricher=build_enricher(settings),
                    document_store=document_store, max_chars=kcfg.max_chars,
                    overlap=kcfg.overlap, split_level=kcfg.split_level,
                )
            )
        except GitHubError as exc:
            await run_in_threadpool(github_store.touch, resolved.account.id, "error")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await run_in_threadpool(github_store.touch, resolved.account.id, "ok")
        stats = outcome.stats
        await run_in_threadpool(
            lambda: repo_crawl_store.record(
                body.repo, stats.corpus, account_id=resolved.account.id,
                commit_sha=outcome.commit_sha, default_branch=outcome.ref,
                files_ingested=stats.files_ingested, chunks_written=stats.chunks_written,
                files_quarantined=stats.files_quarantined, secrets_redacted=stats.secrets_redacted,
            )
        )
        return RepoCrawlResult(
            corpus=stats.corpus, files_ingested=stats.files_ingested,
            files_skipped=stats.files_skipped, files_quarantined=stats.files_quarantined,
            chunks_written=stats.chunks_written, secrets_redacted=stats.secrets_redacted,
            commit_sha=outcome.commit_sha, default_branch=outcome.ref,
        )

    # ---- Doc generation (Phase D-2) ----
    def _docgen_info(rec: Any, components: list[Any]) -> DocgenInfo:
        return DocgenInfo(
            full_name=rec.full_name, last_doc_sha=rec.last_doc_sha,
            default_branch=rec.default_branch, scan_brief=rec.scan_brief,
            components_doced=rec.components_doced, status=rec.status,
            last_run_at=rec.last_run_at, error=rec.error,
            components=[
                DocgenComponentInfo(
                    component_path=c.component_path, component_name=c.component_name,
                    kind=c.kind, status=c.status, arch_doc_path=c.arch_doc_path,
                    last_doc_sha=c.last_doc_sha,
                )
                for c in components
            ],
        )

    @app.get("/v1/admin/github/docgen", response_model=list[DocgenInfo])
    def list_docgen(_: dict = Depends(require_admin)) -> list[DocgenInfo]:
        return [
            _docgen_info(rec, doc_component_store.list(rec.full_name))
            for rec in repo_docgen_store.list()
        ]

    @app.put("/v1/admin/github/docgen/brief", response_model=DocgenInfo)
    def set_docgen_brief(body: DocgenBrief, _: dict = Depends(require_admin)) -> DocgenInfo:
        rec = repo_docgen_store.set_brief(body.repo, body.brief)
        return _docgen_info(rec, doc_component_store.list(rec.full_name))

    @app.post("/v1/admin/github/docgen")
    async def trigger_docgen(body: DocgenTriggerRequest, _: dict = Depends(require_admin)) -> dict[str, Any]:
        from datetime import datetime, timezone
        from pathlib import Path

        from agentic_devops.proxy.docgen_run import run_docgen

        if not settings.knowledge.docgen_enabled:
            raise HTTPException(status_code=400, detail="doc generation is disabled (knowledge.docgen_enabled)")
        resolved = await run_in_threadpool(github_store.resolve_for_repo, body.repo)
        if resolved is None or not resolved.token:
            raise HTTPException(status_code=404, detail="no matching GitHub account for this repo")
        try:
            tier = settings.resolve_tier(settings.knowledge.docgen_tier)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.brief is not None:
            await run_in_threadpool(repo_docgen_store.set_brief, body.repo, body.brief)
        token = resolved.token
        account_id = resolved.account.id

        # Generation is many sequential model calls — run it in the background and
        # let the UI poll GET /docgen (status running → idle); never block the request.
        await run_in_threadpool(repo_docgen_store.set_status, body.repo, "running")

        def work() -> None:
            try:
                run_docgen(
                    github_client, token, body.repo,
                    repo_store=repo_docgen_store, component_store=doc_component_store,
                    kb_store=kb_store, embedder=build_embedder(settings.knowledge),
                    provider=provider, tier=tier,
                    output_dir=Path(settings.knowledge.docgen_output_dir),
                    generated_at=datetime.now(timezone.utc).isoformat(),
                    redactor=redactor, enricher=build_enricher(settings),
                    document_store=document_store, only=body.components or None,
                    max_files=settings.knowledge.docgen_max_files, force=body.force,
                )
                github_store.touch(account_id, "ok")
            except Exception:  # run_docgen records error status itself; flag the account
                github_store.touch(account_id, "error")

        threading.Thread(target=work, daemon=True).start()
        return {"repo": body.repo, "started": True}

    @app.get("/v1/admin/github/crawls", response_model=list[RepoCrawlInfo])
    def list_repo_crawls(_: dict = Depends(require_admin)) -> list[RepoCrawlInfo]:
        # Show the current KB footprint per corpus (live counts), not last-run
        # deltas — a re-crawl re-ingests only changed files, so files_ingested
        # would otherwise read as a shrunken total.
        doc_counts = document_store.corpora()
        chunk_counts = kb_store.corpora()
        return [
            RepoCrawlInfo(
                **asdict(c),
                doc_count=doc_counts.get(c.corpus, 0),
                chunk_count=chunk_counts.get(c.corpus, 0),
            )
            for c in repo_crawl_store.list()
        ]

    # -- Documents / knowledge import (Phase 9c-2) --------------------------
    def _doc_info(doc: Any) -> DocumentInfo:
        return DocumentInfo(**{k: getattr(doc, k) for k in DocumentInfo.model_fields})

    def _job_info(job: Any) -> JobInfo:
        return JobInfo(**{k: getattr(job, k) for k in JobInfo.model_fields})

    @app.get("/v1/admin/documents", response_model=list[DocumentInfo])
    def list_documents(corpus: Optional[str] = None, _: dict = Depends(require_admin)) -> list[DocumentInfo]:
        return [_doc_info(d) for d in document_store.list(corpus)]

    @app.post("/v1/admin/documents", response_model=UploadResult, status_code=201)
    async def upload_documents(
        corpus: str = Form(...),
        files: list[UploadFile] = File(...),
        principal: dict[str, Any] = Depends(require_admin),
    ) -> UploadResult:
        from agentic_devops.knowledge.enrich import doc_title, doc_type
        from agentic_devops.knowledge.ingest import content_hash

        corpus = (corpus or "").strip()
        if not corpus:
            raise HTTPException(status_code=400, detail="corpus is required")

        from agentic_devops.knowledge.redaction import apply_redaction

        uploaded_by = str(principal.get("sub") or "admin")
        accepted: list[tuple[str, str, int]] = []
        quarantined: list = []  # registered failed, never stored unredacted
        for f in files:
            name = (f.filename or "").strip()
            if not name.lower().endswith((".md", ".markdown")):
                continue  # markdown-only
            raw = await f.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            # Redact BEFORE persisting: a quarantined upload is recorded failed and
            # its content is never stored; otherwise we keep the redacted text.
            redacted, red = apply_redaction(text, redactor)
            if redacted is None:
                doc = await run_in_threadpool(
                    lambda n=name: document_store.register(
                        corpus, n, title=n, doc_type="doc", content="", content_hash="",
                        bytes_=0, uploaded_by=uploaded_by, status="failed",
                    )
                )
                await run_in_threadpool(
                    document_store.set_status, doc.id, "failed",
                    error=f"quarantined: suspected secret ({red.summary})",
                )
                quarantined.append(await run_in_threadpool(document_store.by_source, corpus, name))
                continue
            accepted.append((name, redacted, len(redacted.encode("utf-8"))))
        if not accepted and not quarantined:
            raise HTTPException(status_code=400, detail="no valid markdown files (.md/.markdown)")

        created = list(quarantined)
        if accepted:
            job = await run_in_threadpool(job_store.create, corpus, len(accepted))
            for name, text, nbytes in accepted:
                doc = await run_in_threadpool(
                    lambda n=name, t=text, b=nbytes: document_store.register(
                        corpus, n, title=doc_title(t, fallback=n), doc_type=doc_type(n, t),
                        content=t, content_hash=content_hash(t), bytes_=b,
                        uploaded_by=uploaded_by, status="pending", job_id=job.id,
                    )
                )
                created.append(doc)
            ingest_worker.notify()
            job_info = _job_info(job)
        else:
            # Everything quarantined — no work to enqueue.
            job = await run_in_threadpool(job_store.create, corpus, 0)
            await run_in_threadpool(job_store.set_status, job.id, "done")
            job_info = _job_info(await run_in_threadpool(job_store.get, job.id))
        return UploadResult(job=job_info, documents=[_doc_info(d) for d in created if d])

    @app.get("/v1/admin/jobs/{job_id}", response_model=JobInfo)
    def get_job(job_id: str, _: dict = Depends(require_admin)) -> JobInfo:
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_info(job)

    @app.delete("/v1/admin/documents/{document_id}")
    def delete_document(document_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        ok = document_store.delete(document_id)
        if not ok:
            raise HTTPException(status_code=404, detail="document not found")
        return {"id": document_id, "deleted": True}

    @app.get("/v1/admin/corpora", response_model=list[CorpusInfo])
    def list_corpora(_: dict = Depends(require_admin)) -> list[CorpusInfo]:
        doc_counts = document_store.corpora()
        chunk_counts = kb_store.corpora()
        names = sorted(set(doc_counts) | set(chunk_counts))
        return [
            CorpusInfo(name=n, documents=doc_counts.get(n, 0), chunks=chunk_counts.get(n, 0))
            for n in names
        ]

    @app.delete("/v1/admin/corpora/{corpus}")
    def delete_corpus(corpus: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
        removed = document_store.delete_corpus(corpus)
        return {"corpus": corpus, "documents_deleted": removed}

    @app.get("/v1/sessions", response_model=list[SessionInfo])
    def list_sessions(
        user_id: Optional[str] = None,
        x_user_id: Optional[str] = Header(default=None),
    ) -> list[SessionInfo]:
        uid = user_id or x_user_id
        if not uid:
            raise HTTPException(status_code=400, detail="user_id is required to list sessions")
        return [
            SessionInfo(
                id=s.id, user_id=s.user_id, title=s.title, updated_at=s.updated_at,
                turns=s.turns, preview=s.preview,
            )
            for s in sessions.list_for_user(uid)
        ]

    @app.get("/v1/sessions/{session_id}", response_model=SessionDetail)
    def get_session(session_id: str) -> SessionDetail:
        # The faithful display transcript (prompt + final answers) — not Devy's
        # internal context channel.
        session = sessions.load(session_id)
        if not session.messages:
            raise HTTPException(status_code=404, detail="session not found")
        return SessionDetail(
            id=session.id, user_id=session.user_id, title=session.title,
            messages=session.messages,
        )

    @app.patch("/v1/sessions/{session_id}")
    async def rename_session(session_id: str, body: SessionRename) -> dict[str, Any]:
        title = body.title.strip()[:80]
        if not title:
            raise HTTPException(status_code=400, detail="title must not be empty")
        await run_in_threadpool(sessions.rename, session_id, title)
        return {"id": session_id, "title": title}

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        await run_in_threadpool(sessions.delete, session_id)
        if mem_store is not None:
            await run_in_threadpool(mem_store.delete_session, session_id)
        return {"id": session_id, "deleted": True}

    @app.post("/v1/complete", response_model=CompleteResponse)
    async def complete(
        req: CompleteRequest,
        x_user_id: Optional[str] = Header(default=None),
    ) -> CompleteResponse:
        tier = _resolve_tier(req.tier)
        user_id = req.user_id or x_user_id
        session = sessions.load(req.session_id, user_id=user_id)
        if req.session_id:
            await run_in_threadpool(
                sessions.compact_if_needed, session, provider, tier, settings
            )
        messages = assemble_messages(session, req.prompt, req.context, req.system)

        result = await run_in_threadpool(
            run_turn,
            provider,
            router,
            settings,
            messages,
            tier,
            lambda e: tracer.event(session.id, e),
            {"user_id": user_id, "session_id": session.id},
        )

        text = result.text
        if req.max_chars and len(text) > req.max_chars:
            text = text[: req.max_chars].rstrip() + "…"

        if req.session_id:
            session.add_user(req.prompt)
            session.add_assistant(result.text)
            session.add_findings(result.tool_findings, settings.tool_finding_max_chars)
            if not session.title and len(session.messages) >= 2:
                session.title = await run_in_threadpool(
                    generate_title, provider, settings, req.prompt, result.text
                )
            await run_in_threadpool(sessions.save, session)
            await run_in_threadpool(
                _remember, mem_store, session, user_id, req.prompt, result.text, result.tool_findings
            )

        return CompleteResponse(
            markdown=text,
            tools_used=result.tools_used,
            usage=result.usage,
            session_id=session.id if req.session_id else None,
        )

    @app.post("/v1/chat")
    async def chat(
        req: ChatRequest,
        x_user_id: Optional[str] = Header(default=None),
    ):
        tier = _resolve_tier(req.tier)
        user_id = req.user_id or x_user_id
        session = sessions.load(req.session_id, user_id=user_id)
        await run_in_threadpool(sessions.compact_if_needed, session, provider, tier, settings)
        messages = assemble_messages(session, req.message, req.context)

        async def event_stream():
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            result_holder: dict[str, Any] = {}

            def worker() -> None:
                gen = run_turn_streaming(
                    provider, router, settings, messages, tier,
                    {"user_id": user_id, "session_id": session.id},
                )
                try:
                    while True:
                        event = next(gen)
                        tracer.event(session.id, event)
                        loop.call_soon_threadsafe(queue.put_nowait, event)
                except StopIteration as stop:
                    result_holder["result"] = stop.value
                except Exception as exc:  # surface as an error event
                    loop.call_soon_threadsafe(
                        queue.put_nowait, {"type": "error", "message": str(exc)}
                    )
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_SENTINEL)

            threading.Thread(target=worker, daemon=True).start()

            # Tell the client its session id up front.
            yield {"event": "session", "data": json.dumps({"session_id": session.id})}

            while True:
                event = await queue.get()
                if event is _STREAM_SENTINEL:
                    break
                yield {"event": event["type"], "data": json.dumps(event, default=str)}

            result = result_holder.get("result")
            if result is not None:
                session.add_user(req.message)
                session.add_assistant(result.text)
                session.add_findings(result.tool_findings, settings.tool_finding_max_chars)
                if not session.title and len(session.messages) >= 2:
                    session.title = await run_in_threadpool(
                        generate_title, provider, settings, req.message, result.text
                    )
                await run_in_threadpool(sessions.save, session)
                await run_in_threadpool(
                    _remember, mem_store, session, user_id, req.message, result.text,
                    result.tool_findings,
                )

        return EventSourceResponse(event_stream())

    return app
