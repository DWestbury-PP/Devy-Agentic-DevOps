# Knowledge base

Devy grounds answers in *your* documentation — runbooks, postmortems,
architecture notes, the repo itself — and **cites** what it used. Retrieval is a
**tool the agent discovers on demand** (`search_knowledge`), not a pre-step bolted
onto every query. The pipeline is content-agnostic: point it at a directory and
go.

Code: [`src/agentic_devops/knowledge/`](../src/agentic_devops/knowledge/).

## The pipeline

```
agentic-devops ingest <path>
   sweep        → collect .md/.markdown/.txt/.rst files (skipping noise dirs)
   chunk        → structural Markdown chunking (heading-scoped)
   enrich       → prepend a context prefix (lineage; optional synopsis)
   embed        → batch-embed (context + chunk text) (configured embedder)
   store        → upsert into Postgres + pgvector
```

- **Sweep & chunk** ([`ingest.py`](../src/agentic_devops/knowledge/ingest.py),
  [`chunking.py`](../src/agentic_devops/knowledge/chunking.py)): files are split
  along Markdown structure so each chunk carries its **heading path** — that path
  becomes the citation (`corpus / file.md # Section > Subsection`). Splitting is
  heading-depth-aware (`split_level`, default 2 → `#`/`##`; deeper subsections stay
  inline), fenced-code-block-aware (a `#` inside a code block isn't a heading), and
  capped at `max_chars` (8000) for safety.
- **Enrich** ([`enrich.py`](../src/agentic_devops/knowledge/enrich.py)): before
  embedding, each chunk is prefixed with a **deterministic lineage context**
  (`title > heading path`) so an out-of-context fragment still embeds with its
  provenance — free and on by default. An optional fast-tier **synopsis** (a
  one-line "what this chunk is about" from the `fast` model) can be layered on for
  noisier corpora; it's **opt-in / off by default** (`knowledge.contextual_enabled`,
  CLI `--context`).
- **Idempotent re-ingest:** each chunk is content-hashed; a file whose chunks are
  unchanged is skipped (no re-embedding), and a changed file has its old chunks
  dropped and re-embedded. Re-run `ingest` after editing docs and only pay for
  what changed. The hash also folds in an **embed-recipe marker**, so changing the
  enrichment recipe (e.g. turning the synopsis on) re-embeds even identical text.

```bash
agentic-devops ingest --corpus repo .       # dogfood: index this repo
agentic-devops ingest corpora/acme-sre        # a fictional SRE knowledge base
agentic-devops ingest corpora/platform        # the RCA-demo runbook
```

> Ingesting is a native/CLI step (the repo isn't mounted into the container).
> Point the CLI's `$DATABASE_URL` at the same database the proxy uses. If the
> console script raises `ModuleNotFoundError` (editable install + a space in the
> path), use `PYTHONPATH=src python -m agentic_devops.cli.main ingest <path>`.

## Embeddings

Embeddings are configured **separately from the chat tiers** — Anthropic has no
embeddings endpoint — under `knowledge.embedding`
([`embeddings.py`](../src/agentic_devops/knowledge/embeddings.py)):

- Default `openai/text-embedding-3-small` (needs `OPENAI_API_KEY`).
- Local & zero-cost: `ollama/nomic-embed-text` (run Ollama; set `api_base`).
- Or `voyage/voyage-3`, etc.

The same embedder serves the knowledge base and [conversation memory](memory.md).
See [Configuration → Knowledge & embeddings](configuration.md#knowledge--embeddings).

## The vector store

[`store.py`](../src/agentic_devops/knowledge/store.py) — `PgVectorStore` over the
`chunks` table:

- **Hybrid search.** `hybrid_search` runs semantic nearest-neighbour (pgvector
  `<=>`) *and* exact-keyword full-text (a generated `tsv` column + `@@`) in
  parallel, then fuses the two rankings with **Reciprocal Rank Fusion** (RRF). This
  catches both "what I mean" (vectors) and "this exact identifier/flag" (FTS) — each
  hit is tagged with the `sources` that surfaced it.
- The `embedding` column is **dimension-agnostic** (`vector`, no fixed dim), so
  you can switch embedders without a migration. Vectors are written as
  `%s::vector` literals and never read back, so no array adapter is needed.
- **Scaling:** exact search needs no index and is plenty at SRE-KB scale. For
  large corpora, pin the embedding dimension and add an HNSW/ivfflat index — a
  documented upgrade, not required by default.

## The `search_knowledge` tool

[`retrieval.py`](../src/agentic_devops/knowledge/retrieval.py) builds the
`search_knowledge` `ToolSpec`. The agent discovers it via `find_tools` when a
question looks answerable from ingested docs, runs a **hybrid** (semantic +
exact-keyword) search, and returns the top chunks **with citations** so the answer
is attributable rather than laundered. The tool is **always registered** when
knowledge is enabled; corpus coverage (which corpora, how many chunks) is read
**live** on each call — so a document uploaded through the admin control plane is
searchable immediately, with no restart, and an empty knowledge base simply
returns "no matches".

## Demo corpora

[`corpora/`](../corpora/README.md) ships ready-to-ingest examples: the repo itself
(dogfood), a fictional `acme-sre/` knowledge base (runbooks, on-call playbook,
postmortems), and `platform/` (the crash-loop runbook used by the
[RCA demo](../README.md#try-the-rca-demo)).
