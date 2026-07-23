# Conversation memory

Devy remembers — without drowning the model in transcript. Memory has three parts:

1. **Two-channel sessions** — a lossless transcript for *you*, and a compact,
   token-triggered working summary for *Devy* (this page).
2. **Retrieval-over-history** — a `recall_history` tool that pulls back specifics
   from this conversation or prior ones ([below](#retrieval-over-history)).
3. **The durable fact tier** — a small, curated store of lasting truths about your
   systems that Devy (or you) chooses to keep across sessions, so a lesson learned
   once isn't re-derived every time ([below](#durable-facts--the-evolving-fact-tier)).
   Its cross-user **scope** is a deliberate design choice with room to evolve —
   see [Scope](#scope-todays-shared-model-and-how-it-evolves).

Code: [`proxy/sessions.py`](../src/agentic_devops/proxy/sessions.py),
[`proxy/tokens.py`](../src/agentic_devops/proxy/tokens.py),
[`knowledge/history.py`](../src/agentic_devops/knowledge/history.py),
[`tools/builtin/recall.py`](../src/agentic_devops/tools/builtin/recall.py).

## Two channels, one source of truth

A conversation has two representations, stored separately on the `sessions` row:

- **Display channel** (`messages`) — the **lossless, append-only transcript** of
  user prompts and Devy's final answers. It is what the UI renders and what "copy
  as Markdown" exports. It is **never trimmed**.
- **Context channel** — Devy's **derived working memory**, kept small:
  - `summary_state` — a *structured* rolling summary (sections: objective,
    confirmed findings, decisions, open hypotheses, failed attempts, key
    host/service facts, next steps — tuned for SRE/RCA work).
  - `findings` — distilled tool evidence per turn.
  - `compacted_turns` — how many leading exchanges have been folded into
    `summary_state`.

This separation (industry-standard: cf. "UIMessage vs ModelMessage") means *your*
history stays faithful while *Devy's* stays lean. The model sees
`session.working_context()` — the summary + the recent verbatim turns + recent
findings — **not** the full transcript.

## Compaction

When the assembled context grows past a threshold, older turns are folded into the
structured summary; the display transcript is untouched.

- **Trigger — token-based** (not turn-count): compaction fires when
  `count_tokens(working_context) ≥ compaction_ratio × (tier.context_window or
  default_context_window)`. Token estimation uses LiteLLM's per-model counter with
  a `~4 chars/token` fallback (so it never blocks a request).
- **Method — structured & incremental.** A cheap **`fast`-tier** call distills the
  span being dropped (turns + their findings) into the `summary_state` sections,
  *merging* with the prior summary rather than re-summarizing from scratch. The
  most recent `keep_recent_exchanges` are always kept verbatim.
- **Best-effort & safe.** On any parse/LLM failure the session is left intact (no
  compaction that turn). Findings are stored as plain text, so a
  `tool_call`/`tool_result` pair can never be split by compaction.

Knobs: `compaction_ratio`, `default_context_window`, `keep_recent_exchanges`,
`tool_finding_max_chars`, plus per-tier `context_window`
([Configuration](configuration.md#harness--memory)).

## Titles & identity

A conversation is **auto-titled** by a cheap `fast`-tier call after its first
exchange (rename via `PATCH /v1/sessions/{id}`). History is scoped by **`user_id`**
— honor-system today (an `X-User-Id` header), with a real-auth seam described in
[Security → Identity](security.md#identity).

## Retrieval-over-history

Compaction keeps context small but deliberately drops *specifics* (exact values,
error strings, which host). Retrieval brings them back **on demand**, and unlocks
cross-conversation recall ("have we seen this incident before?").

- **Storage:** every exchange (user + answer + that turn's tool findings) is
  embedded into `conversation_memories` (pgvector) when the session saves —
  reusing the configured [embedder](knowledge.md#embeddings). Gate it off with
  `knowledge.history_enabled: false` (privacy).
- **The `recall_history` tool:** discovered via `find_tools` (like
  `search_knowledge` — *not* a per-turn pre-step). `scope: all` searches the
  user's whole history (cross-conversation); `scope: this` stays in the current
  conversation. Results are returned with citations (which conversation, when).
- **Request-scoped:** the tool sets `wants_context=True`; identity (`user_id`,
  `session_id`) is threaded from the request, **never** from model arguments — so
  a tool can't read another user's history.

Devy's system prompt instructs it to *recall first* (via `find_tools`) when a
question refers to something discussed earlier, rather than claiming it has no
memory of past sessions.

> **Verified end-to-end:** a fact stated in one conversation ("checkout runs on
> `chk-prod-7`, pool size 35") was recalled — with a citation — in a brand-new
> conversation, using real embeddings.

## Durable facts — the evolving fact tier

Retrieval-over-history recalls *what was said*. The **fact tier** is different: a
small store of **curated, durable truths about your systems** that Devy (or you)
decides are worth keeping — a service's port, an owner, a config value, the shape of
a failover — so a future session doesn't have to rediscover them. Code:
[`knowledge/facts.py`](../src/agentic_devops/knowledge/facts.py),
[`tools/builtin/facts.py`](../src/agentic_devops/tools/builtin/facts.py). Gate it off
with `knowledge.facts_enabled: false`.

- **How facts get in.** You can tell Devy to remember something, *or* Devy can decide
  a discovery is worth keeping on its own. Writes go through the `memory_add` tool,
  reads through `recall_facts` (both discovered via `find_tools`, not run every turn).
- **Bi-temporal & self-correcting.** Facts are keyed by a `subject`/`attribute` slot
  and versioned in time (`valid_from`/`valid_to`). Restating a fact *supersedes* the
  old value instead of duplicating it, and a query can ask for the current truth or
  the truth *as of* a past instant. Graceful **forget** (the `memory_retract` tool, or
  the admin **Memory** tab) closes a fact's validity window without hard-deleting its
  history — reversible, not destructive.
- **Secret-safe.** Every write passes the [redaction gate](security.md) — tokens/keys
  are stripped, or an ambiguous high-entropy deposit is refused, before anything
  reaches the store.

> **Category, deliberately: `knowledge`, not `memory`.** In Devy's tool taxonomy the
> fact tier is *durable knowledge*, sitting alongside the KB — distinct from
> conversation-recall `memory`. That single classification is why it behaves the way
> the next section describes.

## Scope: today's shared model, and how it evolves

This is the one place the fact tier and conversation history deliberately **differ** —
worth spelling out, because it surprises people (a memory committed in one person's
chat shows up for everyone).

**Today — facts are deployment-shared; history is user-scoped.**

| Tier | Scoped by user? |
|---|---|
| Conversation history (`sessions`, `conversation_memories`) | **Yes** — `user_id` |
| Durable facts (`memories`) | **No** — global to the deployment |
| Knowledge base (`chunks`, `documents`) | No — shared by design (never in question) |

A fact one user commits is visible to *every* user of that Devy. This is
**intentional, not a leak**: the fact tier holds knowledge about the **shared systems**
the whole team operates, so a lesson learned once benefits everyone — it's closer to
the KB than to a private chat. The depositor *is* recorded (stamped into each fact's
`source` and `metadata`); reads simply aren't filtered by it.

**Why not just partition facts like history?** Because the tier quietly holds *two*
kinds of thing, and neither "all shared" nor "all private" fits both:

- **Team-operational** — *"payments failover ≈ 8 min," "prod is RHEL 9,"
  "`svc:pricing` owner = Dana."* Shared is **correct**; partitioning would throw away
  the cross-user learning that makes Devy feel like it grows with the team.
- **Personal / preference** — *"call me DW," "I prefer terse answers," "my on-call is
  Tuesday."* Shared is **wrong** — it bleeds one person's context onto everyone, and
  it's the class most likely to be mildly sensitive.

So the useful axis isn't *global vs partitioned* — it's **giving each memory a scope.**

**The natural evolution (deferred until there's a real second user).** Give each fact a
`scope`: `shared` (default) vs `private` (owner = the depositing user). Retrieval
returns `shared ∪ my-private`; Devy writes operational facts as `shared` and routes
personal-sounding ones to `private`. This keeps the team brain *and* the personal edge,
and maps cleanly onto the existing subject-keyed store (we already know each fact's
author). It is **not** built while there's a single user — but the design is banked,
because doing it well hinges on questions only multi-user reality answers:

- **`private` = per-user or per-team?** One org against one Devy → a binary
  `shared`/`private` is enough. *Multiple* orgs on one Devy is a heavier **tenancy**
  axis — treat it as separate / out of scope.
- **Who may forget a *shared* fact?** A shared forget affects everyone, so
  `memory_retract` on shared facts should become **RBAC-gated** (operator+), while
  `private` retract stays self-service. This is the piece most coupled to the existing
  [auth / RBAC](security.md) work.
- **The privacy edge redaction doesn't cover.** The [redaction gate](security.md) stops
  *secrets* (tokens/keys) crossing users — it does **not** stop sensitive *business*
  context (an internal hostname, a person's name). In a shared tier, anything
  auto-committed is team-visible, which argues for Devy biasing toward `private` (or
  not auto-committing at all) whenever a fact smells personal — independent of whether
  the scope model ships.

> **Operator guidance today:** treat the fact tier as an intentional, deployment-wide
> **team memory**. If a single team/org shares one Devy (the common case), that is
> exactly what you want. Before onboarding users who should *not* share a memory pool,
> wait for — or sponsor — the `scope` evolution above.
