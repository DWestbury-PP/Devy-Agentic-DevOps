# Architecture

Devy is **one capable agent, exposed over one API, reachable from thin clients.**
A single service — the **LLM-PROXY** — owns all the reasoning, tools, memory, and
tracing. Everything else (the web chat, the `ask` TUI, a one-shot HTTP call, the
host MCP on a remote box) is a thin client or a pluggable source. This document
explains the pieces and how a request flows through them.

For the *why* behind these choices — and the more elaborate multi-agent design
that came before — see the [background journey](JOURNEY.md).

## Design principles

- **One capable agent, not a committee.** A single, well-equipped agent harness
  beats a society of role-scoped agents that second-guess each other, churn
  tokens, and fail in correlated ways.
- **One brain, many thin surfaces.** The proxy owns reasoning, context, history,
  and tracing. Surfaces are dumb clients of one API, so a new front-end is a few
  hundred lines, not a fork.
- **Discover tools on demand.** Tools register with metadata; the agent finds
  them by intent through a single `find_tools` call. The working context stays
  small while reach stays broad — the opposite of dumping every tool schema into
  the system prompt.
- **Safe by default.** Anything touching a live host is an allow-listed,
  profile-gated, audited check — never a shell. This is the posture that gets
  Devy past security review and onto real infrastructure.
- **Provider-agnostic and observable.** A thin model layer (LiteLLM) talks to any
  provider; tracing makes the agent's loop visible.

## Components

```
src/agentic_devops/
├── config.py       settings model (tiers, database, knowledge, mcp_servers)
├── proxy/          the LLM-PROXY service
│   ├── app.py          FastAPI app + endpoints (/v1/chat, /v1/complete, /v1/sessions, /v1/admin/*, …)
│   ├── schemas.py      request/response models
│   ├── harness.py      the agent loop (assemble → model → tools → repeat)
│   ├── providers.py    ProviderClient — LiteLLM wrapper (completion + streaming)
│   ├── sessions.py     two-channel sessions + structured compaction
│   ├── prompts.py      system prompt + message assembly
│   ├── tokens.py       token estimation for the compaction trigger
│   ├── mcp_client.py   mounts external MCP servers into the tools-router
│   ├── tracing.py      pluggable tracing (JSONL default, LangSmith optional)
│   │   # ── admin control plane (Phase 9) ──
│   ├── auth.py             interim bcrypt password → HS256 token (the SSO seam)
│   ├── encryption.py       Fernet encryption for per-host MCP tokens at rest
│   ├── hosts.py            HostStore — host registry CRUD + resolve
│   ├── host_mcp_client.py  on-demand caller for a registered host's MCP
│   ├── documents.py        DocumentStore / JobStore (import registry)
│   └── ingest_worker.py    in-process worker that runs queued ingest jobs
├── tools/          the tools-router + tool definitions
│   ├── router.py       registry + find_tools discovery + execution
│   ├── base.py         ToolSpec (the metadata that powers discovery)
│   └── builtin/        native tools: diagnostics, correlate_timeline, recall, hosts
├── knowledge/      the retrieval subsystem
│   ├── ingest.py       sweep → chunk → enrich → embed → store
│   ├── chunking.py     structural Markdown chunking
│   ├── enrich.py       contextual prefix (deterministic lineage; optional synopsis)
│   ├── embeddings.py   provider-agnostic embedder (LiteLLM)
│   ├── store.py        PgVectorStore (hybrid: vector + full-text)
│   ├── history.py      ConversationMemoryStore (conversation recall)
│   ├── retrieval.py    the search_knowledge tool
│   └── factory.py      build store + embedder from config
├── db/             Postgres connection pool + bootstrap schema.sql
└── cli/main.py     `agentic-devops serve | ingest | db init | admin set-password | admin gen-key`
```

## The harness loop

`harness.py` is the heart — deliberately small and owned, not inherited from a
framework. One turn:

1. **Assemble context** (`prompts.assemble_messages`): the system prompt + the
   session's *working context* (see [Memory](memory.md)) + the new user message.
   Only `find_tools` is offered to the model up front.
2. **Call the model** (`providers.ProviderClient`, via the user's tier).
3. **If the model wants tools**, execute them (`tools/router.py`):
   - `find_tools(intent=…)` → the router matches registered tools by intent and
     the harness injects their full schemas into the available set for the next
     round (one round-trip, no separate "load" step).
   - any other tool → executed; the result is fed back, and a distilled **finding**
     is captured for memory.
4. **Repeat** until the model answers without calling a tool (bounded by
   `max_iterations`).

Both the non-streaming path (`run_turn`, used by `/v1/complete`) and the
streaming path (`run_turn_streaming`, used by the `/v1/chat` SSE route) share the
same tool-handling core.

### Tools-router & on-demand discovery

Tools are registered as [`ToolSpec`](../src/agentic_devops/tools/base.py)s with
discovery metadata (`category`, `when_to_use`, `use_cases`). The agent never sees
every schema — it calls `find_tools` with a plain-language intent, the router
ranks matches, and only those become callable. This keeps the prompt small no
matter how many tools (native + every mounted MCP server) are available. See
[Extending Devy → Tools](extending.md#tools).

A tool that needs **request identity** (like `recall_history`, which must scope to
the current user) sets `wants_context=True`; the router then calls it as
`handler(args, context)` where the harness threads `{user_id, session_id}` from
the request. Identity comes from context, never from model-supplied arguments.

## Request flow

**`POST /v1/chat`** (multi-turn, streamed):

```
client → /v1/chat → load session → compact if needed → assemble context
       → run_turn_streaming → [SSE: session, delta, tool_call, tool_result, done]
       → persist (display transcript + distilled findings) → embed exchange (recall)
```

**`POST /v1/complete`** (one-shot, non-streaming): the same pipeline without the
event stream — used by the `ask --complete` flow and scripting. See the full
contract in [API reference](api.md).

## Persistence

One **Postgres + pgvector** instance backs everything that survives a restart:

- `sessions` — conversation history (the lossless display transcript + the
  structured working summary + distilled findings). See [Memory](memory.md).
- `chunks` — knowledge-base chunks + embeddings. See [Knowledge](knowledge.md).
- `conversation_memories` — per-exchange embeddings for `recall_history`.

The DSN (`database.url` / `$DATABASE_URL`) is the single deployment knob — the
bundled compose container or a managed instance (RDS/Aurora). The idempotent
bootstrap (`db/schema.sql`) is applied by compose init, by `agentic-devops db
init`, and best-effort on proxy startup. See [Deployment](deployment.md).

## Surfaces

Every surface is a thin client of the same HTTP/SSE API:

- **[web chat](../web/README.md)** — terminal-themed, streamed Markdown/Mermaid,
  tool-call trail, citations, and a conversation-history slide-out. nginx serves
  the static assets and reverse-proxies the API (single origin, no CORS).
- **[`ask` TUI](../tui/README.md)** — a native Go binary; one-shot, piped stdin,
  or an interactive REPL. Zero runtime deps.
- **one-shot HTTP** — `POST /v1/complete` for scripts and integrations.

Building your own surface is just speaking the [API](api.md).

## Observability of the agent itself

`tracing.py` records the agent's loop (model calls, tool calls, timing) — JSONL
by default, LangSmith optional (`tracing: langsmith`). This is how you debug *why*
Devy did what it did.
