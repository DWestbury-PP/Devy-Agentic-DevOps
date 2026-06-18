# Plan — Admin / Control Plane

Design-and-build plan for Devy's **control plane**: managing *what Devy can reach
and know*. Two features: a **host registry** (the fleet Devy can run diagnostics
against) and **document import** (UI-driven, enriched knowledge ingestion). This
doc is the agreed paper before building; it captures the decisions from the design
discussion.

Status: **agreed**, building in phases (9a → 9b → 9c).

## 1. Framing

Everything to date is the **assistant plane** (Devy answering questions). This work
adds an **admin / control plane** — privileged management of infrastructure config
and the knowledge base. Rules:

- New API namespace: **`/v1/admin/*`** (privileged), separate from the assistant
  endpoints.
- A new **admin UI section** that is a thin client of the admin API (the proxy
  owns all logic), same philosophy as the chat surface.
- Control-plane auth is **separate** from the honor-system chat `user_id`.

## 2. Access control (cross-cutting)

**Decision:** an **interim password login** now; real SSO (Google / Cloudflare+Okta
JWT with an email claim) once there's a hosted instance with a stable domain.
Production OAuth needs registered redirect URIs / a public origin, so it isn't
worth setting up against a local instance yet.

Interim design (Phase 9a):

- An **admin password**, stored **hashed** (bcrypt) in config/env — single
  operator for now.
- `POST /v1/admin/login` verifies the password and issues a **short-lived signed
  token** (HS256 JWT, server secret from env).
- An auth **dependency** guards every `/v1/admin/*` endpoint (verify signature +
  expiry). **This verifier is the seam** where a Google/Okta JWT verifier drops in
  later — the interim is the foundation, not throwaway.

Config / env:

| Setting | Purpose |
|---|---|
| `DEVY_ADMIN_PASSWORD_HASH` | bcrypt hash of the admin password |
| `DEVY_ADMIN_SECRET` | HMAC signing secret for admin tokens |
| `DEVY_ENCRYPTION_KEY` | Fernet key encrypting per-host MCP tokens at rest |

If `DEVY_ADMIN_PASSWORD_HASH` / `DEVY_ADMIN_SECRET` are unset, the admin plane is
**disabled** (endpoints return 503) — the assistant plane runs unchanged. A
`agentic-devops admin set-password` CLI helper prints a hash to paste into config.

## 3. Host registry (Phase 9b)

Generalizes today's static `mcp_servers` config into a **dynamic, DB-backed fleet
registry**. Devy looks hosts up as needed and targets them by identifier; the proxy
resolves identifier → endpoint + token + profile (the agent never handles secrets).

### Data model — `hosts`

| Column | Notes |
|---|---|
| `id` | PK |
| `fqdn` | hostname (unique) |
| `private_ip`, `public_ip` | public optional |
| `instance_id`, `aws_account`, `aws_region` | AWS metadata (informational now; seed for AWS-native + auto-discovery later) |
| `mcp_port`, `mcp_scheme` | how to reach the host MCP (default `https`) |
| `address_preference` | which to dial: `private_ip` (default) \| `public_ip` \| `fqdn` |
| `token_encrypted` | per-host MCP bearer token, **Fernet-encrypted at rest**; never returned by the API |
| `profile` | expected host-MCP profile (`read-only`/`diagnostic`/`elevated`) |
| `active` | whether Devy may use it |
| `labels` | jsonb tags |
| `last_seen_at`, `last_status` | reachability/health |
| `created_at`, `updated_at` | |

### Agent tools (assistant plane)

Generic-but-scoped (decided: powerful single interface, scope per call, batch by
choice; the host-MCP allow-list stays the authority):

- **`host_details_lookup(query)`** — cheap registry lookup over **active** hosts;
  returns FQDN/IP/region/status/profile **and the checks available** on each, so
  Devy is scope-aware before acting.
- **`run_host_check(host, check, args?)`** — one generic interface to a host's
  allow-listed checks; each call runs **one check, scoped by its args**
  (lines/since/grep/container). The proxy resolves `host` → endpoint + decrypted
  token, dials that host's MCP, and the host-MCP validates `check`/`args`.
- **`run_host_checks(host, [checks])`** — **batched** variant: a health sweep
  (disk + memory + load + …) in one round-trip, collapsing the agent loop while the
  agent still chooses the set. Results capped by `tool_finding_max_chars`.

### Admin API & UI

- `GET/POST/PATCH/DELETE /v1/admin/hosts[/{id}]` (token write-only: accepted on
  create/update, never returned).
- `POST /v1/admin/hosts/{id}/check` — test reachability; update `last_seen/status`.
- UI: a **Hosts** page — table with status, add/edit/delete, a "test connection"
  action.

## 4. Document import + enriched ingestion (Phase 9c)

UI-driven markdown import through an enriched pipeline:
**chunk → per-chunk search-enhancement metadata → embed → store**, with hybrid
retrieval. (Decided: contextual retrieval + hybrid + metadata.)

**Decisions locked (2026-06-16):**
- **Split into 9c-1 (backend) → 9c-2 (UI)**, mirroring the 7a/7b split. 9c-1 lands
  the schema deltas + enrichment pipeline + hybrid `search_knowledge` (verifiable
  via the `ingest` CLI + tests); 9c-2 adds the upload UI + async job worker +
  document/job admin endpoints.
- **Enrichment lives in the shared knowledge pipeline**, so BOTH the `agentic-devops
  ingest` CLI and the future UI upload produce contextual + hybrid + metadata
  chunks. The existing corpora can be re-ingested to full quality; the two ingest
  paths stay at parity.
- **`tsv` is a Postgres generated column from `text`** (GIN-indexed) — so already-
  ingested chunks get full-text/hybrid for free; only `context_prefix` + `metadata`
  require a re-ingest.
- **RRF** fusion in hybrid retrieval; **markdown-only** import.

**Decisions refined (2026-06-17) — the "do we need Haiku?" review:**
- **Default context is DETERMINISTIC, not LLM.** Our markdown is heading-structured,
  so chunks already have cheap exact context (the `heading_path`). The default
  embedded text is `title > heading_path` + chunk — "poor-man's contextual
  retrieval," free and deterministic, capturing most of the benefit. The headline
  contextual-retrieval gains came from *context-stripped* chunks; our marginal lift
  from an LLM blurb is small.
- **Haiku synopsis stays but defaults OFF** (`contextual_enabled: false`, CLI
  `--context` to opt in) — an A/B / context-poor-corpus lever, not a per-ingest tax.
  *(9c-1 shipped it default-on; 9c-2 flips the default and adds the deterministic
  context path.)*
- **Embed vs store — the lineage distinction.** Only *embedded* text affects vector
  similarity. So embed the structural context (`title`, `heading_path`); keep
  **name / version / import-date / doc-type as metadata only** (they're noise in a
  similarity vector — they'd hurt — but power filtering, citations, and lineage).
- **Chunk on H2** (configurable `split_level`, default 2): each chunk is a whole
  section, H3+ subsections kept inline with their parent; variable-sized, with only
  an embedding-token safety cap forcing a sub-split.

### Enrichment (the "search-enhancement metadata")

- **Structural context (default, free)** — prepend `title > heading_path` to the
  chunk **before embedding**, re-injecting the section lineage that chunking moved
  into the path. No LLM call.
- **Contextual blurb (opt-in)** — when enabled, a `fast`-tier LLM adds a 1–2 sentence
  synopsis on top of the structural context, before embedding.
- **Keywords + `tsvector`** — for **hybrid** search (vector + Postgres full-text),
  catching exact tokens (error codes, hostnames, flags) vectors miss.
- **Metadata (stored, not embedded)** — title, source, corpus, doc-type, headings,
  **version, import date** — for filtering, change-lineage, and richer citations.

### Data model

`documents` (source registry):

| Column | Notes |
|---|---|
| `id` | PK |
| `corpus`, `title`, `source`, `doc_type` | |
| `content` | raw markdown (so re-enrich without re-upload) |
| `content_hash`, `bytes` | dedupe / change detection |
| `status` | `pending`/`processing`/`ready`/`failed` |
| `chunk_count`, `uploaded_by`, `created_at`, `updated_at` | |

`chunks` — extend the existing table:

- `document_id` (FK), `context_prefix` (the contextual blurb), `metadata` (jsonb:
  keywords, entities, tags, title), `tsv` (`tsvector`, GIN-indexed). The embedding
  now covers `context_prefix + text`.

`ingest_jobs` — `id`, `corpus`, `status` (`queued`/`running`/`done`/`failed`),
`total`, `done`, `error`, timestamps.

### Async job model

Ingest + LLM enrichment is too slow to block an upload. A **single-instance
in-process background worker** processes queued documents and updates `ingest_jobs`
progress; the UI polls. (A real queue is a multi-replica upgrade — deferred.)

### Retrieval change

`search_knowledge` becomes **hybrid**: vector (`<=>`) + full-text (`tsv @@
plainto_tsquery`), fused (Reciprocal Rank Fusion), returned with metadata-rich
citations.

### Admin API & UI

- `GET/POST/DELETE /v1/admin/documents[/{id}]` (POST = upload + enqueue), `GET
  /v1/admin/jobs/{id}`, `GET /v1/admin/corpora`.
- UI: a **Knowledge** page — upload markdown, see per-document status/progress,
  browse/delete by corpus.

## 5. Security

- **Encryption at rest** for per-host tokens (Fernet, key from env); decrypted only
  in-proxy to dial a host MCP. Tokens are never returned by the API.
- **Admin auth** on every `/v1/admin/*` endpoint; the verifier is the SSO seam.
- The **host-MCP allow-list remains the authority** on what runs on a host — the
  generic `run_host_check` cannot exceed it (the host-MCP enforces server-side).
- **Audit** admin actions (host create/delete, document ingest/delete).

## 6. New dependencies

`bcrypt` (or `passlib[bcrypt]`), `cryptography` (Fernet), `pyjwt` — added to the
proxy package.

## 7. Build sequencing

- **Phase 9a — Auth foundation + admin shell.** ✅ *(merged, PR #19)* Config/secrets,
  `admin set-password` CLI, `POST /v1/admin/login` + `require_admin` dependency, a
  minimal admin UI shell behind login.
- **Phase 9b — Host registry.** ✅ *(this PR)* `hosts` table + CRUD + Fernet
  encryption + reachability check; `host_details_lookup` / `run_host_check` /
  `run_host_checks` tools (generic-but-scoped, on-demand MCP routing via
  `HostMCPClient`); the Hosts admin UI. Tests + live-verified against the real
  host MCP (18 checks discovered, `disk` returned live `df -h`).
- **Phase 9c-1 — Enriched ingestion + hybrid retrieval (backend).** ⏭ Extend
  `chunks` (`document_id`, `context_prefix`, `metadata`, generated `tsv` + GIN
  index); contextual-retrieval enrichment + keyword/metadata extraction in the
  **shared** knowledge pipeline (CLI + future UI); hybrid `search_knowledge`
  (vector `<=>` + full-text `@@`, RRF-fused) with metadata-rich citations. Tests +
  live verify (re-ingest a corpus, confirm contextual + hybrid hits).
- **Phase 9c-2 — Document import UI + async jobs.** ⏭ `documents`/`ingest_jobs`
  tables; upload + job/status + corpora admin endpoints; single-instance in-process
  ingest worker; the Knowledge admin UI (upload, per-doc progress, browse/delete by
  corpus). Tests + live verify.

## 8. Deferred / future

- **Real SSO** (Google / Cloudflare+Okta JWT) replacing the password at the auth
  seam; multi-user admin + RBAC.
- **AWS-native**: auto-discovery (`describe-instances`) populating the registry;
  CloudWatch/CloudTrail correlation and SSM targeting keyed off instance-id /
  account / region.
- **Multi-replica** ingestion via a real job queue.
