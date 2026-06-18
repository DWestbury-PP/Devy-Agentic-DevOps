# The LLM-PROXY

The proxy ([`src/agentic_devops/proxy/`](../src/agentic_devops/proxy/)) is the
service that *is* Devy — the one place reasoning, tools, memory, and tracing live.
This is a tour of its internals; for the request/response contract see the
[API reference](api.md), and for the big picture see [Architecture](architecture.md).

## The app

[`app.py`](../src/agentic_devops/proxy/app.py) builds the FastAPI app via
`create_app(settings, provider, router)` — the latter two are injectable for
tests. At startup it:

1. Applies the database schema (best-effort) and opens the connection pool.
2. Builds the `ToolsRouter`: registers builtin tools, mounts configured MCP
   servers, and registers `search_knowledge` / `recall_history` when their stores
   are available.
3. Wires the `PgSessionStore` and the conversation-memory store.

Endpoints: `/v1/chat`, `/v1/complete`, `/v1/tiers`, `/v1/tools`,
`/v1/sessions[...]`, `/healthz`, and the privileged `/v1/admin/*` control plane
(auth, host registry, document import — see [API](api.md)).

## The harness loop

[`harness.py`](../src/agentic_devops/proxy/harness.py) drives one user turn. It is
intentionally small and owned — not a framework. The core (shared by streaming
and non-streaming paths):

```
loop (bounded by max_iterations):
  response = model.call(working_messages, tools=available_tools)
  if response wants no tools:  final answer → break
  else:
    append the assistant tool-call message
    for each tool call:
      find_tools → rank matches, inject their schemas into available_tools
      other tool → router.execute(name, args, context); capture a finding
    append tool results → continue
```

- **`run_turn`** — non-streaming; used by `/v1/complete` and most tests. Emits
  events via an `on_event` callback.
- **`run_turn_streaming`** — a generator that `yield`s events (`delta`,
  `tool_call`, `tools_found`, `tool_result`, `done`) and `return`s the final
  `TurnResult`; used by the `/v1/chat` SSE route.

`TurnResult` carries the final text, the full message list, `tools_used`, token
`usage`, and the **`tool_findings`** captured this turn (which feed
[memory](memory.md)). The `tool_context` parameter (`{user_id, session_id}`) is
threaded to context-aware tools — see [Extending → Tools](extending.md#tools).

### Why on-demand discovery

The model is only ever offered `find_tools` up front. It describes what it needs
in plain language; the [`ToolsRouter`](../src/agentic_devops/tools/router.py)
ranks registered tools by their metadata and the harness injects the matching
schemas for subsequent rounds. The working context stays small no matter how many
tools exist (native + every mounted MCP server). This is the project's defining
pattern — see [Architecture](architecture.md) and [JOURNEY](JOURNEY.md).

## The provider layer

[`providers.py`](../src/agentic_devops/proxy/providers.py) wraps
[LiteLLM](https://docs.litellm.ai) behind a thin `ProviderClient` with two
methods — `complete()` and `stream()` — returning a normalized `ProviderResponse`
(text, tool calls, usage). Because it's LiteLLM underneath, any provider works
(OpenAI, Anthropic, Google, Ollama, Azure, Bedrock, …), selected per the user's
**tier** (see [Configuration → Model tiers](configuration.md#model-tiers)). The
concrete model is never exposed to clients.

## Sessions & the turn lifecycle

On each turn the proxy: loads the session, compacts it if the context is too large
([Memory](memory.md)), assembles the working context + the new message, runs the
turn, then persists the **display transcript** + distilled **findings**,
auto-titles a new conversation (cheap tier), and embeds the exchange for recall.
Session storage and compaction live in
[`sessions.py`](../src/agentic_devops/proxy/sessions.py); message assembly in
[`prompts.py`](../src/agentic_devops/proxy/prompts.py).

## Tracing

[`tracing.py`](../src/agentic_devops/proxy/tracing.py) records the loop (model
calls, tool calls, timing) so you can debug *why* Devy did what it did. JSONL by
default (under the trace dir); set `tracing: langsmith` for
[LangSmith](https://www.langchain.com/langsmith), or `none` to disable.
