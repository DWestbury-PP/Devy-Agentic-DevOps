# Deployment

Devy ships as a small set of containers plus an optional native binary. This
guide covers the bundled stack, using a managed database, running natively, and
production hardening.

## The compose stack

[`docker-compose.yml`](../docker-compose.yml) defines four services:

| Service | Role | Exposed |
|---|---|---|
| `postgres` | Postgres + pgvector (sessions, knowledge, memory) | compose network only |
| `proxy` | the LLM-PROXY | `127.0.0.1:8765` (host loopback) |
| `host-mcp` | safe-allowlist host + Docker diagnostics | compose network only (`:8780`) |
| `chat-ui` | nginx serving the web chat + reverse-proxying the API | `127.0.0.1:8080` |

Plus a `demo-faulty` service behind the `demo` profile (the crash-loop RCA demo).

```bash
docker compose up -d --build        # build + start (postgres self-bootstraps its schema)
docker compose logs -f              # follow logs
docker compose down                 # stop (keeps the DB volume)
docker compose down -v              # stop AND drop the DB volume (destroys data)
docker compose up -d --build chat-ui  # rebuild just the web surface after edits
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

Modes: **dev** (default — base + SSO overlay + LocalStack) or **`--prod`** (adds
`docker-compose.prod.yml`: real AWS via IAM role, no LocalStack, secure cookies —
a *scaffold*, validated by the Terraform deployment). `--no-auth` runs the base
stack only (password-mode bootstrap / break-glass). Pure bash, no dependencies.

Config and secrets are read from a mounted directory (default
`~/.config/agentic-devops`, override with `$AGENTIC_DEVOPS_CONFIG_DIR`) — the same
`config.yaml` + `.env` a native install uses. Compose reads a `.env` next to
`docker-compose.yml` for `HOST_MCP_TOKEN`, `POSTGRES_PASSWORD`, and `DATABASE_URL`.

> **Enabling the admin control plane** (host registry + document import) needs
> two bootstrap secrets in the *mounted* `~/.config/agentic-devops/.env`:
> `DEVY_ADMIN_PASSWORD_HASH` + `DEVY_ADMIN_SECRET` (both required, else
> `/v1/admin/*` → `503`). They gate the admin plane itself, so they're bootstrap
> (environment), not vault-managed. Generate them with
> `agentic-devops admin set-password`. Connector/provider tokens are then managed
> *in* the vault via the admin Secrets tab. See
> [Security → Secrets model](security.md#secrets-model).

```bash
# one-time: a shared token for the host MCP
echo "HOST_MCP_TOKEN=$(openssl rand -hex 24)" >> .env
```

Bound to host **loopback** by design — the proxy and web chat are not exposed on
the network. Put a reverse proxy / VPN / SSO in front for shared access.

## Database: bundled or managed

The DSN (`database.url` / `$DATABASE_URL`) is the single switch.

**Bundled (zero setup):** the compose `postgres` service uses the `pgvector`
image and runs `db/schema.sql` on first init (the `vector` extension + tables).
Data persists in the `agentic-pgdata` volume.

**Managed (RDS / Aurora / Cloud SQL / …):**

```bash
# 1. point the proxy (and CLI) at your instance
export DATABASE_URL=postgresql://USER:PASS@your-db.example.com:5432/agentic
# 2. provision it once (needs a role allowed to CREATE EXTENSION vector)
agentic-devops db init
# 3. start only the app services (skip the bundled DB)
docker compose up -d --build proxy host-mcp chat-ui
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

1. **Deploy** the base stack (`docker compose up -d`). `auth.mode: password`.
2. **Set the admin password:** `agentic-devops admin set-password` (writes
   `DEVY_ADMIN_PASSWORD_HASH`/`DEVY_ADMIN_SECRET` to `.env`). The admin console is now
   reachable in password mode.
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
   docker compose -f docker-compose.yml -f docker-compose.auth.yml up -d --build
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
