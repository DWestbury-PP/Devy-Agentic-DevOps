# Configuration reference

Devy is configured in two layers:

1. **`config.yaml`** — the operator's file. The authoritative place for model
   tiers, the database DSN, mounted MCP servers, and the knowledge subsystem.
2. **`.env`** — secrets and provider API keys, loaded into the environment so
   LiteLLM and the provider SDKs can read them.

Built-in defaults make the proxy run out of the box if a provider key is present.
[`config.example.yaml`](../config.example.yaml) and [`.env.example`](../.env.example)
are annotated starting points.

## Where files live

| | Location |
|---|---|
| Config file | `$AGENTIC_DEVOPS_HOME/config.yaml` (default `~/.config/agentic-devops/config.yaml`), or the path in `$AGENTIC_DEVOPS_CONFIG` |
| Secrets | `$AGENTIC_DEVOPS_HOME/.env` and/or `./.env` |
| In Docker | the host config dir is mounted to `/config` (override with `$AGENTIC_DEVOPS_CONFIG_DIR`) |

**`${VAR}` expansion:** string values in `config.yaml` are expanded against the
environment (after `.env` is loaded) — e.g. `token: ${MY_TOKEN}` keeps secrets
out of the YAML. **Scalar `AGENTIC_DEVOPS_*` env vars** override the matching
setting (e.g. `AGENTIC_DEVOPS_PORT=9000`).

## Model tiers

End users pick a **tier**; the operator maps each tier to a concrete model. The
concrete model is never exposed to clients.

```yaml
default_tier: balanced

tiers:
  fast:
    model: ollama/llama3.1            # any LiteLLM model string
    label: Fast (local)               # shown to users (model stays hidden)
    api_base: http://localhost:11434  # e.g. an Ollama endpoint
    max_tokens: 2048
  balanced:
    model: anthropic/claude-sonnet-4-6
    label: Balanced
    max_tokens: 4096
  deep:
    model: anthropic/claude-opus-4-8
    label: Deep
    max_tokens: 8192
    temperature: 0.2                  # optional
    context_window: 200000            # used by the compaction trigger
```

| Tier field | Meaning | Default |
|---|---|---|
| `model` | LiteLLM model string (provider-prefixed) | — (required) |
| `label` | Friendly name shown to users | the model string |
| `max_tokens` | Max output tokens | `4096` |
| `temperature` | Optional sampling temperature | provider default |
| `api_base` | Override endpoint (Ollama, Azure, gateways) | — |
| `context_window` | Total input budget; drives compaction (see below) | `default_context_window` |
| `fallbacks` | Ordered backup model profiles for provider failover (see below) | `[]` |

### Provider failover (`fallbacks`)

Give any tier an ordered list of **backup model profiles**. When the primary
model fails in a way worth retrying elsewhere — billing/credit exhausted, auth,
rate-limit, provider overload, or timeout — the next backup is tried
automatically. Failures that would fail *identically* on any provider (context
too large, content policy, malformed request) are **not** retried and surface as
a friendly message instead.

```yaml
tiers:
  balanced:
    model: anthropic/claude-sonnet-4-6
    max_tokens: 4096
    fallbacks:
      - { model: openai/gpt-5-mini,        max_tokens: 6144 }
      - { model: gemini/gemini-flash-latest, max_tokens: 6144 }
```

The user still just picks the **tier** — which provider actually answers is
invisible operator policy (the web chat shows a subtle "answered with a backup
model" note; the concrete model lands in the trace/audit). Each backup is a full
tier profile, so it carries its own `max_tokens`/`api_base`/`temperature` — a
GPT-5 or local-Ollama backup differs from an Anthropic primary. The backup
provider's key is **vault-mastered** (admin Secrets tab, e.g.
`devy/provider/gemini`), not set in `.env`. Two gotchas: GPT-5-class models are
reasoning models — give them a generous `max_tokens` or reasoning consumes the
budget and the answer comes back empty; and prefer Gemini's **`-latest`** aliases
(`gemini-pro-latest`, `gemini-flash-latest`) — pinned versions like
`gemini-2.5-pro` return 404 for API keys created after they were superseded.

## Database

```yaml
database:
  url: ${DATABASE_URL}   # e.g. postgresql://agentic:agentic@localhost:5432/agentic
```

The single persistence knob — bundled compose Postgres or a managed instance
(RDS/Aurora). Defaults to `$DATABASE_URL` if set, else a local dev DSN. Postgres
with the `pgvector` extension is **required**. See [Deployment](deployment.md).

## MCP servers

Mount external [MCP](https://modelcontextprotocol.io) servers; their tools join
the router. See [Extending → MCP](extending.md#mcp-servers).

```yaml
mcp_servers:
  - name: host                 # tool category + name prefix
    transport: http            # http | stdio
    url: http://host-mcp:8780/mcp
    token: ${HOST_MCP_TOKEN}   # bearer token for http
  - name: filesystem
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
    env: {}                    # extra env for the spawned process
```

| Field | Meaning |
|---|---|
| `name` | Tool category and name prefix (required) |
| `transport` | `stdio` (proxy spawns it) or `http` (connect to a running server) |
| `command` / `args` / `env` | stdio: how to spawn the server |
| `url` / `token` | http: endpoint and bearer token |
| `auth_header` | http: non-standard header to carry the bearer (default `Authorization: Bearer`; e.g. `X-Grafana-Api-Key` for the Grafana MCP) |
| `category` / `safety_tier` | optional UX overrides |

## Knowledge & embeddings

```yaml
knowledge:
  enabled: true
  history_enabled: true          # embed each exchange for recall_history (privacy off-switch)
  contextual_enabled: false      # opt-in fast-tier synopsis per chunk (off: deterministic lineage only)
  contextual_max_doc_chars: 8000 # cap on the doc text fed to the synopsis step
  embedding:
    model: openai/text-embedding-3-small   # needs OPENAI_API_KEY
    # model: ollama/nomic-embed-text         # local, zero-cost
    # model: voyage/voyage-3                  # needs VOYAGE_API_KEY
    api_base: null               # e.g. http://localhost:11434 for Ollama
    batch_size: 64
  chunk:
    max_chars: 8000              # ~2000 tokens (safety cap; chunks split on headings)
    overlap: 200
    split_level: 2               # heading depth to split on (2 → #/##; deeper stays inline)
```

Embeddings are configured **separately from the chat tiers** (Anthropic has no
embeddings endpoint). Chunks and conversation memory share the same embedder and
the same Postgres/pgvector store. The `embedding` vector column is
dimension-agnostic, so swapping models needs no migration. `history_enabled:
false` stores no conversation content for retrieval (privacy). `contextual_enabled`
controls the **optional** fast-tier per-chunk synopsis — off by default, since the
deterministic `title > heading path` lineage context is embedded for free either
way; turn it on (or pass `ingest --context`) for noisier, less-structured corpora.
See [Knowledge](knowledge.md) and [Memory](memory.md).

## Harness & memory

```yaml
# Optional overrides (defaults shown):
max_iterations: 16             # max tool-calling rounds per turn
default_context_window: 200000 # used when a tier has no context_window
compaction_ratio: 0.78         # compact at ~78% of the active tier's window
keep_recent_exchanges: 4       # always kept verbatim (not summarized)
tool_finding_max_chars: 800    # cap on each stored raw tool finding
```

Conversation compaction triggers when the assembled context exceeds
`compaction_ratio × (tier.context_window or default_context_window)`. See
[Memory](memory.md) for the mechanics.

## Guarded actions

Devy *proposes* reversible remediations; a human *approves*; the proxy *executes*
on the host MCP. Devy has no tool that mutates directly.

```yaml
actions:
  enabled: false          # turn the guarded-action plane on/off
  allow_insecure_dev: false  # permit actions without SSO (dev only)
  ttl_seconds: 900        # how long a proposed action stays approvable
```

**Fail-closed:** the actions plane refuses to enable unless `auth.mode: jwt` **or**
`allow_insecure_dev: true` — so it stays off through the unauthenticated bootstrap.
A second, deployment-level gate lives on the host-MCP sidecar
(`HOST_MCP_ALLOW_MUTATIONS`, below): both must be set for any mutating verb to run.

## Attachments & blob store

Image attachments (composer paperclip/paste) and tool-rendered images (e.g.
Grafana panels) are stored in a content-addressed (sha256) **S3 blob store** —
LocalStack in dev, real S3 in prod (same `DEVY_MODE` + AWS wiring as secrets;
dev needs `AWS_ENDPOINT_URL` set, else attachments are disabled). Blobs are served
via `GET /v1/blobs/{hash}`. User turns store image *refs*, never base64.

## Auth

Identity/RBAC live in `config.yaml` under `auth:` — full treatment in
[Security → Identity](security.md#identity). `mode: password` (default,
bootstrap/break-glass) or `jwt` (verify an edge-forwarded id_token).

```yaml
auth:
  mode: password                       # password | jwt
  jwks_url: https://www.googleapis.com/oauth2/v3/certs
  issuer:                              # string, or a list (Google needs both)
    - https://accounts.google.com
    - accounts.google.com
  audience: ${OAUTH2_PROXY_CLIENT_ID}  # OAuth Client ID the forwarded JWT is checked against
  header: Authorization                # header the edge forwards the JWT in (Bearer stripped); e.g. Cf-Access-Jwt-Assertion
  groups_claim: groups                 # JWT claim → RBAC groups
  email_claim: email                   # JWT claim → verified identity
```

## Service & tracing

```yaml
host: 127.0.0.1                # bind host (container binds 0.0.0.0; compose maps to loopback)
port: 8765
tracing: jsonl                 # jsonl (default) | langsmith | none
```

## Environment variables (`.env`)

Only the keys for the providers you actually use are needed.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres DSN (bootstrap; overrides `database.url` default) |
| `POSTGRES_PASSWORD` | Compose only: password for the bundled Postgres (bootstrap) |
| `DEVY_ADMIN_PASSWORD_HASH` / `DEVY_ADMIN_SECRET` | Enable the admin control plane (`agentic-devops admin set-password`); unset = admin disabled (bootstrap — gates admin itself) |
| `DEVY_MODE` | `dev` (LocalStack SM, writable) or `prod` (real AWS SM via IAM role) |
| `HOST_MCP_TOKEN` | Bootstrap for the host-MCP **server** only; the proxy resolves its copy from the vault (`devy/mcp/host`) |
| `HOST_MCP_ALLOW_MUTATIONS` | Host-MCP **sidecar** switch (default OFF); must be set for any mutating verb to execute. Orthogonal to the profile — a deployment-level gate for guarded actions |

Provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`TAVILY_API_KEY`, `LANGSMITH_API_KEY`, embedder keys like `VOYAGE_API_KEY`) are
**not** environment config — they are **vault-mastered** and set on the admin
Secrets tab (`devy/provider/*`). The vault is authoritative; a provider key left
in `.env` is warned about at startup and the vault value wins. See
[Security → Secrets model](security.md#secrets-model).
| `AGENTIC_DEVOPS_CONFIG` | Path to a non-default config file |
| `AGENTIC_DEVOPS_CONFIG_DIR` | Compose: host dir mounted to `/config` |
| `AGENTIC_DEVOPS_*` | Scalar overrides (e.g. `AGENTIC_DEVOPS_PORT`) |

> **Never commit secrets.** `.env` is gitignored; keep it that way.
