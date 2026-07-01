# `ask` — web surface

A terminal-themed browser client for the LLM-PROXY. Like the Go `ask` TUI, it's
a **thin client of the proxy API** — it owns no agent logic. It POSTs to
`/v1/chat` and renders the SSE event stream (streamed text, tool-call trail,
`search_knowledge` citations, then a polished Markdown/Mermaid render of the
final answer).

## Run it

It ships as a service in the repo's `docker-compose.yml`:

```bash
docker compose up -d --build      # starts the full stack (postgres + proxy + host-mcp + chat-ui)
open http://127.0.0.1:8080        # the web surface
```

`chat-ui` is an nginx container that serves these static assets **and
reverse-proxies `/v1` + `/healthz` to the `proxy` service** over the compose
network — so the browser talks to a single origin (no CORS, no proxy changes).

## Design notes

- **No build step / no Node.** Plain HTML/CSS/JS. The Markdown (marked),
  sanitiser (DOMPurify), syntax highlighter (highlight.js), and diagram
  renderer (mermaid) are fetched **at image-build time** (pinned in the
  `Dockerfile`), so the running container makes **no external calls**.
- **Tier, not model.** The tier selector is populated from `/v1/tiers`; the user
  picks a tier (`/model deep` also works), never the concrete model.
- **Shows the differentiators.** On-demand `find_tools` discovery and
  `search_knowledge` retrieval appear as an inline tool trail with collapsible
  results — the things the TUI deliberately hides.
- **Conversation history.** A **slide-out** (`☰ history`) lists your past
  conversations with auto-generated titles; load / rename / delete each. **`⧉
  copy`** exports the whole conversation as Markdown, and each message has its own
  copy button. Menu icons are inline [Lucide](https://lucide.dev) SVGs (no emoji).
- **Identity (honor-system).** History is scoped to a name kept in `localStorage`
  and sent as the `X-User-Id` header. `authHeaders()` in `app.js` is the single
  seam where a real provider (Google auth, a Cloudflare+Okta JWT) drops in — see
  [docs/security.md](../docs/security.md#identity).
- **Slash commands** (UI only — Devy's *tools* are invoked by asking in plain
  language): `/model <tier>`, `/models`, `/tools`, `/new`, `/clear`, `/help`.

## Admin control plane

A separate **privileged** page — [`admin.html`](admin.html) +
[`admin.js`](admin.js), served by the same nginx at
`http://127.0.0.1:8080/admin.html`. Password sign-in (`POST /v1/admin/login`)
unlocks three tabs:

- **Hosts** — the host-MCP registry: add / edit / remove the hosts Devy can run
  diagnostics against, and test reachability. Per-host MCP tokens are stored
  encrypted and never shown back.
- **Repos** — the GitHub connector: register a read-only PAT (stored encrypted),
  test it, and crawl a repo's Markdown into the knowledge base on demand. A
  **Scanned repos** table records what's been crawled, when, and at which commit
  (with one-click Rescan). Devy reads repos for triage/RCA via the read-only
  `repo_*` tools. A **Generate component docs** subsection has Devy read a repo's
  *code* and author OKF architecture docs (diff-driven — unchanged repos cost
  nothing; an optional scan brief steers the generator); a status table tracks
  each component as it completes.
- **Knowledge** — document import: upload Markdown into a corpus (chunked,
  enriched, embedded), watch ingest jobs, and list / delete documents and corpora.
- **Secrets** — the unified credential inventory (Phase S-2): provider/service keys
  (Anthropic, OpenAI, Tavily, LangSmith) + connector tokens (GitHub, hosts), each
  with loaded-state and a live **Test**. Values are never shown; provider keys are
  editable here in dev, read-only (test-only) in prod. Backed by the secrets manager
  (LocalStack in dev, AWS SM in prod).

It reuses the same terminal theme and is **gated by the admin env secrets**
(`DEVY_ADMIN_PASSWORD_HASH` + `DEVY_ADMIN_SECRET`) — if they're unset the plane
returns `503` and the page shows a "not configured" notice. See
[docs/api.md](../docs/api.md#admin-control-plane--v1admin) for the endpoints.

## Develop without rebuilding the image

Serve the static files against a running proxy (so `/v1` resolves), e.g. with a
tiny dev proxy, or just rebuild the one container:

```bash
docker compose up -d --build chat-ui
```
