# Bootstrapping from a cold clone

Getting a **fresh `git clone`** to a working local Devy — chat, admin console, host
diagnostics — on a machine that has nothing set up. Every command here was run
end-to-end on a clean macOS box with no `~/.config/agentic-devops`, no `.env`, no
`~/.aws`, no `.venv`, and no containers.

The [README quickstart](../README.md#quickstart) is the three-line version. This is the
one that covers the parts that bite: the expected-but-alarming first-boot error, the
admin credentials, and the LocalStack persistence trap.

For the auth *model* (why password mode exists, how SSO layers on) see
[Deployment → Authentication](deployment.md#authentication-bootstrap-first-then-google-sso);
for the credential reference see [Security](security.md#identity).

> **Scope: this is the LOCAL path.** AWS is a different topology, not this guide with
> different values — `docker-compose-aws.yml` is self-contained and shipped by the CD
> pipeline, the host MCP runs as a **native systemd unit** rather than a container,
> there is **no LocalStack** (`DEVY_MODE=prod` → real Secrets Manager via the instance
> IAM role), and `./devy.sh` is not involved at all. What *is* shared: the config
> schema, the vault refs (`devy/admin/*`, `devy/provider/*`), the tier model, and the
> migration ledger. See [Deployment → the AWS variant](deployment.md#the-compose-stack)
> and [Deploy design](deploy-design.md).

---

## What you need first

| | Why |
|---|---|
| **Docker** (Desktop or Colima), running | The whole stack is containers; the proxy image builds from `python:3.12-slim` |
| **A model provider key** | Devy can start without one, but can't answer |
| Python 3.12 + `uv` | **Only** for running tests / native mode — *not* needed to bootstrap. See [Local development](#local-development-optional) |

You do **not** need a local Python to complete this guide. The proxy container ships
the `agentic-devops` CLI, and every command below runs through it.

---

## 1. Config and env

Two files, two different homes — this trips people up:

```bash
mkdir -p ~/.config/agentic-devops
cp config.example.yaml ~/.config/agentic-devops/config.yaml
cp .env.example        ~/.config/agentic-devops/.env

# The host-MCP token goes in the PROJECT-dir .env (compose's default env file),
# which is a different file from the one above.
echo "HOST_MCP_TOKEN=$(openssl rand -hex 24)" >> .env
```

| File | Read by | Contains |
|---|---|---|
| `~/.config/agentic-devops/config.yaml` | the proxy (mounted at `/config`) | tiers, MCP mounts, knowledge settings |
| `~/.config/agentic-devops/.env` | the proxy | bootstrap scalars (usually near-empty) |
| `./.env` (repo root) | **docker compose** | `HOST_MCP_TOKEN`, and SSO vars later |

Now edit `config.yaml`. At minimum set your [model tiers](configuration.md#model-tiers):

```yaml
tiers:
  fast:
    model: anthropic/claude-sonnet-4-5
    label: Fast
    max_tokens: 8192
  balanced:
    model: anthropic/claude-opus-4-8
    label: Balanced
    max_tokens: 8192
  deep:
    model: anthropic/claude-fable-5
    label: Deep
    max_tokens: 16384
```

> ⚠️ **Do not set `temperature` (or `top_p` / `top_k`) on recent Anthropic models.**
> Opus 4.7 and later — including Opus 4.8 and Fable 5 — **removed the sampling
> parameters and return HTTP 400** if any are sent. `config.example.yaml` ships a
> commented `# temperature: 0.2` on the deep tier; uncommenting it against those models
> breaks every request on that tier. `providers.py` only forwards `temperature` when a
> tier sets it, so leaving it unset is what keeps these tiers working. Steer with
> prompting instead.

> Fable 5 additionally requires **30-day data retention** on the Anthropic org. Under
> zero-data-retention *every* Fable 5 request returns 400 regardless of payload.

Uncomment the host-MCP mount in the same file so Devy gets host and Docker
diagnostics:

```yaml
mcp_servers:
  - name: host
    transport: http
    url: http://host-mcp:8780/mcp
    secret_ref: devy/mcp/host      # vault-mastered; seeded in step 3
```

Leave `auth:` commented out. The default is **`auth.mode: password`**, which is what a
fresh deployment wants — you can't use SSO to configure SSO.

---

## 2. Start the stack

```bash
./devy.sh --no-auth up
```

`--no-auth` selects password mode (base compose only, no oauth2-proxy edge). The
wrapper assembles the compose files, then brings the DB to head. First run builds
images and pulls Postgres/LocalStack, so expect several minutes.

Success looks like six containers and a clean migration:

```
$ docker compose -f docker-compose-local.yml ps
agentic-devops-proxy       Up (healthy)
agentic-devops-postgres    Up (healthy)
agentic-devops-localstack  Up (healthy)
agentic-devops-host-mcp    Up (healthy)
agentic-devops-grafana-mcp Up
agentic-devops-chat-ui     Up
```

> **Expected on first boot — not a failure:**
> ```
> mcp_servers[host] references secret_ref devy/mcp/host but the vault has no value
> MCP server 'host' failed to mount: unhandled errors in a TaskGroup
> ```
> The vault starts empty; you seed it in the next step. The proxy is healthy and chat
> works — only the host tools are missing until then.

---

## 3. Seed the host-MCP bearer

The proxy resolves its copy of the token from the vault; the host-MCP *server* reads
its own from the repo-root `.env`. They must match — and since compose already passed
`HOST_MCP_TOKEN` into the proxy container, you can copy it across without the value
ever touching your shell history:

> The containerized `host-mcp` is a **local demo convenience**. Its `host_*` checks
> report on *that container*, not your Mac. On AWS the sidecar is deployed natively as
> a systemd unit per host so the checks see the real machine — same MCP surface, same
> `devy/mcp/host` vault ref, different packaging. See [The host MCP](host-mcp.md).

```bash
docker exec agentic-devops-proxy sh -c \
    'agentic-devops secrets set devy/mcp/host "$HOST_MCP_TOKEN"'
```

---

## 4. Admin credentials

Two values, two jobs. Mixing them up fails **silently** — the console loads and every
login is rejected with no explanation.

| Vault ref | What it is | Recognise it by |
|---|---|---|
| `devy/admin/password-hash` | bcrypt hash of your password | starts `$2b$`, 60 chars |
| `devy/admin/secret` | HS256 key signing session tokens | 64 hex chars |

**Generate the pair.** This is pure local computation — bcrypt + a random token, no
vault write, no AWS:

```bash
docker exec -it agentic-devops-proxy agentic-devops admin set-password
```

**Store both.** Single-quote the hash; its `$` characters are shell variables otherwise:

```bash
docker exec agentic-devops-proxy agentic-devops secrets set \
    devy/admin/password-hash '$2b$12$……'
docker exec agentic-devops-proxy agentic-devops secrets set \
    devy/admin/secret        '……64 hex……'
```

> ⚠️ **On local, always use `secrets set` — never `aws --profile ls-devy
> secretsmanager create-secret`.** LocalStack Community doesn't persist. `secrets set`
> mirrors every write to `~/.config/agentic-devops/secrets-store.json` and re-seeds
> LocalStack on boot; raw `aws` writes skip that mirror and **vanish on the next
> `./devy.sh` recreate**. The failure is delayed and silent: login works today, then
> stops after an unrelated `down`/`up`.
>
> Real AWS has no such issue — ASM persists, and `secrets set` refuses in prod mode.

---

## 5. Restart and verify

```bash
./devy.sh --no-auth restart proxy
```

Three checks. First, the host tools mounted (no MCP error, and `host_*` tools appear):

```bash
curl -s http://127.0.0.1:8765/v1/tools | grep -o '"name":"host_[a-z]*"' | head
```

Second, the tiers are the ones you configured:

```bash
curl -s http://127.0.0.1:8765/v1/tiers
```

Third, the admin plane is **gated rather than disabled** — `401` is success here, `503`
means the credentials didn't hydrate:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/v1/admin/hosts
```

| Code | Meaning |
|---|---|
| `401` | ✅ admin plane live, authentication required |
| `503` | ❌ one or both admin refs missing or malformed — re-check step 4 |

Confirm the mirror caught all three refs, which is what makes them survive a recreate:

```bash
python3 -c "import json;print(sorted(json.load(open('$HOME/.config/agentic-devops/secrets-store.json')).keys()))"
# ['devy/admin/password-hash', 'devy/admin/secret', 'devy/mcp/host']
```

---

## 6. Provider keys — from the admin console

Open **http://127.0.0.1:8080**, then the admin console, and log in with the password
from step 4.

Everything else is vault-mastered and set through the UI — nothing more goes in `.env`:

| Tab | What belongs there |
|---|---|
| **Secrets** | Anthropic, OpenAI, Gemini, Tavily, LangSmith provider keys |
| **MCP** | Per-server bearer tokens (host MCP, Grafana) |
| **Repos** | GitHub PATs, per account |

Add your chat provider key on the **Secrets** tab and Devy answers immediately —
provider keys re-hydrate without a restart. `Test` validates a key without revealing
it.

Optional but worth it: **OpenAI** also powers embeddings (Anthropic has no embeddings
endpoint), which the knowledge base and `recall_history` need; **Tavily** enables the
native `web_search` tool.

---

## The `ls-devy` AWS profile (optional)

For *inspecting* the dev vault and blob store. Not required, and **not** for writing
secrets — see the warning in step 4.

```bash
aws configure set aws_access_key_id     test                  --profile ls-devy
aws configure set aws_secret_access_key test                  --profile ls-devy
aws configure set region                us-east-1             --profile ls-devy
aws configure set output                json                  --profile ls-devy
aws configure set endpoint_url          http://localhost:4566 --profile ls-devy
```

`test`/`test` isn't a placeholder to replace — LocalStack doesn't validate credentials,
and these match what compose passes into the proxy container. `endpoint_url` as a
config key needs **AWS CLI ≥ 2.13**.

```bash
export AWS_PROFILE=ls-devy    # or pass --profile on every command

aws secretsmanager list-secrets --query 'SecretList[].Name'
aws s3 ls s3://devy-blobs/ --recursive
```

> If you get `NoRegion` / `NoCredentials`, you omitted the profile. `ls-devy` is
> typically the only profile on a fresh box — there's no `[default]` to fall back to.
> Prefer the explicit `AWS_PROFILE` export over creating a `[default]` section, so that
> once you add a real AWS profile, a forgotten `--profile` fails loudly instead of
> quietly picking a side.

Verify the admin credentials by **format**, never by value:

```bash
for n in devy/admin/password-hash devy/admin/secret; do
  v=$(aws secretsmanager get-secret-value --secret-id "$n" --query SecretString --output text 2>/dev/null)
  if [ -z "$v" ]; then echo "$n : NOT SET"; continue; fi
  echo "$n : $(echo "$v" | grep -qE '^\$2[aby]\$[0-9]{2}\$' && echo bcrypt-hash \
        || (echo "$v" | grep -qE '^[0-9a-f]{64}$' && echo 64-hex || echo other))"
done
```

`password-hash` **must** report `bcrypt-hash`. If it says `64-hex`, you pasted the two
values into the wrong slots.

---

## Local development (optional)

Only needed to run the test suites, lint, or the native proxy. Note that a
*containerized* proxy's builtin `host_diagnostics` sees the container, not your
machine — native mode is how you inspect your own box.

The project targets **Python 3.12** (matching `Dockerfile`). macOS system Python is
3.9, below the `requires-python = ">=3.10"` floor, so you need your own interpreter.
[`uv`](https://docs.astral.sh/uv/) fetches it for you:

```bash
brew install uv
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Don't reorder `PATH` to displace the system Python. Homebrew's `python3` is a symlink
that follows the newest formula, so it silently changes major version on an unrelated
`brew upgrade` — a per-project venv is both safer and version-stable.

### Tests

```bash
# Postgres + pgvector is REQUIRED for the DB-backed tests — and it's a SEPARATE
# instance from the compose one, which publishes no host port.
docker run -d --name agentic-test-pg -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=agentic_test -p 5433:5432 pgvector/pgvector:pg16

export AGENTIC_TEST_DATABASE_URL="postgresql://postgres:postgres@localhost:5433/agentic_test"

.venv/bin/python -m pytest -q                 # proxy suite  → 475 passed
.venv/bin/python -m pytest -q host-mcp/tests  # host-MCP suite → 51 passed
```

Two traps:

- **Use `python -m pytest`, not bare `pytest`.** `[tool.pytest.ini_options] pythonpath`
  lists `src` and `host-mcp/src` but not `.`, so three modules that import
  `tests.conftest` fail to collect under the bare entry point. The module form puts the
  CWD on `sys.path`.
- **Without `AGENTIC_TEST_DATABASE_URL`, 187 of 475 tests skip** — 39% of the suite
  passing vacuously. A bare run reports `288 passed` and looks green.

### Lint

```bash
uvx ruff@0.6.9 check src/ tests/       # → 8 findings
```

`[tool.ruff]` sets only `line-length` and `target-version` — no explicit rule
selection — and the dev extra says `ruff>=0.4` with no upper bound. Ruff's default rule
set has grown substantially since, so an unpinned modern ruff reports **642** findings
against the same unchanged code (477 of them `UP045`, `Optional[X]` → `X | None`).
Pin the version until `pyproject.toml` pins the rule set.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `MCP server 'host' failed to mount` on first boot | vault has no `devy/mcp/host` | Step 3, then restart the proxy |
| Admin endpoints return `503` | one/both admin refs missing or malformed | Step 4; verify formats in step 5 |
| Admin console loads, every login rejected | hash and secret pasted into swapped slots | Re-store; `password-hash` must be `bcrypt-hash` |
| Admin login worked, stopped after `down`/`up` | creds written with raw `aws`, skipping the mirror | Re-store with `secrets set` |
| HTTP 400 on the balanced/deep tier | `temperature`/`top_p`/`top_k` set on Opus 4.7+ | Remove it from the tier |
| Every Fable 5 request 400s | org is on zero data retention | Fable 5 needs 30-day retention |
| `aws: NoRegion` / `NoCredentials` | `--profile` omitted; no `[default]` exists | `export AWS_PROFILE=ls-devy` |
| `pytest` → `No module named 'tests'` | bare entry point; CWD not on `sys.path` | `python -m pytest` |
| Login silently breaks after enabling SSO | ran `docker compose up` without the auth overlay | Use `./devy.sh up` — it adds the overlay |

---

## Next steps

- **Turn on Google SSO** — additive; keep password mode as break-glass.
  [Deployment → Google SSO](deployment.md#google-sso-via-the-oauth2-proxy-edge)
- **Ingest your runbooks** so `search_knowledge` has something to cite.
  [Knowledge](knowledge.md)
- **Deploy the host MCP on a real host** for true host-level inspection.
  [The host MCP](host-mcp.md)
- **Try the RCA demo** — a live crash-loop to investigate.
  [README](../README.md#try-the-rca-demo)
