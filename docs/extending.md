# Extending Devy

Devy is a **platform**: every capability is a component you can extend or replace
without touching the core. This is what lets Devy become "remote eyes and hands"
for *your* environment — point it at your hosts, your runbooks, your
observability backends, and your own in-house tools.

This guide covers each plug point. For how the pieces fit together, see
[Architecture](architecture.md).

| Plug point | When to reach for it |
|---|---|
| [Tools](#tools) | Give Devy a new capability it can call (a native check, an API call) |
| [MCP servers](#mcp-servers) | Integrate an existing system that speaks MCP — or any of your own |
| [The host MCP](#the-host-mcp) | Safe diagnostics on a real (remote) host — no shell |
| [Observability](#observability) | Metrics/logs/audit backends as investigation data sources |
| [Models & tiers](#models--tiers) | Choose which models back fast/balanced/deep |
| [Embedders](#embedders) | Choose how knowledge & memory are embedded |
| [Storage](#storage) | Bundled Postgres or a managed instance |
| [Surfaces](#surfaces) | Build a new front-end against the API |
| [Identity & auth](#identity--auth) | Scope memory/history to real users |

---

## Tools

A tool is a [`ToolSpec`](../src/agentic_devops/tools/base.py): a handler plus the
**discovery metadata** that lets the agent find it by intent. You never list
tools in the prompt — the agent calls `find_tools(intent="…")`, the router ranks
matches on this metadata, and the matching tools become callable.

```python
from agentic_devops.tools.base import ToolSpec

def _handler(args: dict) -> str:
    service = args["service"]
    # …call your API / run your check…
    return f"{service}: healthy (p99 42ms)"

my_tool = ToolSpec(
    name="service_health",
    category="observability",
    description="Check a service's current health and latency.",
    when_to_use="When asked whether a specific service is healthy or slow.",
    use_cases=["is checkout healthy", "what's the p99 latency for api"],
    input_schema={
        "type": "object",
        "properties": {"service": {"type": "string", "description": "service name"}},
        "required": ["service"],
    },
    handler=_handler,
    safety_tier="read-only",   # read-only | diagnostic | elevated
)
```

Register it where the proxy builds its router (`proxy/app.py`, alongside
`register_builtin_tools`), or contribute it under
[`tools/builtin/`](../src/agentic_devops/tools/builtin/). Good metadata
(`when_to_use`, `use_cases`) is what makes discovery work — write it for the way
a user would phrase the need.

**Request-scoped tools.** If a tool must know *who* is asking (e.g. to scope to a
user), set `wants_context=True`; the router then calls
`handler(args, context)` with `{user_id, session_id}` threaded from the request.
`recall_history` ([`tools/builtin/recall.py`](../src/agentic_devops/tools/builtin/recall.py))
is the reference example. Identity always comes from context — never from
model-supplied arguments — so a tool can't be tricked into reading another user's
data.

**Safety tiers** (`read-only` < `diagnostic` < `elevated`) are metadata the agent
and operators can reason about. For anything touching a live host, prefer the
[host MCP's](#the-host-mcp) allow-list model over a bespoke tool.

## MCP servers

Devy is an **MCP client**: mount any [Model Context Protocol](https://modelcontextprotocol.io)
server and its tools join the router, discoverable via `find_tools` like any
native tool. This is the fastest way to integrate an existing system — there's a
large ecosystem of MCP servers, and your in-house ones work the same way.

Configure under `mcp_servers` in `config.yaml`:

```yaml
mcp_servers:
  # A server the proxy spawns locally over stdio:
  - name: filesystem
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]

  # A remote server over authenticated streamable-HTTP:
  - name: my-platform
    transport: http
    url: https://mcp.internal.example.com/mcp
    token: ${MY_PLATFORM_TOKEN}     # bearer token, expanded from .env
```

`name` becomes the tool category and name prefix. Front remote servers with TLS;
the bearer token is the authn. See [Configuration](configuration.md#mcp-servers).

## The host MCP

For diagnostics on a real host, the answer is the bundled **[host MCP](../host-mcp/README.md)**
— a separate, deployable package that exposes a **declarative, profile-gated
allow-list** of host + Docker checks as MCP tools, with **no shell and no
arbitrary execution**. The proxy never gets shell on your hosts; it can only call
the checks you've allow-listed at the host's active profile.

You extend it by editing its allow-list YAML (each check is a fixed `argv` with
typed/constrained placeholders) — see
[`host-mcp/allowlist.example.yaml`](../host-mcp/allowlist.example.yaml) and the
[host MCP README](../host-mcp/README.md). This is the model to follow whenever
"eyes and hands" means *running something on a box* — keep it allow-listed.

## Observability

Devy's incident-RCA reasoning is only as good as the data it can reach. Plug your
observability stack in as **bring-your-own MCP servers** (a Grafana, CloudWatch,
CloudTrail, Loki, Prometheus, … MCP) under `mcp_servers`. Once mounted, those
tools become another data source the investigation discovers via `find_tools` and
folds into its `correlate_timeline` chronology — no core changes. See the RCA
walkthrough in the [README](../README.md#try-the-rca-demo).

## Models & tiers

Users pick a **tier** (`fast` / `balanced` / `deep`); the operator maps each tier
to a concrete model. Any [LiteLLM](https://docs.litellm.ai) provider works
(OpenAI, Anthropic, Google, Ollama, Azure, Bedrock, …), and you can mix providers
across tiers:

```yaml
tiers:
  fast:     { model: ollama/llama3.1, label: Fast (local), api_base: http://localhost:11434 }
  balanced: { model: anthropic/claude-sonnet-4-6, label: Balanced }
  deep:     { model: anthropic/claude-opus-4-8, label: Deep, context_window: 200000 }
```

The concrete model stays hidden from clients (they see the label). See
[Configuration → Model tiers](configuration.md#model-tiers).

## Embedders

Knowledge and conversation memory are embedded with a model configured
**separately** from the chat tiers (Anthropic has no embeddings endpoint). Swap
it in one line:

```yaml
knowledge:
  embedding:
    model: openai/text-embedding-3-small   # default (needs OPENAI_API_KEY)
    # model: ollama/nomic-embed-text        # local, zero-cost
    # model: voyage/voyage-3                 # needs VOYAGE_API_KEY
```

The pgvector column is **dimension-agnostic**, so changing embedders needs no
migration. See [Knowledge](knowledge.md) and
[Configuration → Embeddings](configuration.md#knowledge--embeddings).

## Storage

All persistence is one **Postgres + pgvector** instance. Run the bundled compose
container for zero setup, or point `DATABASE_URL` at a managed instance
(RDS/Aurora) and provision it once with `agentic-devops db init`. Nothing else
changes. See [Deployment](deployment.md).

## Surfaces

A surface is just a client of the HTTP/SSE [API](api.md). The web chat, the `ask`
TUI, and the one-shot endpoint all use the same `POST /v1/chat` (streamed) /
`POST /v1/complete` (one-shot) contract. To build your own (an IDE plugin, a
Slack bot, a status-page widget): stream `/v1/chat`, render the events, and pass
`X-User-Id` for history. The [web client](../web/app.js) is a compact reference.

## Identity & auth

Today identity is **honor-system**: a surface sends an `X-User-Id` header (the web
chat stores a name in `localStorage`), and history/recall are scoped to it. The
seam is deliberately centralized so a real provider drops in without touching
feature code:

- **Client side** — `authHeaders()` in [`web/app.js`](../web/app.js) is the one
  place that produces the identity header. Swap it to attach a Google ID token or
  a Cloudflare+Okta JWT.
- **Server side** — the proxy reads identity from the request (header today; add
  a JWT-verifying dependency that sets `user_id` from a verified `email` claim).
  Tools receive it only through the `wants_context` channel.

See [Security → Identity](security.md#identity) for the current posture and the
path to real multi-tenant auth.
