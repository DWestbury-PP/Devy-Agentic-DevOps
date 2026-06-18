# Conversation memory

Devy remembers — without drowning the model in transcript. Memory has two parts:

1. **Two-channel sessions** — a lossless transcript for *you*, and a compact,
   token-triggered working summary for *Devy* (this page).
2. **Retrieval-over-history** — a `recall_history` tool that pulls back specifics
   from this conversation or prior ones ([below](#retrieval-over-history)).

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
