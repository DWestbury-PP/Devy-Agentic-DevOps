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
    # Evolving fact tier (Knowledge Memory, Phase A): the durable, bi-temporal
    # structured-fact store behind the `recall_facts` / `memory_add` tools. Shared
    # cross-conversation knowledge, distinct from per-user conversation history.
    # Set false to disable both tools (no fact storage/retrieval).
    facts_enabled: bool = True


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

    # Harness behavior. max_iterations bounds tool-calling rounds per turn; an
    # adaptive RCA investigation needs room to gather across passes (raise it in
    # config.yaml for deeper investigations).
    max_iterations: int = 16

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
        overrides = data
    return Settings(**overrides)
