# API reference

Every Devy surface is a thin client of this HTTP/SSE API. The proxy listens on
`:8765` by default; in the compose stack, nginx reverse-proxies `/v1` and
`/healthz` so the browser talks to a single origin.

> **Live, always-current docs:** FastAPI serves interactive OpenAPI at
> **`/docs`** (Swagger UI) and the schema at **`/openapi.json`**. This page is the
> hand-written overview; trust `/openapi.json` for exact field-level detail.

## Identity

History and recall are scoped to a user. Send identity as the **`X-User-Id`**
header (preferred) on any request; `/v1/chat` and `/v1/complete` also accept a
`user_id` body field. Identity is honor-system today — see
[Security → Identity](security.md#identity).

## Endpoints

### `GET /healthz`
Readiness probe.
```json
{ "status": "ok", "version": "0.1.0", "default_tier": "balanced" }
```

### `GET /v1/tiers`
The model tiers a user may select. **Labels only — the concrete model is hidden.**
```json
[ { "name": "fast", "label": "Fast (local)" },
  { "name": "balanced", "label": "Balanced" },
  { "name": "deep", "label": "Deep" } ]
```

### `GET /v1/tools`
Registered tool metadata (discovery surface).
```json
[ { "name": "host_diagnostics", "category": "host-diagnostics",
    "when_to_use": "…", "safety_tier": "read-only" } ]
```

### `GET /v1/whoami` — the caller's identity (auth-aware)
Feeds the web header (avatar chip + conditional Admin nav). In **jwt** mode returns
the **verified** identity decoded from the forwarded id_token; in password/dev mode
returns the honor-system name with `authenticated: false`.
```json
{ "authenticated": true, "mode": "jwt", "email": "you@example.com",
  "name": "You", "picture": "https://…", "roles": ["admin"], "is_admin": true }
```

### `POST /v1/chat` — multi-turn, streamed (SSE)
Request body:
```json
{ "message": "is the disk ok?", "session_id": "abc123",
  "tier": "balanced", "context": "optional piped/page context",
  "user_id": "darrell",
  "attachments": [ { "mime": "image/png", "data": "<base64>", "name": "graph.png" } ] }
```
`attachments` are optional images (screenshots, dashboards) — stored in the
content-addressed blob store and reasoned over as vision input; past-turn images
flatten to a one-time digest (see `GET /v1/blobs/{hash}`).
Returns a `text/event-stream`. Each event has an `event:` type and a JSON `data:`
payload:

| Event | Payload | Meaning |
|---|---|---|
| `session` | `{session_id}` | The session id (sent first; capture for the next turn) |
| `delta` | `{text}` | A streamed chunk of the answer |
| `tool_call` | `{name, arguments}` | The agent invoked a tool |
| `tools_found` | `{names[]}` | `find_tools` discovered these tools |
| `tool_result` | `{name, ok, preview}` | A tool returned (truncated preview) |
| `done` | `{iterations, usage, text}` | Turn complete; `text` is the final answer |
| `error` | `{message}` | Something failed mid-stream |

> Note: sse-starlette separates events with `\r\n\r\n`; a browser `EventSource`
> handles this, but a hand-rolled `fetch` reader should normalize CRLF→LF before
> splitting on a blank line. The [web client](../web/app.js) shows the pattern.

### `POST /v1/complete` — one-shot, non-streaming
Request body:
```json
{ "prompt": "summarize disk usage", "tier": "fast",
  "system": "optional system override", "context": "optional",
  "max_chars": 2000, "session_id": "optional", "user_id": "optional" }
```
Response:
```json
{ "markdown": "…", "tools_used": ["host_diagnostics"],
  "usage": { "total_tokens": 1276 }, "session_id": "abc123" }
```
A session is persisted only when `session_id` is supplied.

### `GET /v1/sessions` — list a user's conversations
Requires identity (`X-User-Id` header or `?user_id=`). `400` if absent.
```json
[ { "id": "abc123", "user_id": "darrell", "title": "DevOps Runbook Definition",
    "updated_at": "2026-06-14T03:54:24+00:00", "turns": 2,
    "preview": "what is a devops runbook?" } ]
```

### `GET /v1/sessions/{id}` — the faithful display transcript
```json
{ "id": "abc123", "user_id": "darrell", "title": "…",
  "messages": [ { "role": "user", "content": "…" },
                { "role": "assistant", "content": "…" } ] }
```
`404` if the session has no messages. This is the **display channel** — the
lossless transcript, not Devy's internal working context (see [Memory](memory.md)).

### `PATCH /v1/sessions/{id}` — rename
```json
// request:  { "title": "Checkout latency investigation" }
// response: { "id": "abc123", "title": "Checkout latency investigation" }
```
`400` on an empty title.

### `DELETE /v1/sessions/{id}` — delete
Removes the session and its conversation-memory rows.
```json
{ "id": "abc123", "deleted": true }
```

### `GET /v1/blobs/{hash}` — fetch a stored image
Content-addressed (sha256) fetch from the blob store — backs image attachments and
tool-rendered images (Devy embeds them inline as `![](/v1/blobs/<hash>)`). Returns
the image bytes with their stored content type.

### Guarded actions — human-approved remediations
Devy can **propose** a reversible remediation (via its `request_action` tool) but
never executes one itself; a person approves it here, and only then does the proxy
run it on the host MCP. Approving/denying requires the **`elevated`** tier (RBAC).
The plane is `503` unless guarded actions are enabled.

| Endpoint | Purpose |
|---|---|
| `GET /v1/actions` | List actions (optional `?session_id=`/`?status=`). Each: `{id, verb, args, rationale, reversibility, status, …}` |
| `POST /v1/actions/{id}/approve` | Approve → execute on the host MCP (CAS: only an unexpired `proposed` action runs; returns the executed action with `returncode`/`result`) |
| `POST /v1/actions/{id}/deny` | Deny a proposed action (no execution) |

Verbs are Tier-A reversible only (`restart_service` / `restart_container` /
`reload_config` / `prune_images`). See [Security → Guarded actions](security.md#guarded-mutating-actions).

## Admin control plane — `/v1/admin/*`

A **privileged** plane, separate from the assistant endpoints above, for managing
*what Devy can reach and know*: the host registry and the knowledge base. It is
**env-gated** — if `DEVY_ADMIN_PASSWORD_HASH` + `DEVY_ADMIN_SECRET` are unset,
every `/v1/admin/*` route returns **`503`** (admin disabled). See
[Security](security.md) and the [admin plan](plans/admin-control-plane.md).

### Auth

```
POST /v1/admin/login   { "password": "…" }  →  { "token": "…", "token_type": "bearer", "expires_in": 28800 }
```

Send the token as `Authorization: Bearer <token>` on every other `/v1/admin/*`
call. `401` on a missing/expired/invalid token; `503` if the plane is disabled.
`GET /v1/admin/me` → `{ "authenticated": true, "sub": "…", "scope": "…" }` (token
check; `sub`/`scope` are echoed from the token's claims).
The password hash + secret are generated by `agentic-devops admin set-password`
and live in `~/.config/agentic-devops/.env`. **This verifier is the SSO seam** —
a Google/Okta JWT verifier drops in here later.

### Host registry — `/v1/admin/hosts`

The fleet Devy reaches via each host's MCP. Per-host MCP tokens live in the
**secrets manager** (Phase S-1; the row holds a `secret_ref`, never the value) and
are **never returned** (only `has_token`).

| Method / path | Purpose |
|---|---|
| `GET /v1/admin/hosts` | List hosts (token never included). |
| `POST /v1/admin/hosts` | Create (**`201`**; **`409`** on duplicate `fqdn`). Body: `fqdn` (required), `private_ip`/`public_ip`, `instance_id`, `aws_account`/`aws_region`, `mcp_port` (8780), `mcp_scheme` (`https`\|`http`), `address_preference` (`private_ip`\|`public_ip`\|`fqdn`), `profile`, `active`, `labels`, `secret_ref` (override the manager name). Token is set on the **Secrets** tab, not here. |
| `GET /v1/admin/hosts/{id}` | One host. |
| `PATCH /v1/admin/hosts/{id}` | Update metadata. |
| `DELETE /v1/admin/hosts/{id}` | Remove. |
| `POST /v1/admin/hosts/{id}/check` | Test reachability → `{ "status": "reachable"\|"unreachable", "checks": [...] }`; updates `last_status`. |
| `GET /v1/admin/mcp-mounts` | Statically-mounted MCP servers from `config.yaml` (`mcp_servers`) as read-only **built-in** hosts — always includes the local host MCP on Devy's own network (a guaranteed test target). Each: `name`, `transport`, `address`, `url`, `reachable`, `checks`. Not removable (config-managed). |

### MCP servers registry — `/v1/admin/mcp-servers`

General external HTTP MCP tool sources (distinct from `hosts`), mountable at runtime.
**Read-only by default** (`allow_writes` opts in; a mounted tool that self-declares as
a writer via `readOnlyHint=false` is withheld from Devy unless allowed). Bearer tokens
live in the **secrets manager** (`secret_ref`, never returned).

| Method / path | Purpose |
|---|---|
| `GET /v1/admin/mcp-servers` | List registered servers. |
| `POST /v1/admin/mcp-servers` | Register (**`201`**). Body: `name` (not a reserved built-in category), `url`, `category`, `secret_ref`, `allow_writes`, and **`auth_header`** — the header carrying the bearer when it isn't `Authorization: Bearer` (e.g. `X-Grafana-Api-Key` for the Grafana MCP). |
| `PATCH /v1/admin/mcp-servers/{id}` | Update. |
| `DELETE /v1/admin/mcp-servers/{id}` | Remove. |
| `POST /v1/admin/mcp-servers/{id}/test` | Probe reachability + auth (invokes a read-only zero-arg tool to catch a masked upstream `401`). |
| `POST /v1/admin/mcp-servers/{id}/refresh` | Re-snapshot the server's tool list. |

### GitHub connector — `/v1/admin/github/*`

Credential-centric: register a read-only **PAT** once (stored in the vault under
`devy/github/<account>`, never returned); repos are discovered live via the API. Devy reads repos through
the read-only `repo_*` tools; an operator can crawl a repo's markdown into the
knowledge base on demand.

| Method / path | Purpose |
|---|---|
| `GET /v1/admin/github/accounts` | List accounts (token never included; `has_token` only). |
| `POST /v1/admin/github/accounts` | Create (**`201`**; **`409`** on duplicate `label`). Body: `label` (required), `login`, `default_corpus`, `active`, `labels`, `token` (write-only PAT). |
| `PATCH /v1/admin/github/accounts/{id}` | Update (token changed only if the `token` field is present). |
| `DELETE /v1/admin/github/accounts/{id}` | Remove. |
| `POST /v1/admin/github/accounts/{id}/test` | Verify the PAT → `{ "ok": true, "login": "…" }` (auto-fills `login`) or `{ "ok": false, "error": "…" }`. |
| `GET /v1/admin/github/repos[?account=…]` | Live-list accessible repos (name it when several accounts). |
| `POST /v1/admin/github/crawl` | Body: `repo` (`owner/name`), optional `corpus`/`account`. Fetches the repo's markdown via the API → OKF + redaction ingest → `{ "corpus", "files_ingested", "files_skipped", "files_quarantined", "chunks_written", "secrets_redacted", "commit_sha", "default_branch" }`. Records the crawl (commit + counts) in the scan history. |
| `GET /v1/admin/github/crawls` | Scan history — one row per crawled repo (most-recent first): the last-run fields (`commit_sha`, `default_branch`, `files_ingested`, `chunks_written`, `files_quarantined`, `secrets_redacted`, `crawled_at`) plus the **live** corpus footprint `doc_count`/`chunk_count` (current totals, computed per request, not last-run deltas). Powers the "Scanned repos" table. |

#### Doc generation — `/v1/admin/github/docgen` (Phase D-2)

Devy reads a repo's **code** and writes OKF architecture docs (one per discovered
component), redacts them before disk, and ingests them into a `gen:<repo>` corpus.
Diff-driven: a repo unchanged since the last run is skipped (zero model calls).
Gated by `knowledge.docgen_enabled` (default off → `400`).

| Endpoint | Behaviour |
| --- | --- |
| `POST /v1/admin/github/docgen` | Trigger. Body: `repo` (`owner/name`, required), optional `components` (limit to these paths), `brief` (scan guidance, persisted + fed to the generator), `force` (regenerate even if unchanged). Generation is many sequential model calls, so it runs in a **background thread** — returns `{ "repo", "started": true }` immediately; poll the GET below for progress. `400` if disabled, `404` if no GitHub account owns the repo. |
| `GET /v1/admin/github/docgen` | Per-repo status + components: `full_name`, `status` (`idle`/`running`/`error`), `last_doc_sha`, `scan_brief`, `components_doced`, `error`, and `components[]` (`component_path`, `component_name`, `kind`, `status`, `arch_doc_path`, `last_doc_sha`). Powers the generated-docs table. |
| `PUT /v1/admin/github/docgen/brief` | Persist a repo's scan brief without triggering a run. Body: `repo`, `brief`. Returns the updated record. |

### Secrets / Connections — `/v1/admin/secrets` (Phase S-2)

The unified credential inventory: provider/service keys (Anthropic, OpenAI,
Tavily, LangSmith) plus the connector tokens (GitHub, hosts). **Values are never
returned** — only loaded-state and a live Test. In **dev** the Secrets tab is the
single write-point for secret *values* (provider keys AND connector tokens); the
connector tabs own the *metadata* (account/host rows) and derive the `secret_ref`.
Writes are refused (`403`) in **prod** (secrets provisioned out-of-band). Provider
keys carry an `env` (hydrated into `os.environ`); connector tokens are resolved
on-demand and have no env var.

| Endpoint | Behaviour |
| --- | --- |
| `GET /v1/admin/secrets` | The catalog: `{ mode, writable, reachable, secrets[] }`. Each entry: `service`, `label`, `ref`, `category` (`provider`/`github`/`host`), `env` (null for connectors), `loaded`, `editable`, `testable`. |
| `PUT /v1/admin/secrets` | Set a secret value. Body: `{ ref, value }`. `403` in prod; `400` for an unknown `ref` (register the account/host on its tab first). For a provider key, re-hydrates the matching env var so it takes effect without a restart. |
| `DELETE /v1/admin/secrets?ref=…` | Clear a secret (dev only; `403` in prod). |
| `POST /v1/admin/secrets/test` | Live-validate a secret without revealing it. Body: `{ ref }` → `{ ok, detail }`. Provider keys → a lightweight authenticated call (e.g. Anthropic/OpenAI models list); GitHub → `whoami`; host → MCP list-tools ping. |

### Document import — `/v1/admin/documents`, `/jobs`, `/corpora`

UI-driven markdown ingest into the hybrid knowledge base (chunk → context →
embed → store), processed by the in-process ingest worker.

| Method / path | Purpose |
|---|---|
| `GET /v1/admin/documents[?corpus=…]` | List documents (no raw content). |
| `POST /v1/admin/documents` | **multipart/form-data** (**`201`**): `corpus` (field) + `files` (one or more `.md`/`.markdown`). Registers pending documents + a queued job → `{ "job": {...}, "documents": [...] }`. Markdown-only; `400` otherwise. |
| `DELETE /v1/admin/documents/{id}` | Delete a document and its chunks (cascade). |
| `GET /v1/admin/jobs/{id}` | Poll ingest progress → `{ "status": "queued\|running\|done\|failed", "total", "done", "error" }`. |
| `GET /v1/admin/corpora` | `[ { "name", "documents", "chunks" } ]` (live counts). |
| `DELETE /v1/admin/corpora/{corpus}` | Delete a whole corpus (documents + chunks). |

## Errors

Standard HTTP status codes with a JSON `{ "detail": "…" }` body (FastAPI
convention): `400` (bad request / unknown tier / missing identity / non-markdown
upload), `401` (admin token missing/invalid), `404` (session / host / document /
job not found), `409` (duplicate host `fqdn`), `503` (admin plane disabled).
Successful creates (`POST /v1/admin/hosts`, `POST /v1/admin/documents`) return
`201`. Mid-stream failures on `/v1/chat` arrive as an `error` event rather than a
non-200 status.
