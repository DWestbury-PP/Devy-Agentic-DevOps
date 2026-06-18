# Security

Devy is designed to be **adoptable on real infrastructure** — which means the
security posture is a first-class design concern, not an afterthought. This page
describes the boundaries, the data it handles, and the current limits. To report
a vulnerability, see [SECURITY.md](../SECURITY.md).

## The host-MCP boundary (the core safety story)

The single most important property: **the agent never gets a shell on your
hosts.** All host and container inspection goes through the
[host MCP](../host-mcp/README.md), which is safe by construction:

- **Declarative allow-list.** Each check is a fixed `argv` (or per-OS `argv`).
  Arguments can only fill a whole `{placeholder}` token, and only after passing
  type / pattern / enum / range constraints. **No shell is ever invoked**, so
  there is no command injection surface.
- **Profile-gated.** The server runs at one active profile —
  `read-only` < `diagnostic` < `elevated` — and exposes only the checks at or
  below it. Run target hosts at the lowest profile that does the job.
- **Read-only by default.** The packaged allow-list contains **no mutating or
  shell verbs** — no `docker exec/run/rm/stop`, no arbitrary `cat`/`tail`, no
  `dmesg`. The boundary is the allow-list itself, not the Docker socket's mount
  mode.
- **Authenticated & audited.** The HTTP transport requires a bearer token (front
  it with TLS); every invocation (check, args, resolved `argv`, exit, duration)
  can be appended to a JSONL audit log.

This is what makes pointing Devy at a *production* host reviewable: a SecOps team
can read the allow-list and know the complete set of things the agent can do.

## Network posture

- In the compose stack the **proxy and web chat bind to host loopback**
  (`127.0.0.1`); they are not exposed on the network. The host MCP is reachable
  only on the compose network. Put SSO / a reverse proxy / a VPN in front for
  shared access.
- Remote host MCPs should be fronted with **TLS**; the bearer token is the authn,
  the allow-list is the authz.

## Data handling & privacy

Devy stores, in **your** Postgres (bundled or managed):

- **Conversation transcripts** (`sessions`) — the lossless user/assistant history.
- **Distilled tool findings** and a **structured summary** (Devy's working memory).
- **Per-exchange embeddings** (`conversation_memories`) powering `recall_history`.
- **Knowledge-base chunks + embeddings** (`chunks`) from documents you ingest.

Controls:

- **`knowledge.history_enabled: false`** turns off conversation-memory embedding
  entirely — nothing is stored for retrieval (a privacy off-switch).
- **`DELETE /v1/sessions/{id}`** removes a conversation *and* its memory rows.
- Data never leaves your database except as context sent to your configured
  **model/embedding provider** — choose providers (or local Ollama) accordingly.
- No telemetry is sent anywhere by default; agent tracing is local JSONL unless
  you opt into LangSmith.

## Identity

> **The honor-system `user_id` is scoping, not authentication.** Treat the
> current build as single-tenant / trusted-network until you wire real auth.

History and recall are scoped by an `X-User-Id` header (the web chat stores a name
in `localStorage`). Anyone who can set that header can act as any user. The seam
to fix this is in place and centralized:

- **Client:** `authHeaders()` in [`web/app.js`](../web/app.js) is the one place
  that produces the identity header — swap it to attach a verified token.
- **Server:** add a dependency that verifies a JWT (e.g. a Cloudflare+Okta or
  Google ID token) and sets `user_id` from a trusted `email` claim; tools receive
  identity only through the `wants_context` channel, never from model arguments.

Until then, deploy behind your own SSO / reverse proxy and don't expose Devy
directly to untrusted users.

## Admin control plane

The privileged `/v1/admin/*` plane (host registry, document import) is **separate
from the assistant endpoints** and **off unless explicitly configured**:

- **Env-gated.** If `DEVY_ADMIN_PASSWORD_HASH` and `DEVY_ADMIN_SECRET` are unset,
  every `/v1/admin/*` route returns `503`. The assistant plane runs fine with
  admin disabled.
- **Interim auth = the SSO seam.** Today it's a single operator password (bcrypt
  hash) exchanged for a short-lived HS256 bearer token via `POST /v1/admin/login`.
  This is a deliberate placeholder: the `require_admin` dependency is the **one
  seam** where a real JWT verifier (Google/Okta) drops in — the same seam described
  under [Identity](#identity).
- **Secrets at rest.** Per-host MCP tokens in the registry are **Fernet-encrypted**
  (`DEVY_ENCRYPTION_KEY`) and **never returned** by the API (only `has_token`). The
  agent passes a *host identifier*; the proxy resolves and decrypts the token
  server-side, so the model never sees it.

Generate the secrets with `agentic-devops admin set-password` / `admin gen-key`;
they live in `~/.config/agentic-devops/.env` (never committed).

## Agent / prompt-injection posture

Devy reads data from tools, documents, and (potentially) untrusted systems. Two
mitigations are built in, and one is your responsibility:

- **Tools are the guardrail.** Because host access is an allow-list of read-only
  checks, a prompt-injection attempt in a log line or document can't escalate into
  arbitrary execution — there is no tool that runs arbitrary commands.
- **Findings are data, not control.** Tool output is fed back as content the model
  reasons over, and stored as plain text — it is never executed.
- **Your responsibility:** vet the **MCP servers** you mount and the **profile**
  you grant. A third-party MCP server you mount, or an `elevated` profile you
  enable, expands what the agent can do — review them like any dependency.

## Reporting

Please report security issues privately — see [SECURITY.md](../SECURITY.md). Do
not open public issues for vulnerabilities.
