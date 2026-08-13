# Deployment

Devy ships as a small set of containers plus an optional native binary. This
guide covers the bundled stack, using a managed database, running natively, and
production hardening.

## The compose stack

[`docker-compose-local.yml`](../docker-compose-local.yml) defines six services (the
self-contained AWS deploy variant is [`docker-compose-aws.yml`](../docker-compose-aws.yml)):

| Service | Role | Exposed |
|---|---|---|
| `postgres` | Postgres + pgvector (sessions, knowledge, memory) | compose network only |
| `proxy` | the LLM-PROXY | `127.0.0.1:8765` (host loopback) |
| `localstack` | **dev secrets vault + blob store** (Secrets Manager + S3) | `127.0.0.1:4566` (host loopback) |
| `host-mcp` | safe-allowlist host + Docker diagnostics | compose network only (`:8780`) |
| `grafana-mcp` | mounted read-only Grafana MCP (opt-in; needs `GRAFANA_URL`) | compose network only (`:8000`) |
| `chat-ui` | nginx serving the web chat + reverse-proxying the API | `127.0.0.1:8080` |

Plus a `demo-faulty` service behind the `demo` profile (the crash-loop RCA demo).

**The AWS variant is a different service set — not this file with an overlay.**
[`docker-compose-aws.yml`](../docker-compose-aws.yml) is self-contained and shipped to
the host by the CD pipeline (Devy's `deploy/` role). What changes:

| | Local | AWS |
|---|---|---|
| `host-mcp` | container (demo only) | **native systemd unit** per host, so `host_*` checks see the real host — deployed by [`host-mcp-deploy.yml`](../.github/workflows/host-mcp-deploy.yml); reached at `host.docker.internal:8781` |
| `localstack` | the dev vault + blob store | **absent** — `DEVY_MODE=prod`, real Secrets Manager + S3 via the instance IAM role |
| `demo-faulty` | `demo` profile | absent |
| images | `build:` from the repo | pinned ECR tags (`DEVY_{PROXY,CHAT_UI}_IMAGE`), built by [`build.yml`](../.github/workflows/build.yml) |
| Postgres init | `001_baseline.sql` bind-mount | no mount — `db migrate` brings a fresh DB to head |
| driven by | `./devy.sh` | [`deploy.yml`](../.github/workflows/deploy.yml) → Ansible over SSM |

`./devy.sh` is **local only** and must never be pointed at a deploy host. See
[Deploy design](deploy-design.md) for the CI→CD seam.

> **There is no `docker-compose.yml` in this repo** — the compose files are
> `docker-compose-local.yml`, `docker-compose-aws.yml`, and the `docker-compose.auth.yml`
> overlay. A bare `docker compose …` therefore fails with *"no configuration file
> provided: not found"*. Use `./devy.sh` (below), or pass `-f docker-compose-local.yml`
> explicitly.

```bash
./devy.sh --no-auth up              # build + start in password mode, then migrate the DB
./devy.sh logs -f                   # follow logs
./devy.sh down                      # stop (keeps the DB volume)
./devy.sh down -v                   # stop AND drop the DB volume (guarded — confirms first)
./devy.sh rebuild chat-ui           # rebuild just the web surface after edits
```

### `./devy.sh` — the canonical wrapper (use this)

Raw `docker compose` is easy to get wrong once the **SSO overlay** is in play: run
`docker compose up -d` *without* `-f docker-compose.auth.yml` and the proxy loses
`OAUTH2_PROXY_CLIENT_ID`, so the JWT `audience` check fails ("Audience doesn't
match") and login **silently breaks**. [`./devy.sh`](../devy.sh) assembles the right
`-f` files (and mode env) for you and prints a banner so you always see what's
included:

```bash
./devy.sh up                 # start (dev + SSO edge). alias for: up -d
./devy.sh rebuild chat-ui    # rebuild + restart one service
./devy.sh logs proxy         # follow logs
./devy.sh psql               # psql into the app DB
./devy.sh doctor             # ps + a mode/.env preflight
./devy.sh mode               # print active mode + compose files
./devy.sh down -v            # (guarded — confirms before dropping the DB volume)
./devy.sh <any compose subcommand> …   # ps, exec, images, config, restart, …
```

`./devy.sh` is **local dev only** — `docker-compose-local.yml` + the SSO overlay, with
LocalStack for secrets/S3. `--no-auth` runs the local stack without the SSO edge
(password-mode bootstrap / break-glass); `--no-migrate` skips the post-`up` `db migrate`.
Pure bash, no dependencies. The **AWS deploy is a separate concern** — the CD pipeline
(Devy's `deploy/` role) ships the self-contained `docker-compose-aws.yml`; it is not
driven by this wrapper.

Config and secrets are read from a mounted directory (default
`~/.config/agentic-devops`, override with `$AGENTIC_DEVOPS_CONFIG_DIR`) — the same
`config.yaml` + `.env` a native install uses. Compose reads a `.env` next to
`docker-compose-local.yml` for `HOST_MCP_TOKEN`, `POSTGRES_PASSWORD`, and `DATABASE_URL`.

> **Enabling the admin control plane** (host registry + document import) needs two
> credentials — `devy/admin/password-hash` + `devy/admin/secret`, both required, else
> `/v1/admin/*` → `503`. Since #117 they are **vault-mastered** like every other
> secret: generate the pair with `agentic-devops admin set-password` (it *prints* them;
> it does not write any file), then store both with `agentic-devops secrets set`. They
> hydrate into `DEVY_ADMIN_PASSWORD_HASH` / `DEVY_ADMIN_SECRET` at boot.
>
> A copy in the mounted `~/.config/agentic-devops/.env` still works as a fallback, but
> the vault wins when both are set (the proxy logs a warning). Connector/provider
> tokens are managed *in* the vault via the admin Secrets tab. See
> [Security → Setting the admin credentials](security.md#setting-the-admin-credentials-runbook)
> for the full runbook, or [Bootstrapping from a cold clone](bootstrap.md) for the
> container-only path that needs no local Python.

```bash
# one-time: a shared token for the host MCP
echo "HOST_MCP_TOKEN=$(openssl rand -hex 24)" >> .env
```

Bound to host **loopback** by design — the proxy and web chat are not exposed on
the network. Put a reverse proxy / VPN / SSO in front for shared access.

## Database: bundled or managed

The DSN (`database.url` / `$DATABASE_URL`) is the single switch.

**Bundled (zero setup):** the compose `postgres` service uses the `pgvector` image.
Schema *evolution* is owned by `agentic-devops db migrate` in both worlds (see
[DB migrations](db-migrations.md)); only the **first-init** path differs:

| | Fresh-volume bootstrap |
|---|---|
| **Local** (`docker-compose-local.yml`) | [`001_baseline.sql`](../src/agentic_devops/db/migrations/001_baseline.sql) is bind-mounted into the image's `docker-entrypoint-initdb.d`, so a brand-new volume self-bootstraps; `./devy.sh up` then runs `db migrate` |
| **AWS** (`docker-compose-aws.yml`) | **no init mount** — a deploy host has no source checkout, so a fresh DB is brought to head entirely by the gated `db migrate` pipeline step |

Data persists in the `agentic-pgdata` volume either way.

**Managed (RDS / Aurora / Cloud SQL / …):**

Below is the **local** form. On AWS this is the normal case and the CD pipeline already
does it — `DATABASE_URL` comes from the rendered `.env` the `devy` role ships, and
`db migrate` runs as a gated deploy step. Don't run `./devy.sh` against a deploy host;
it is local-dev only.

```bash
# 1. point the proxy (and CLI) at your instance
export DATABASE_URL=postgresql://USER:PASS@your-db.example.com:5432/agentic
# 2. provision it once (needs a role allowed to CREATE EXTENSION vector)
agentic-devops db init
# 3. start only the app services (skip the bundled DB)
./devy.sh --no-auth up proxy host-mcp chat-ui localstack
```

`db init` is idempotent. The proxy also applies the schema best-effort on startup,
so a least-privilege app role still works once an admin has run `db init`. The
`vector` extension is the only special requirement (available on RDS/Aurora as
`CREATE EXTENSION vector`).

## Native (inspecting your own machine)

The proxy can run natively — useful because a *containerized* proxy's builtin
`host_diagnostics` sees the container, not your host.

```bash
python -m pip install -e ".[dev]"
export DATABASE_URL=postgresql://…        # a local or bundled Postgres
agentic-devops db init                    # if not already provisioned
agentic-devops serve                      # http://127.0.0.1:8765
```

> Editable install + a space in the repo path can trip the `.pth` finder; if the
> console script raises `ModuleNotFoundError`, run
> `PYTHONPATH=src python -m agentic_devops.cli.main serve`.

## Deploying the host MCP on real hosts

For true host-level inspection of a remote box, deploy the
**[host MCP](../host-mcp/README.md)** natively on that host and mount it from the
proxy over authenticated HTTP (front it with TLS). The proxy never gets shell —
only the allow-listed, profile-gated checks. See the
[host MCP README](../host-mcp/README.md) and [Security](security.md).

## Authentication: bootstrap first, then Google SSO

Devy starts in **`auth.mode: password`** (the default) — a fresh deployment runs with
zero identity setup so the operator can get in and configure everything. Google SSO is
an **additive upgrade** you flip on when ready. You can't use SSO to configure SSO, so
**never delete password mode** — it stays your break-glass way back in.

**Order of operations for a new deployment:**

1. **Deploy** the base stack (`./devy.sh --no-auth up`). `auth.mode: password`.
2. **Set the admin password:** `agentic-devops admin set-password` **prints** a bcrypt
   hash and a signing secret; store both in the vault with `agentic-devops secrets set`
   (`devy/admin/password-hash`, `devy/admin/secret`), then restart the proxy. The admin
   console is now reachable in password mode. Step-by-step:
   [Bootstrapping from a cold clone](bootstrap.md#4-admin-credentials).
3. **Configure** from the admin console: provider keys / host MCP (Secrets tab), your
   `rbac.email_roles` (who becomes admin/operator/viewer under SSO), and the Google OAuth
   client (below).
4. **Turn on SSO:** set `auth.mode: jwt` in `config.yaml` and bring the stack up with the
   auth overlay. Everyone now logs in with Google; your email maps to `admin`.
5. **Break-glass:** if SSO ever breaks, revert `auth.mode: password` to get back in.

Guarded actions are **fail-closed on `auth.mode: jwt`**, so they stay off automatically
through the unauthenticated bootstrap — no accidental exposure during setup.

### Google SSO via the oauth2-proxy edge

`docker-compose.auth.yml` puts the whole app behind Google login with
[oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/). Devy never runs the OAuth
flow — the edge does, and forwards a verified OIDC id_token that Devy checks against
Google's JWKS (`auth.mode: jwt`).

1. **Google Cloud Console → Google Auth Platform:**
   - **Clients →** create an *OAuth client ID* (**Web application**). Authorized redirect
     URI: `http://localhost:8080/oauth2/callback` (add your prod `https://…/oauth2/callback`
     when you have a domain).
   - **Data Access →** scopes `openid`, `email`, `profile` (all non-sensitive — no
     verification needed).
   - **Audience →** keep **Testing**, add yourself as a **test user** (only test users can
     sign in until you publish).
2. **`.env`** (repo root, gitignored):
   ```bash
   OAUTH2_PROXY_CLIENT_ID=<client id>.apps.googleusercontent.com
   OAUTH2_PROXY_CLIENT_SECRET=<client secret>          # sensitive
   OAUTH2_PROXY_COOKIE_SECRET=<openssl rand -base64 32>
   ```
3. **`config.yaml`** — `auth.mode: jwt` + Google JWKS/issuer + `audience:
   ${OAUTH2_PROXY_CLIENT_ID}` + `rbac.email_roles` (see `config.example.yaml`).
4. **Bring it up with the overlay:**
   ```bash
   docker compose -f docker-compose-local.yml -f docker-compose.auth.yml up -d --build
   ```
   The edge takes `:8080`; the chat-ui and proxy host ports are closed so the edge is the
   only way in. Open `http://localhost:8080` → Google login. The web shows your signed-in
   email (with a **sign out** link); history and audit are scoped to the verified email.

**Gotchas (verified live on this setup):**

- **Use `http://localhost:8080`, NOT `http://127.0.0.1:8080`.** They're different cookie
  hosts. The registered `redirect_uri` is `localhost`, so if you start the flow on
  `127.0.0.1` the CSRF cookie is set on the wrong host and the callback fails with
  *403 "Unable to find a valid CSRF token."* (Add a `127.0.0.1` redirect URI in Google if
  you want both to work.)
- **Google id_tokens use EITHER `https://accounts.google.com` or `accounts.google.com`**
  for `iss`. Configure `auth.issuer` as a **list of both** (see above) or verification
  fails with *"Invalid issuer"* — which shows up as "login works but history isn't scoped
  to my email" (identity silently falls back to anonymous). Devy logs JWT verify failures,
  so `docker logs <proxy>` will show the reason.
- **Image attachments need a larger body limit** — nginx defaults to 1 MB, so a
  screenshot 413s. The bundled `web/nginx.conf` sets `client_max_body_size 25m`.

For **production**, pin the oauth2-proxy image version, serve over **HTTPS** (Google
requires https redirects off-localhost; set `OAUTH2_PROXY_COOKIE_SECURE=true`), and add
the prod redirect URI to the Google client. Needs Docker Compose ≥ 2.24 (`!override`).

## Production hardening checklist

- **Secrets:** keep `.env` out of git (it is gitignored). Prefer `${VAR}`
  expansion in `config.yaml` over inlining. Change the default `agentic`/`agentic`
  Postgres credentials.
- **Network:** keep the proxy on loopback (or a private network) and put SSO /
  a reverse proxy in front. Use TLS for any remote host-MCP.
- **Identity:** the honor-system `user_id` is **not authentication** — see
  [Security → Identity](security.md#identity) before exposing Devy to multiple
  users. Wire real auth into the seam first.
- **Host MCP profile:** run target hosts at the lowest profile that works
  (`read-only` < `diagnostic` < `elevated`); enable the audit log.
- **Backups:** back up the database (managed snapshots, or the `agentic-pgdata`
  volume) — it holds conversation history and the knowledge base.

## Scaling notes

The proxy is effectively stateless per request — all durable state lives in
Postgres — so you can run multiple proxy replicas behind a load balancer against
one shared (managed) database. Each replica keeps a small connection pool
(`psycopg-pool`). For large knowledge corpora, pin the embedding dimension and add
an HNSW index on the vector columns (see [Knowledge](knowledge.md)).
