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

### LangSmith tracing (data egress)

Setting `tracing: langsmith` sends a **waterfall** of each turn — its LLM calls
and its tool calls — to LangSmith's cloud (`api.smith.langchain.com`, or your
self-hosted endpoint). This is real data egress, so it's **off by default** and
gated on both the setting and a key you set on the Secrets tab
(`devy/provider/langsmith`).

What leaves the process is controlled by `langsmith.capture`, which — left unset —
**follows `DEVY_MODE`**:

- **`dev` → `full`**: prompts, completions, and **tool outputs** (which can include
  live host diagnostics and knowledge-base content). Best for building and
  stress-testing the harness. Use only with a private LangSmith project.
- **`prod` → `metadata`**: only span names, timings, success/failure, and token
  usage — **no prompt, completion, or tool-output bodies** leave the process.

Note the secret-redaction gate runs at *ingest*, not over live tool output — so in
`full` mode, unredacted host/infra data in tool results will reach LangSmith. Prefer `metadata` (or leave tracing off) in any
environment where that matters. Pin `langsmith.capture` explicitly to override the
mode-derived default.

## Secrets management

Every external credential (LLM/provider keys, GitHub PATs, per-host MCP tokens,
MCP-server bearer tokens) resolves through **one AWS Secrets Manager API surface**
— LocalStack in `dev`, real AWS SM in `prod`. The single knob is `DEVY_MODE`.

- **Nothing secret lives in Devy's database.** Registry rows (`hosts`,
  `github_accounts`, `mcp_servers`) hold only a **`secret_ref`** — the *name* of
  the secret in the manager, never the value. The API returns loaded-state
  (`has_token`) and a live **Test**, never the value.
- **`prod` posture (the defensible one):** the app authenticates to AWS SM via the
  ambient **instance IAM role** — there is **no bootstrap key at rest**. Secrets are
  **provisioned out-of-band** by your IaC; the admin UI is **test-only** (writes
  return `403`, enforced server-side).
- **`dev` posture:** a bundled LocalStack stands in for AWS SM so the resolve path
  is identical to prod. The admin UI can set/test; writes mirror to a local file and
  re-hydrate LocalStack on boot (it doesn't persist). This is a convenience, **not**
  a stronger boundary — a local secret is readable by whoever owns the host.
- **Caching + rotation:** resolved values are cached for `secrets.cache_ttl`
  seconds (default 60) to bound AWS SM calls/latency/cost on the hot path; writes
  invalidate, and an externally-rotated secret is picked up within the TTL.
- **Audit trail:** with `secrets.audit_enabled` (default on), every secret op
  (set / delete / test / resolve-on-fetch) appends a **value-free** line to
  `trace_dir/secrets-audit.jsonl` — the "who touched which secret when" record.

**Least-privilege IAM policy** (attach to Devy's instance/task role) — read-only,
scoped to the `devy/*` namespace:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
    "Resource": "arn:aws:secretsmanager:*:*:secret:devy/*"
  }]
}
```

Devy never needs `CreateSecret`/`PutSecretValue` in prod — provision out-of-band:

```hcl
resource "aws_secretsmanager_secret" "github_home" {
  name = "devy/github/home"
}
resource "aws_secretsmanager_secret_version" "github_home" {
  secret_id     = aws_secretsmanager_secret.github_home.id
  secret_string = var.github_home_pat   # from your TF vars / a secrets pipeline
}
```

Register the connector (metadata only) in the admin UI, then **Test** it — Devy
resolves `devy/github/home` from AWS SM via the role. Ref naming:
`devy/provider/<svc>`, `devy/github/<label>`, `devy/host/<fqdn>`, `devy/mcp/<name>`.

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

- **Off unless configured.** In `password` mode, if `DEVY_ADMIN_PASSWORD_HASH` /
  `DEVY_ADMIN_SECRET` are unset every `/v1/admin/*` route returns `503`. The
  assistant plane runs fine with admin disabled.
- **Two auth modes + roles (RBAC-1).** `auth.mode: password` (dev/interim) is a
  single operator password (bcrypt) exchanged for a short-lived HS256 token via
  `POST /v1/admin/login`; that token grants the `admin` role. `auth.mode: jwt`
  (prod) verifies a **forward-auth JWT** from your edge proxy against the IdP
  **JWKS** (issuer/audience/signature), reads `email` + `groups`, and maps groups
  to Devy roles (`rbac.group_roles`). The `require_role(...)` dependency gates every
  admin route (`403` if the role is missing, `401` if unauthenticated). In jwt mode
  there is no login endpoint — identity comes from the proxy.

  ```yaml
  auth:
    mode: jwt
    jwks_url: https://<your-idp>/.well-known/jwks.json
    issuer:  https://<your-idp>/
    audience: devy-admin
    header:  Authorization        # or e.g. Cf-Access-Jwt-Assertion
    groups_claim: groups
  rbac:
    group_roles: { devy-admins: admin, devy-operators: operator, devy-viewers: viewer }
  ```
- **Secrets at rest.** Connector/host tokens live in the **secrets manager** (see
  [Secrets management](#secrets-management)) — the registry row holds only a
  `secret_ref`, and the API returns `has_token`, never the value. The agent passes an
  *identifier*; the proxy resolves the secret server-side, so the model never sees it.
- **Audit actor.** Secret operations are recorded with the caller's identity — the
  verified `email` in jwt mode (`admin`/`system` in password mode).
- **Role-gated tools (RBAC-2).** Each role caps the tool **safety tier** the agent
  may invoke on that caller's behalf: `viewer` → read-only, `operator` → +diagnostic
  (host checks), `admin` → +elevated (e.g. opted-in MCP writes). The harness refuses
  an over-tier tool with a clear message. On the chat plane, the tier comes from the
  verified JWT role in jwt mode, or `rbac.assistant_role` (default `admin` =
  unrestricted) when identity isn't verified — tighten it if you run chat without SSO.

Generate the password-mode secrets with `agentic-devops admin set-password`; they
live in `~/.config/agentic-devops/.env` (never committed).

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
