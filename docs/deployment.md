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

Config and secrets are read from a mounted directory (default
`~/.config/agentic-devops`, override with `$AGENTIC_DEVOPS_CONFIG_DIR`) — the same
`config.yaml` + `.env` a native install uses. Compose reads a `.env` next to
`docker-compose.yml` for `HOST_MCP_TOKEN`, `POSTGRES_PASSWORD`, and `DATABASE_URL`.

> **Enabling the admin control plane** (host registry + document import) needs
> three more secrets in the *mounted* `~/.config/agentic-devops/.env`:
> `DEVY_ADMIN_PASSWORD_HASH` + `DEVY_ADMIN_SECRET` (both required, else
> `/v1/admin/*` → `503`) and `DEVY_ENCRYPTION_KEY` (Fernet, for per-host tokens).
> Generate them with `agentic-devops admin set-password` / `admin gen-key`. See
> [Security → Admin control plane](security.md#admin-control-plane).

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
