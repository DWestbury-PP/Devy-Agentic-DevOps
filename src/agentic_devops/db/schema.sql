-- Agentic DevOps — Postgres bootstrap schema.
--
-- Idempotent: safe to run repeatedly (everything is IF NOT EXISTS). Applied two
-- ways, covering both deployment modes:
--   * the bundled `postgres` compose service runs it on first init
--     (mounted into /docker-entrypoint-initdb.d/), and
--   * against an existing/managed database (e.g. RDS/Aurora):
--     `agentic-devops db init` (run once by someone with rights to CREATE EXTENSION).
-- The proxy also applies it best-effort on startup, so a fresh local DB just works.

CREATE EXTENSION IF NOT EXISTS vector;

-- Knowledge base: one row per embedded chunk, with provenance for citation.
-- `embedding` is an UNSPECIFIED-dimension vector so any embedder works without a
-- schema change (OpenAI-1536 / Ollama-768 / Voyage / ...). Exact cosine search
-- (ORDER BY embedding <=> query) needs no index and is plenty at SRE-KB scale;
-- for large corpora, pin the dimension and add an HNSW index (see docs).
CREATE TABLE IF NOT EXISTS chunks (
    id             TEXT PRIMARY KEY,
    corpus         TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    heading_path   TEXT NOT NULL DEFAULT '',
    text           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    embedding      vector NOT NULL,
    -- Enriched ingestion (Phase 9c-1): a contextual blurb situating the chunk in
    -- its document (prepended before embedding), search-enhancement metadata, and
    -- a generated tsvector so full-text/hybrid search works even for chunks
    -- ingested before enrichment existed.
    context_prefix TEXT NOT NULL DEFAULT '',
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX IF NOT EXISTS idx_chunks_corpus ON chunks (corpus);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks (corpus, source_path);
-- Upgrade existing chunks tables in place (no-op once present). These MUST run
-- before the tsv GIN index below, so the column exists when the index is built.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS context_prefix TEXT NOT NULL DEFAULT '';
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (tsv);

-- Conversation history — two channels (Phase 7):
--   * `messages`  = the lossless DISPLAY transcript (clean user/assistant turns,
--                   no tool scaffolding). Append-only; never trimmed.
--   * `summary_state` (structured rolling summary) + `findings` (distilled tool
--                   evidence) = Devy's derived working CONTEXT, kept small.
-- `compacted_turns` = how many leading exchanges have been folded into
-- summary_state (assembly uses messages after that point). Findings are plain
-- text/JSON, never raw tool_call/result pairs, so compaction can't split a pair.
-- `user_id` is an optional honor-system identity; `title` is an auto-generated label.
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT,
    title           TEXT,
    messages        JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary_state   JSONB NOT NULL DEFAULT '{}'::jsonb,
    findings        JSONB NOT NULL DEFAULT '[]'::jsonb,
    compacted_turns INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id, updated_at DESC);

-- Conversation memory (Phase 8): one embedded row per exchange, for
-- retrieval-over-history (the `recall_history` tool). Scoped by user_id (cross-
-- conversation) and session_id (this conversation). `embedding` is the same
-- dimension-agnostic vector type as `chunks`; exact cosine search via `<=>`.
CREATE TABLE IF NOT EXISTS conversation_memories (
    id          TEXT PRIMARY KEY,           -- "<session_id>:<turn>"
    session_id  TEXT NOT NULL,
    user_id     TEXT,
    turn        INTEGER NOT NULL,
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding   vector NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cmem_user ON conversation_memories (user_id);
CREATE INDEX IF NOT EXISTS idx_cmem_session ON conversation_memories (session_id);

-- Host registry (Phase 9b, admin control plane): the fleet Devy can run
-- diagnostics against via each host's MCP. Devy targets a host by identifier;
-- the proxy resolves it to an endpoint + (decrypted) token. `token_encrypted` is
-- the per-host MCP bearer token, Fernet-encrypted at rest (key from env) and
-- never returned by the API.
CREATE TABLE IF NOT EXISTS hosts (
    id                 TEXT PRIMARY KEY,
    fqdn               TEXT NOT NULL UNIQUE,
    private_ip         TEXT,
    public_ip          TEXT,
    instance_id        TEXT,
    aws_account        TEXT,
    aws_region         TEXT,
    mcp_port           INTEGER NOT NULL DEFAULT 8780,
    mcp_scheme         TEXT NOT NULL DEFAULT 'https',
    address_preference TEXT NOT NULL DEFAULT 'private_ip',  -- private_ip | public_ip | fqdn
    token_encrypted    BYTEA,
    profile            TEXT,                                 -- expected host-MCP profile
    active             BOOLEAN NOT NULL DEFAULT TRUE,
    labels             JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at       TIMESTAMPTZ,
    last_status        TEXT,                                 -- reachable | unreachable | unknown
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_hosts_active ON hosts (active);

-- Document import (Phase 9c-2): one row per imported source document. Both the
-- `ingest` CLI and the UI upload register documents (one unified registry), so
-- the Knowledge admin page shows every corpus. Chunks link back via
-- `chunks.document_id`; the in-process ingest worker tracks batch progress in
-- `ingest_jobs`. `content` keeps the raw markdown so a doc can be re-enriched
-- without re-upload; `version` bumps when re-imported content changes.
CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    corpus        TEXT NOT NULL,
    source_path   TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    doc_type      TEXT NOT NULL DEFAULT 'doc',
    content       TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL DEFAULT '',
    bytes         INTEGER NOT NULL DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | processing | ready | failed
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    uploaded_by   TEXT NOT NULL DEFAULT '',
    job_id        TEXT,                                -- the upload batch this doc came in with
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (corpus, source_path)
);
CREATE INDEX IF NOT EXISTS idx_documents_corpus ON documents (corpus, source_path);

-- Ingest jobs (Phase 9c-2): one row per upload batch; `total`/`done` count its
-- documents so the UI can poll progress. Single-instance in-process worker.
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id          TEXT PRIMARY KEY,
    corpus      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',      -- queued | running | done | failed
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON ingest_jobs (status, created_at);

-- Link chunks to their source document (nullable: CLI corpora predating the
-- registry stay null until re-ingested). Deleting a document deletes its chunks
-- explicitly in DocumentStore (no FK cascade, keeping the schema simple).
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS document_id TEXT;
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks (document_id);

-- Upgrade pre-existing sessions tables (Phase 6 → 7) in place. Idempotent.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title           TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_state   JSONB   NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS findings        JSONB   NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS compacted_turns INTEGER NOT NULL DEFAULT 0;
