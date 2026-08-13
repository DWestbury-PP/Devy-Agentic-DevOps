# Devy — remote-readiness (productionization checklist)

Devy has grown up **built locally** (`./devy.sh build && ./devy.sh up`). To be deployed by the CI/CD
pipeline — build → ECR (done ✅) and **deploy = Ansible-over-SSM, owned by this repo** (design:
[`deploy-design.md`](deploy-design.md); CD code in [`deploy/`](../deploy)) — Devy's
*deployment* has to be refactored from "works on my machine" to "remotely managed."

## The principle (and the coaching template)

> **"Works locally" and "remotely deployable" differ by exactly one thing: how much is *implicit*.**
> Local dev survives on assumptions the developer carries in their head. Productionization is the
> disciplined act of making every one of them **explicit, externalized, and idempotent.**

| Local (implicit) | Remote-managed (explicit) |
|---|---|
| my `.env`, my `~/.config` | externalized config + **Secrets Manager via instance identity** |
| "I'll run `build` then `up`" | **gated pipeline steps**: build → push → migrate → deploy → smoke |
| "I know to init the DB first" | an **explicit, gated sequence** — ordering is encoded, not remembered |
| my local volume | a **managed data tier** + **versioned migrations** |
| localhost, no TLS | **explicit ingress + identity** (the oauth2-proxy edge + TLS) |
| "it's up because I can see it" | **health/smoke gates** that decide for you |

Every "it works on my machine" is really "my machine holds state and knowledge production doesn't."
This checklist is Devy's live rehearsal of moving that knowledge out of the developer's head and into
the repo, the pipeline, and the platform — the exact thing to have engineers internalize *before* their
first 2am rollback.

## Checklist

Status: ✅ done · 🔨 in progress · ⬜ todo

### 1. ✅ Deploy compose variant (ECR images, not local build)
A self-contained **`docker-compose-aws.yml`**: every buildable service is `image: ${DEVY_*_IMAGE}`
(pulled, **no `build:`**), `DEVY_MODE=prod` with no `AWS_ENDPOINT_URL`/static keys (real ASM via the
instance role), and **no LocalStack / demo / repo bind-mounts**. Image URIs come from a `.env` the
pipeline renders from the release manifest. Local dev is a separate file, **`docker-compose-local.yml`**
(the `build:` stack), left untouched. *(Superseded the overlay approach — a base+deploy+prod layering
whose empty-`AWS_ENDPOINT_URL` substitution silently fell back to LocalStack; two self-contained files
removed that whole class of surprise.)*
- **Implicit → explicit:** "compose builds my images" → "compose references pinned, immutable ECR tags."

### 2. ✅ `db migrate` — app-owned, versioned, expand/contract
A first-class `agentic-devops db migrate` command backed by a `schema_migrations` tracking table.
Applies only **not-yet-applied** migrations, in order; a fresh DB applies all from `0` (so **bootstrap +
incremental unify**). Migrations are **expand/contract** (backward-compatible — the old app must still run
against the new schema, which is what keeps rollback safe). Wrapped in a transaction (Postgres
transactional DDL → atomic apply, auto-rollback on failure). Run by the pipeline as a **gated pre-step from
the NEW image against the LIVE Postgres**, before the app tier cycles.
- **Implicit → explicit:** "`schema.sql` auto-applies on an empty volume" → "explicit, versioned,
  deliberately-run migrations that are safe to roll an app back across."
- **Design + grounding:** [`docs/db-migrations.md`](db-migrations.md). Probing the live DB proved the
  point — it has already drifted from `schema.sql` (a legacy `token_encrypted` column the current bootstrap
  can't drop), so the first real migration (`002`) reconciles that drift and makes fresh + live converge.

### 3. 🔨 Config & secrets externalization
Real **AWS Secrets Manager via the instance role** (keyless, IMDS) — not LocalStack. Set `DEVY_MODE=prod`
semantics, `AWS_ENDPOINT_URL=""` (real AWS), and ensure the `devy/*` secrets exist in the account's
Secrets Manager. Runtime provider/MCP keys come from the vault, never the repo or the pipeline.

Two halves:
- **(a) get the values into ASM — DONE.** `agentic-devops secrets sync` (out-of-band, admin creds) reads the
  local dev catalog and **upserts the same refs** into real AWS SM. Idempotent + diff-aware (re-run to push
  only the delta — doubles as the rotation path); values never touch disk or logs; non-destructive (a ref in
  AWS but not in dev is left alone — e.g. `devy/alloy/*`, which is outside Devy's catalog). `--dry-run`
  previews the plan; `--only/--skip` scope it. Ref parity (`devy/provider/*`, `devy/github/*`, `devy/host/*`)
  is what makes the dev→prod resolve path identical.
- **(b) let the host READ them — DONE (Terraform), least-privilege by role.** Rather than one broad
  `devy/*` grant, your IaC layer's permission-set menu now splits secrets by role: `alloy-secrets`
  narrowed to `devy/alloy/*` (every host runs Alloy) and a new **`devy-platform-secrets`**
  (`devy/{provider,github,host,mcp}/*`) attached to the **`devy-platform` host only**. Edges (host-mcp)
  read only the Alloy key — they never see provider/GitHub secrets. No CMK in play (the managed
  `aws/secretsmanager` key needs no explicit `kms:Decrypt`). Runtime is read-only; only the out-of-band
  sync tool writes. Mirrors the `devy-ecr-pull` permission-set pattern.
- **Implicit → explicit:** "my `.env` + LocalStack" → "externalized config + secrets fetched on-host by
  the host's own identity."

### 4. ⬜ Data tier
Persistent **Postgres/pgvector volume on EBS** for the demo (survives redeploys — only the app tier
cycles, the DB stays up). **RDS/Aurora** is the prod breadcrumb (set `DATABASE_URL`, drop the bundled
`postgres` service, run `db init`/`migrate` once).
- **Implicit → explicit:** "my laptop's volume" → "a managed, persistent, backed-up data tier."

### 5. ⬜ Ingress / TLS
The published ports bind **loopback only** today. Remote access needs a real front door: the oauth2-proxy
edge (`:8080`) fronted by TLS termination (ALB, or caddy/nginx). *(Demo interim: SSM port-forward or a
scoped exposure; real: TLS + the public zone.)*
- **Implicit → explicit:** "localhost, no TLS" → "an explicit, authenticated, encrypted front door."

### 6. ⬜ Entrypoint modularity
Each component's `entrypoint` does one thing cleanly: **pull config → (migrations run as a *separate*
gated step, NOT baked into app start) → serve**, with graceful shutdown and a real health endpoint
(proxy `/healthz` exists; host-mcp is a TCP check). Decoupling migration from app-start is what lets the
pipeline gate on it.
- **Implicit → explicit:** "the container does whatever on boot" → "a modular, ordered, observable
  startup the pipeline can reason about."

### 7. ✅ host-mcp to the edges — DONE (native systemd, 2026-08-10; PRs #107–#113)
Shipped — and **better than the original plan**: the host MCP runs on the edges (and the platform) as a
**hardened, unprivileged native systemd unit**, NOT a `docker.sock`-bound container, which sidesteps the
wide-open-RCE concern this gate was about. The proxy↔edge diagnostic mesh is live — verified: Devy
diagnosed the platform **and** both edge hosts in a single turn. Full treatment: [`docs/host-mcp.md`](host-mcp.md).
All prerequisites met:
- **Linux variant of host-mcp** ✅ — a cross-distro native surface (AL2023/RHEL/Fedora + Ubuntu), authored
  to journald/systemd strengths (`journal_query`, `failed_units`, `time_sync`, `hardware_info`, …).
  Deployed by [`host-mcp-deploy.yml`](../.github/workflows/host-mcp-deploy.yml) — SHA-pinned source shipped
  over SSM, installed as the hardened unit.
- **Bearer-token auth from ASM** ✅ — the sidecar requires a bearer over HTTP; the proxy presents the
  vaulted `devy/mcp/host` token. And the native unit is **unprivileged** (`devy-hostmcp`, zero
  capabilities, `ProtectSystem=strict`) — defense in depth *beyond* the token, so a compromised proxy
  can't get a shell or escalate on an edge.
- **Scoped IAM + SG** ✅ — the deploy role's `secretsmanager:GetSecretValue` on `devy/mcp/host` is
  codified in Terraform (its own statement, not a widening); the **platform→edge `:8781` SG rule** is open
  (SG-to-SG, least privilege). Layered: allow-list (no shell) → unprivileged user → systemd sandbox →
  bearer → SG.
- **Implicit → explicit:** ✅ components land on their designated hosts by role, wired by explicit SG rules
  **and mutually authenticated**.

## Already done

- ✅ **Build → ECR** — `.github/workflows/build.yml`: registry-driven parallel matrix builds all three
  components and pushes immutable `<component>:<ref>-<sha>-<utc>` tags to `devy-*` ECRs.

## Suggested order

**1 + 2 first** (deploy compose variant + `db migrate`) — they unblock the actual deploy and the
migration gate. Then 3 (secrets), 6 (entrypoint modularity), 4 (data tier). 5 (ingress) and 7 (edges) can
follow once the platform stack deploys cleanly.
