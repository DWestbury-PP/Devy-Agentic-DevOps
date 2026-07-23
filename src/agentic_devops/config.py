"""Configuration for the Agentic DevOps proxy.

Two layers, in increasing precedence:

1. Built-in defaults (below) — enough to run out of the box if a provider key
   is present in the environment.
2. An operator config file (YAML) — the authoritative place to define the
   model *tiers* (``fast`` / ``balanced`` / ``deep``) and their concrete models.
   Located at ``$AGENTIC_DEVOPS_HOME/config.yaml`` (default
   ``~/.config/agentic-devops/config.yaml``) or the path in
   ``$AGENTIC_DEVOPS_CONFIG``.

Scalar settings may additionally be overridden by ``AGENTIC_DEVOPS_*`` env vars.

The key design point: end users select a *tier*, never a concrete model. The
mapping from tier to model is an operator decision (cost, security posture,
preference). See docs/JOURNEY.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TierName = Literal["fast", "balanced", "deep"]


def _home() -> Path:
    return Path(
        os.environ.get("AGENTIC_DEVOPS_HOME", Path.home() / ".config" / "agentic-devops")
    ).expanduser()


class ModelTier(BaseModel):
    """An operator-defined model profile that a user selects by name."""

    model: str  # LiteLLM model string, e.g. "anthropic/claude-...", "ollama/llama3.1"
    label: str = ""  # friendly name shown to users (the concrete model stays hidden)
    max_tokens: int = 4096
    temperature: Optional[float] = None
    api_base: Optional[str] = None  # e.g. an Ollama endpoint: http://localhost:11434
    context_window: Optional[int] = None  # total input budget; compaction triggers off it
    # Ordered backup model profiles tried when the primary fails in a way worth
    # retrying elsewhere (billing/credit, auth, rate-limit, overload, timeout —
    # see proxy/errors.classify). The user still just picks a *tier*; which
    # provider actually answers is invisible operator policy. Each backup is a
    # full profile so it can carry its own max_tokens/api_base/temperature (a
    # GPT or local-Ollama backup differs from an Anthropic primary).
    fallbacks: list["ModelTier"] = Field(default_factory=list)

    def display(self) -> str:
        return self.label or self.model


class MCPServerConfig(BaseModel):
    """An external MCP server whose tools the proxy mounts into the tools-router."""

    name: str  # used as the tool category and name prefix
    transport: Literal["stdio", "http"] = "stdio"
    # stdio transport (proxy spawns the server as a subprocess):
    command: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # streamable-HTTP transport (proxy connects to a running, possibly remote server):
    url: Optional[str] = None
    token: Optional[str] = None  # bearer token (sent as Authorization: Bearer ...)
    # Preferred over an inline `token`: resolve the bearer from the vault at mount
    # time (the vault is the source of truth). Keeps the token out of config/.env
    # entirely — set the value on the admin Secrets tab or `secrets set <ref>`.
    secret_ref: Optional[str] = None  # e.g. devy/mcp/host
    # Optional UX overrides:
    category: Optional[str] = None  # defaults to `name`
    safety_tier: str = "external"


class EmbeddingConfig(BaseModel):
    """How chunks and queries are embedded. Separate from chat tiers because
    Anthropic has no embeddings endpoint — default to OpenAI, swap to Ollama or
    Voyage in one line."""

    model: str = "openai/text-embedding-3-small"  # LiteLLM model string
    api_base: Optional[str] = None  # e.g. http://localhost:11434 for Ollama
    batch_size: int = 64


class DatabaseConfig(BaseModel):
    """Postgres connection — the single deployment knob. Point ``url`` at the
    bundled compose container or a managed instance (RDS/Aurora); the pgvector
    extension is required (provisioned by the bootstrap schema / ``db init``).
    Defaults to ``$DATABASE_URL`` if set, else a local dev DSN."""

    url: str = Field(
        default_factory=lambda: os.environ.get("DATABASE_URL")
        or "postgresql://agentic:agentic@localhost:5432/agentic"
    )


class ChunkConfig(BaseModel):
    # Chunks are heading-scoped and variable-sized; max_chars is only a safety cap
    # (well under the embedding token limit) that windows a pathologically long
    # section. split_level picks the heading depth to split on (2 → #/##), keeping
    # deeper subsections inline with their parent section.
    max_chars: int = 8000  # ~2000 tokens; only splits genuinely oversized sections
    overlap: int = 200
    split_level: int = 2


class KnowledgeConfig(BaseModel):
    """The retrieval subsystem. Chunks live in Postgres/pgvector (the shared
    ``database.url``). The search tool is only registered once the store actually
    holds chunks, so an un-ingested proxy runs fine with this on."""

    enabled: bool = True
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    chunk: ChunkConfig = Field(default_factory=ChunkConfig)
    # Contextual enrichment (Phase 9c). The default embedded context is
    # DETERMINISTIC and free: the chunk's `title > heading_path` lineage, prepended
    # before embedding. This optional flag adds a `fast`-tier LLM synopsis on top
    # (Anthropic "contextual retrieval") — an A/B / context-poor-corpus lever, off
    # by default since heading-structured markdown already carries cheap context.
    # Independent of hybrid search (which always works via the generated tsvector).
    contextual_enabled: bool = False
    # Cap how much of the surrounding document is sent as context per chunk call.
    contextual_max_doc_chars: int = 8000
    # Conversation memory (Phase 8): embed each exchange so the `recall_history`
    # tool can retrieve relevant past context (this + prior conversations). Set
    # false to store no conversation content for retrieval (privacy).
    history_enabled: bool = True
    # Web search (extended retrieval): a native `web_search` tool via Tavily. Needs
    # the Tavily API key (set on the Secrets tab / TAVILY_API_KEY). Set false to
    # not register the tool at all.
    web_search_enabled: bool = True
    # Evolving fact tier (Knowledge Memory, Phase A): the durable, bi-temporal
    # structured-fact store behind the `recall_facts` / `memory_add` tools. Shared
    # cross-conversation knowledge, distinct from per-user conversation history.
    # Set false to disable both tools (no fact storage/retrieval).
    facts_enabled: bool = True
    # Secret redaction at ingest (Knowledge Memory, Phase C): strip secrets from
    # documents and fact deposits before they're persisted/embedded. `fail_closed`
    # quarantines a doc on an ambiguous high-entropy hit (human review); known
    # secret patterns are always redacted inline. `best_effort` redacts everything
    # inline and never quarantines. `redaction_entropy` toggles the Tier-2 heuristic.
    redaction_enabled: bool = True
    redaction_mode: str = "fail_closed"  # fail_closed | best_effort
    redaction_entropy: bool = True
    # LLM documentation generation (Phase D-2): synthesize OKF component docs from
    # code. Diff-driven (skips unchanged repos). Generated markdown is written under
    # `docgen_output_dir` (a durable bindmount/volume — never overlay FS) and ingested
    # into a `gen:<repo>` corpus. The reduce/synthesis step uses `docgen_tier`
    # (Sonnet-level minimum — quality is the primary hallucination control).
    docgen_enabled: bool = False
    docgen_output_dir: str = "docs-corpus"
    docgen_tier: str = "balanced"  # Sonnet-level minimum; operator may map to a `deep` tier
    docgen_max_files: int = 40  # signal-file cap per component (cost guard)


class SecretsConfig(BaseModel):
    """Where external credentials (connector tokens, provider keys) come from.

    One AWS Secrets Manager API surface everywhere — LocalStack in ``dev``, real
    AWS SM in ``prod`` — so the resolve path is identical dev→prod. The mode is the
    single deployment knob (set ``DEVY_MODE`` in ``.env``):

    - ``dev``  — store = LocalStack (``endpoint_url``); the admin UI can **set** and
      test secrets; writes are mirrored to ``store_file`` and re-hydrated into
      LocalStack on boot (LocalStack Community doesn't persist), so secrets survive
      restarts. Dummy AWS creds (LocalStack ignores them).
    - ``prod`` — store = real AWS SM (no ``endpoint_url``); the app authenticates via
      the ambient instance **IAM role** (no key at rest); secrets are provisioned
      **out-of-band** (Terraform/CDK) and the admin UI is **test-only** (writes 403).

    The mode is *not* a secret, so it lives in ``.env``/config, never in the store.
    """

    mode: Literal["dev", "prod"] = "dev"
    region: str = "us-east-1"
    # LocalStack endpoint in dev (e.g. http://localstack:4566); None → real AWS SM.
    endpoint_url: Optional[str] = None
    # DEV write-through/re-hydration file so UI-set secrets survive restarts.
    store_file: Optional[str] = Field(default_factory=lambda: str(_home() / "secrets-store.json"))
    # Dummy creds for LocalStack (ignored by it); unset in prod → IAM role chain.
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    # Secret-name prefix/namespace in the manager.
    namespace: str = "devy"
    # Prod hardening (Phase S-3). Short-TTL cache for resolved values so a busy
    # resolve path (per tool call) doesn't hit AWS SM every time — bounds latency,
    # cost, and rate limits. Writes invalidate the entry; a rotated secret is picked
    # up within the TTL. 0 disables caching (always re-fetch).
    cache_ttl: float = 60.0
    # Append a structured (value-free) audit line per secret op (set/delete/test/
    # resolve-on-fetch) to `trace_dir/secrets-audit.jsonl` — the SecOps access trail.
    audit_enabled: bool = True


class AttachmentsConfig(BaseModel):
    """User-attached images (composer paperclip) stored in a content-addressed S3
    blob store. Same AWS wiring as secrets — the S3 client reads its endpoint +
    credentials from the environment (LocalStack in dev via ``AWS_ENDPOINT_URL``;
    real S3 via the instance **IAM role** in prod). The only prod knob is the
    bucket name; no endpoint or keys in code.
    """

    enabled: bool = True
    bucket: str = "devy-blobs"
    max_bytes: int = 5_000_000          # per image (5 MB)
    max_per_turn: int = 6
    allowed_mime: list[str] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp"]
    )
    # A durable text description generated ONCE per unique image (dedup by hash),
    # so later turns carry the digest instead of re-processing the pixels. Uses a
    # VISION-capable tier — quality is the durability control (a wrong digest is
    # authoritative and reused), so default to balanced, not fast. See
    # .claude/plans/multimodal-attachments.md.
    digest_enabled: bool = True
    digest_tier: str = "balanced"


class AuthConfig(BaseModel):
    """Admin-plane identity (Phase RBAC-1). Two modes:

    - ``password`` (default, dev/interim) — the existing bcrypt admin password →
      HS256 token; the token grants the ``admin`` role. Fully backward compatible.
    - ``jwt`` (prod) — a forward-auth JWT from an edge proxy (Cloudflare Access /
      Okta+ALB / oauth2-proxy) is verified against the IdP **JWKS**; ``email`` +
      ``groups`` are read from the claims and groups are mapped to roles (see
      ``rbac``). No OAuth flow runs in-app; there is no login endpoint in jwt mode.
    """

    mode: Literal["password", "jwt"] = "password"
    # jwt mode:
    jwks_url: Optional[str] = None          # IdP JWKS endpoint (RS256 verification)
    issuer: Optional[str] = None            # expected `iss`
    audience: Optional[str] = None          # expected `aud` (verified when set)
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    header: str = "Authorization"           # header the proxy puts the JWT in (Bearer stripped)
    email_claim: str = "email"
    groups_claim: str = "groups"


class RbacConfig(BaseModel):
    """Map IdP group claims → Devy roles (Phase RBAC-1). The IdP is the source of
    truth. Roles: ``admin`` (control plane), ``operator``, ``viewer``."""

    group_roles: dict[str, str] = Field(default_factory=dict)  # IdP group -> devy role
    default_role: Optional[str] = None      # role for an authenticated user with no mapped group
    # (RBAC-2) Role assumed for assistant-plane (chat) callers whose identity ISN'T
    # verified — i.e. honor-system / password mode. Defaults to `admin` (unrestricted,
    # preserves current behaviour); in jwt mode the real role from the JWT is used.
    # Tighten this if you run the chat plane without SSO in a shared setting.
    assistant_role: str = "admin"


class LangSmithConfig(BaseModel):
    """LangSmith tracing (opt-in; ``tracing: langsmith``). The API key is a secret
    (``LANGSMITH_API_KEY``, set on the Secrets tab); everything here is plain config.

    ``capture`` controls what leaves the process for LangSmith's cloud:
      * ``full``     — prompts, completions, and tool I/O (best for building/debugging)
      * ``metadata`` — only span names, timings, success, and token usage (conservative)
    Left unset it follows ``DEVY_MODE``: **dev → full, prod → metadata** — so the same
    image is thorough in dev and privacy-conservative in prod without a code change.
    ``endpoint``/``project`` fall back to the ``LANGSMITH_ENDPOINT``/``LANGSMITH_PROJECT``
    env vars when unset, matching LangSmith's standard setup.
    """

    project: str = "devy"
    endpoint: str = "https://api.smith.langchain.com"
    capture: Optional[Literal["full", "metadata"]] = None


def _default_tiers() -> dict[str, ModelTier]:
    """Sensible starting tiers. Operators are expected to override these in
    ``config.yaml`` to match their own providers, costs, and security posture."""
    return {
        "fast": ModelTier(
            model="ollama/llama3.1",
            label="Fast (local)",
            api_base="http://localhost:11434",
        ),
        "balanced": ModelTier(model="anthropic/claude-3-5-haiku-latest", label="Balanced"),
        "deep": ModelTier(model="anthropic/claude-3-5-sonnet-latest", label="Deep"),
    }


class Settings(BaseSettings):
    """Runtime settings for the proxy and CLI."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTIC_DEVOPS_",
        env_file=(_home() / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Service
    host: str = "127.0.0.1"
    port: int = 8765

    # Model tiers (overridden by config.yaml; users pick a tier, not a model)
    tiers: dict[str, ModelTier] = Field(default_factory=_default_tiers)
    default_tier: TierName = "balanced"

    # External MCP servers to mount (overridden by config.yaml)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)

    # Postgres (sessions + knowledge), overridden by config.yaml / $DATABASE_URL
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)

    # Knowledge / retrieval subsystem (overridden by config.yaml)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)

    # Secrets backend (dev=LocalStack / prod=AWS SM). Mode also settable via DEVY_MODE.
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    # User-attached images → content-addressed S3 blob store (LocalStack dev / real S3 prod).
    attachments: AttachmentsConfig = Field(default_factory=AttachmentsConfig)

    # Admin-plane identity + roles (RBAC-1). Defaults to password mode (backward compat).
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rbac: RbacConfig = Field(default_factory=RbacConfig)

    # Optional operator note about THIS deployment/environment, injected into the
    # model context each turn (e.g. "Host: Mac Mini, macOS 26, Apple Silicon,
    # Homebrew; single-node dev box."). Gives Devy ground-truth about where it runs
    # so it doesn't have to infer host identity from tool output. The mounted tool
    # sources are listed automatically alongside it.
    deployment_context: Optional[str] = None

    # Harness behavior. max_iterations bounds tool-calling rounds per turn; an
    # adaptive RCA investigation needs room to gather across passes (raise it in
    # config.yaml for deeper investigations).
    max_iterations: int = 16

    # Per-call provider timeout (seconds). Bounds every model request so a stalled
    # streaming connection fails with an error event instead of hanging the turn
    # (and its worker thread) forever. A normal turn completes in seconds.
    request_timeout: float = 120.0

    # Conversation memory (Phase 7). Compaction triggers when the assembled
    # context exceeds compaction_ratio of the active tier's context window
    # (tier.context_window, else default_context_window). keep_recent_exchanges
    # are always kept verbatim; older ones are distilled into summary_state.
    # tool_finding_max_chars caps each stored (raw) tool finding.
    default_context_window: int = 200_000
    compaction_ratio: float = 0.78
    keep_recent_exchanges: int = 4
    tool_finding_max_chars: int = 800

    # Paths
    trace_dir: Path = Field(default_factory=lambda: _home() / "traces")

    # Tracing: "jsonl" (local, default), "langsmith", or "none"
    tracing: Literal["jsonl", "langsmith", "none"] = "jsonl"
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)

    def resolve_tier(self, tier: Optional[str] = None) -> ModelTier:
        """Resolve a tier name to its operator-configured model profile."""
        name = tier or self.default_tier
        if name not in self.tiers:
            available = ", ".join(sorted(self.tiers)) or "(none configured)"
            raise KeyError(f"Unknown model tier {name!r}. Configured tiers: {available}")
        return self.tiers[name]


def _config_path() -> Optional[Path]:
    explicit = os.environ.get("AGENTIC_DEVOPS_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    default = _home() / "config.yaml"
    return default if default.exists() else None


def _populate_env() -> None:
    """Load .env files into ``os.environ`` so provider SDKs (via LiteLLM) can read
    their API keys. pydantic-settings only maps declared fields, not arbitrary
    provider keys, so we do this explicitly."""
    from dotenv import load_dotenv

    for env_file in (_home() / ".env", Path(".env")):
        if env_file.exists():
            load_dotenv(env_file, override=False)


def _expand_env(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``$VAR`` references in string values using
    ``os.environ`` (populated from .env first). Lets config.yaml reference secrets
    like ``token: ${HOST_MCP_TOKEN}`` instead of inlining them."""
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def load_settings() -> Settings:
    """Load settings, merging an optional operator YAML config over the defaults."""
    _populate_env()
    path = _config_path()
    overrides: dict = {}
    if path is not None:
        data = _expand_env(yaml.safe_load(path.read_text()) or {})
        if "tiers" in data and isinstance(data["tiers"], dict):
            data["tiers"] = {k: ModelTier(**v) for k, v in data["tiers"].items()}
        if "mcp_servers" in data and isinstance(data["mcp_servers"], list):
            data["mcp_servers"] = [MCPServerConfig(**s) for s in data["mcp_servers"]]
        if "knowledge" in data and isinstance(data["knowledge"], dict):
            data["knowledge"] = KnowledgeConfig(**data["knowledge"])
        if "database" in data and isinstance(data["database"], dict):
            data["database"] = DatabaseConfig(**data["database"])
        if "secrets" in data and isinstance(data["secrets"], dict):
            data["secrets"] = SecretsConfig(**data["secrets"])
        if "auth" in data and isinstance(data["auth"], dict):
            data["auth"] = AuthConfig(**data["auth"])
        if "rbac" in data and isinstance(data["rbac"], dict):
            data["rbac"] = RbacConfig(**data["rbac"])
        if "langsmith" in data and isinstance(data["langsmith"], dict):
            data["langsmith"] = LangSmithConfig(**data["langsmith"])
        overrides = data
    settings = Settings(**overrides)
    # DEVY_MODE (.env) is the single deploy-mode knob; it wins over config.yaml so
    # the same image flips dev↔prod by environment alone.
    mode = (os.environ.get("DEVY_MODE") or "").strip().lower()
    if mode in ("dev", "prod"):
        settings.secrets.mode = mode  # type: ignore[assignment]
    return settings
