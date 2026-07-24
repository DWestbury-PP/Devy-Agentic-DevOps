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
- **Read-only by default.** The packaged allow-list contains **no shell verbs** —
  no `docker exec/run/rm`, no arbitrary `cat`/`tail`, no `dmesg`. The boundary is
  the allow-list itself, not the Docker socket's mount mode. A small set of
  **reversible** mutating verbs (restart a service/container, reload config, prune
  images) exists, but they are **off unless the deployment opts in**
  (`HOST_MCP_ALLOW_MUTATIONS`) and only ever run through the human-approved
  [guarded-action path](#guarded-mutating-actions) — never on the agent's own say-so.
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

### Secrets model

Secrets fall into **two planes**, handled deliberately differently:

**Plane 1 — Bootstrap** (environment / platform injection / IaC). The minimal set
the platform needs to *come up and be administrable*. They can't be managed from
the admin UI because the platform isn't up yet — a bootstrap paradox — so they
live in the environment:
- `DATABASE_URL` (+ `POSTGRES_PASSWORD` for the bundled DB) — Devy can't start without it.
- `DEVY_ADMIN_PASSWORD_HASH` + `DEVY_ADMIN_SECRET` — they gate the admin plane
  *itself*; you can't set the admin-access secret through admin access.
- Vault access: `DEVY_MODE` and, in dev, the LocalStack `AWS_*` wiring. In prod
  this is an **instance IAM role** — no key at rest.

**Plane 2 — Runtime external-service credentials** (the **vault**, managed on the
admin **Secrets tab**). Everything Devy *reaches out to*, manageable while the
platform runs: provider keys (`devy/provider/*` — Anthropic, OpenAI, Gemini,
Tavily, LangSmith), GitHub PATs (`devy/github/*`), host and MCP bearer tokens
(`devy/host/*`, `devy/mcp/*`). **The vault is authoritative** — if a Plane-2 key
is also present in `.env`, the proxy logs a warning at startup and the vault value
wins (no silent shadowing). Provider keys are hydrated into the environment at
boot (for the provider SDKs); MCP bearers are resolved at mount time via a config
`secret_ref` — so no Plane-2 secret needs to sit in `config.yaml` or `.env`.

> One nuance: the deployable **host-MCP sidecar** is a standalone package with no
> vault client, so *it* reads its own `HOST_MCP_TOKEN` from its environment/token
> file — that copy is the **server's** own credential, provisioned to match the
> vault master (`devy/mcp/host`) that the **proxy** (the client) resolves. The
> vault remains the source of truth; the server just holds a provisioned copy.

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
  (host checks), `admin` → +elevated (proposing a [guarded action](#guarded-mutating-actions),
  opted-in MCP writes). The harness refuses an over-tier tool with a clear message. On
  the chat plane, the tier comes from the verified JWT role in jwt mode, or
  `rbac.assistant_role` (default `admin` = unrestricted) when identity isn't verified —
  tighten it if you run chat without SSO. **Approving** an action requires the
  `elevated` tier too.

Generate the password-mode secrets with `agentic-devops admin set-password`; they
live in `~/.config/agentic-devops/.env` (never committed).

## Guarded mutating actions

Devy can help *fix* things, not just report them — but the write path is built so a
prompt injection (or a bad-day model) cannot mutate infrastructure on its own. Devy
**proposes**; a human **approves**; the proxy **executes**. There is **no tool that
mutates directly** — the propose/approve split is structural, so "never self-approve"
isn't a rule the model can be talked out of.

Three independent gates must all be open for a mutation to run:

1. **Deployment switch.** The host MCP sidecar refuses every mutating verb unless it
   was started with **`HOST_MCP_ALLOW_MUTATIONS`** — a dedicated, default-off switch
   orthogonal to the profile, so a SecOps team controls whether *any* write is even
   possible on a given host.
2. **RBAC — `elevated` tier.** Only an `elevated`-tier caller can approve an action
   (`POST /v1/actions/{id}/approve`). Viewers/operators can see proposals but not run
   them.
3. **Per-action human approval.** Each proposal is a `pending_actions` row with a TTL;
   approval is a compare-and-set (only one unexpired `proposed` action executes), and
   the whole set is **fail-closed** — the actions plane won't enable without
   `auth.mode: jwt` (or an explicit dev opt-in), so it stays off through the
   unauthenticated bootstrap.

Only **reversible** verbs exist (`restart_service`/`restart_container`/`reload_config`/
`prune_images`) — no `rm`, volume deletion, or shell, locked by test. As a
defence-in-depth backstop, the host MCP annotates each tool with **`readOnlyHint`**, and
the proxy **withholds any mounted tool that self-declares as a writer** (`readOnlyHint=false`)
from the agent's tool set — so even a mounted third-party MCP can't hand Devy a direct
write verb.

## Agent / prompt-injection posture

Devy reads data from tools, documents, and (potentially) untrusted systems. Two
mitigations are built in, and one is your responsibility:

- **Tools are the guardrail.** Because host access is an allow-list of scoped
  checks, a prompt-injection attempt in a log line or document can't escalate into
  arbitrary execution — no tool runs arbitrary commands, and the only mutating verbs
  are reversible ones that a human must approve (see [Guarded mutating
  actions](#guarded-mutating-actions)). A prompt injection can, at most, cause Devy
  to *propose* an action — which a person still has to approve before it runs.
- **Findings are data, not control.** Tool output is fed back as content the model
  reasons over, and stored as plain text — it is never executed.
- **Your responsibility:** vet the **MCP servers** you mount and the **profile**
  you grant. A third-party MCP server you mount, or an `elevated` profile you
  enable, expands what the agent can do — review them like any dependency.

## Reporting

Please report security issues privately — see [SECURITY.md](../SECURITY.md). Do
not open public issues for vulnerabilities.
