# Devy — remote-readiness (productionization checklist)

Devy has grown up **built locally** (`./devy.sh build && ./devy.sh up`). To be deployed by the CI/CD
pipeline — build → ECR (done ✅) and **deploy = Ansible-over-SSM** (design:
[`aws-terraform`/`aws-ansible` → `deploy-design.md`](../../aws-ansible/docs/deploy-design.md)) — Devy's
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
A `docker-compose.deploy.yml` where every service is `image: ${DEVY_*_IMAGE}` (pulled), **no `build:`**.
The variables come from a `.env` the pipeline renders from the release manifest. Local dev keeps using
the `build:` base compose untouched. *(Evolve the existing "unvalidated scaffold" `docker-compose.prod.yml`.)*
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
  `devy/*` grant, the `aws-terraform` permission-set menu now splits secrets by role: `alloy-secrets`
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

### 7. ⬜ host-mcp to the edges  ⚠️ SECURITY GATE
Deploy `host-mcp` to the `role_edge` hosts (containerized with `docker.sock`, or native), and open the
**platform → edge `:8780` SG rule** (the breadcrumb we've carried since the Terraform design). This is
what makes the proxy↔edge diagnostic mesh (and tier-3 smoke) real.

**Two prerequisites, both currently unmet — do NOT expose host-mcp on the edges without them:**
- **A RHEL/AL2023/Ubuntu variant of host-mcp.** Today host-mcp is **Mac-only**; there is no Linux build.
  The edge deploy is blocked on building (and containerizing) that variant.
- **Bearer-token auth, retrieved from ASM.** An MCP server bound to `docker.sock` with no authentication
  is a **wide-open RCE surface** — network controls (private subnet + the platform→edge SG) are necessary
  but **not sufficient** (a compromised proxy or any lateral movement ⇒ full host control on every edge).
  The Linux variant MUST require a bearer token that the proxy presents and the edge validates, sourced
  from a new `devy/mcp/host` secret in ASM. That secret then earns the edges a **tightly-scoped** IAM read
  grant in `aws-terraform` (its own edge permission set — NOT a widening of `devy-platform-secrets`; the
  hook is already noted in `bootstrap/dev/instance-permission-sets.tf`). App-layer auth + network + secret
  scope land together as the Phase-2 edge-hardening bundle.
- **Implicit → explicit:** "I run everything on one box" → "components land on their designated hosts by
  role, wired by explicit network rules **and mutually authenticated**."

## Already done

- ✅ **Build → ECR** — `.github/workflows/build.yml`: registry-driven parallel matrix builds all three
  components and pushes immutable `<component>:<ref>-<sha>-<utc>` tags to `devy-*` ECRs.

## Suggested order

**1 + 2 first** (deploy compose variant + `db migrate`) — they unblock the actual deploy and the
migration gate. Then 3 (secrets), 6 (entrypoint modularity), 4 (data tier). 5 (ingress) and 7 (edges) can
follow once the platform stack deploys cleanly.
