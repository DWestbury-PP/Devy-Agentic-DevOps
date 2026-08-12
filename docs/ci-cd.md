# CI/CD — building & deploying Devy

This is the **operator's guide** to shipping Devy: how to build images, record a
release, and deploy (or roll back) the stack on AWS — plus the exact contracts the
build and deploy steps hand off through, and how the whole thing is built to grow
new front doors.

Three companion docs sit around it:
- **[deploy-design.md](deploy-design.md)** — the *why* (principles, decisions/ADRs). This page is the *how*.
- **[remote-readiness.md](remote-readiness.md)** — the "local → remotely-managed" productionization checklist.
- **[host-mcp.md](host-mcp.md)** — the host-MCP sidecar, deployed by its own workflow (covered briefly below).

> **One-line mental model:** **CI builds immutable images and writes a release
> ledger to SSM; CD reads that ledger and runs an Ansible play over SSM.** Nothing
> auto-deploys — every ship is a deliberate, plan-then-apply act.

---

## 0. The lifecycle at a glance

```
        you dispatch                    you dispatch
        build.yml                       deploy.yml
            │                               │
   ┌────────▼─────────┐            ┌────────▼──────────┐
   │ build ×N (matrix)│            │ resolve release   │  ← reads the SSM ledger
   │  → push to ECR   │            │  from the ledger  │
   │  (immutable tags)│            └────────┬──────────┘
   └────────┬─────────┘                     │
            │ record job                    │ Ansible play over SSM
   ┌────────▼─────────┐            ┌────────▼──────────┐
   │  SSM ledger       │──────────▶│ quiesce → pull@tag │
   │  /devy/builds/*   │  the seam  │ → migrate → up     │
   │  (the handoff)    │            │ → smoke → hold/done│
   └───────────────────┘            └────────────────────┘
```

Three workflows, each **dispatch-only** (no push-triggered deploys):

| Workflow | What it does | Artifact |
|---|---|---|
| **`build.yml`** | Build components → push immutable ECR tags → record a release manifest to SSM | container images + SSM ledger |
| **`deploy.yml`** | Resolve a release from SSM → run the `deploy/` Ansible play (compose stack) | the running platform stack |
| **`host-mcp-deploy.yml`** | Ship the native host-MCP systemd unit to a host set | a systemd service (no image) |

---

## 1. How to build (CI)

`build.yml` builds one, several, or **all** components in parallel and pushes
**immutable** tags to their per-component ECRs, then records the release to SSM.

**The ref you build is chosen with GitHub's native "Use workflow from" ref picker** —
the build pins every leg to that commit (`github.sha`). That commit SHA becomes the
release's identity in the ledger.

```bash
# Build everything on the current default branch and record the release
gh workflow run build.yml -f components=all -f push=true

# Build a single component
gh workflow run build.yml -f components=proxy

# Build an explicit combination (the advanced override beats the dropdown)
gh workflow run build.yml -f components_advanced=proxy,chat-ui

# Dry run — build only, no push, no record (validate a Dockerfile/context)
gh workflow run build.yml -f components=all -f push=false

# Build from a feature branch (the ref picker pins the commit)
gh workflow run build.yml --ref feat/my-change -f components=proxy
```

### Inputs

| Input | Type | Default | Meaning |
|---|---|---|---|
| `components` | choice | `all` | `all` (registry-driven: every `devy-*` ECR) · `proxy` · `chat-ui` |
| `components_advanced` | string | `""` | Comma list (e.g. `proxy,chat-ui`). **Overrides the dropdown when non-empty.** |
| `push` | bool | `true` | Push to ECR **and** write the SSM manifest. Uncheck for a build-only dry run. |

**Registry-driven matrix.** `all` enumerates every `devy-*` ECR repository and reads
each repo's **provenance tags** (`Component`, `Dockerfile`) to learn what to build and
where its context is — so a *new* component ECR is picked up by `all` with no change to
the workflow. (Provenance tags are set on the ECR repo in Terraform; a repo missing them
fails the matrix loudly.)

> **Note — `host-mcp` is not built here.** On AWS the host MCP deploys as a native
> systemd unit (see §5), not an image. Its Dockerfile survives only for the local demo.

---

## 2. The handoff — the SSM release ledger (the seam)

This is the contract that binds CI to CD. **`build.yml`'s `record` job writes it;
`deploy.yml`'s `resolve` job reads it.** It lives in **SSM Parameter Store** under
`/devy/builds`, in the deploy account (so dev/prod param stores isolate environments
for free). Three shapes:

| Parameter | Value | Purpose |
|---|---|---|
| `/devy/builds/by-commit/<sha>` | the **full manifest** (JSON, below) | the release, keyed by immutable commit identity |
| `/devy/builds/by-branch/<branch>/latest` | `<sha>` (a plain string) | "newest build on this branch" pointer |
| `/devy/builds/components/<component>/latest` | one component entry (JSON) | "newest image of this component" — the `assembled-latest` fill |

### The `by-commit` manifest schema

```jsonc
{
  "schema": 1,
  "sha":       "9f8e7d6c...",              // full commit SHA (the identity)
  "short_sha": "9f8e7d6",
  "branch":    "main",                     // sanitized (slashes → dashes)
  "ref":       "main",                     // the dispatched ref name
  "built_at":  "2026-07-26T15:00:00Z",     // ISO-8601 UTC
  "label":     "main-9f8e7d6-2026-07-26-1500Z",  // human-facing
  "run_url":   "https://github.com/.../actions/runs/123",
  "actor":     "DWestbury-PP",
  "status":    "complete",                 // "complete" | "partial"  (see below)
  "components": {
    "proxy":   { "image": "<acct>.dkr.ecr...devy-proxy:main-9f8e7d6-20260726T1500Z",
                 "tag": "main-9f8e7d6-20260726T1500Z", "digest": "sha256:…",
                 "repository": "devy-proxy" },
    "chat-ui": { "image": "…devy-chat-ui:…", "tag": "…", "digest": "…",
                 "repository": "devy-chat-ui" }
  }
}
```

Two semantics that aren't obvious from the shape:

- **`status: complete | partial`.** `partial` means fewer components were pushed than
  the dispatch requested — a coherent full-platform release is `complete`. **Only
  `complete` whole-platform manifests are meant to be deployed** (components move
  together; a deploy needs the full, coherent set). Single-component or partial builds
  are CI-validation / dev-loop material.
- **Re-runs augment, they don't clobber.** Re-building one component *at the same
  commit* merges its fresh entry into the existing `by-commit` manifest (a
  deep-merge — new component keys win) rather than overwriting the others. So you can
  fill a release incrementally and it converges to `complete`.

### Browsing the ledger — the `releases` tooling

A single **read layer** (`proxy/releases.py` → `ReleaseLedger`) parses the ledger
schema in one place; three thin surfaces sit on top, so "what can I deploy?" has a
real answer everywhere:

**1. The `releases` CLI** (terminal — runs with your ambient/SSO AWS credentials,
`--profile`/`--region` like `secrets sync`; needs no new IAM):

```bash
agentic-devops releases ls               # recorded builds, newest first (STATUS/BUILT/BRANCH/…)
agentic-devops releases latest main      # newest build on a branch (the newest-on-branch pick)
agentic-devops releases show <sha>       # one manifest in full (the specific-commit / rollback target)
agentic-devops releases components       # newest image per component (the assembled-latest pick)
```

**2. The admin API** (what web/Slack/Devy surfaces call — RBAC-admin-gated):

```
GET /v1/admin/releases[?limit=20]     → { reachable, prefix, releases:[…] }
GET /v1/admin/releases/latest?branch= → the newest release on a branch
GET /v1/admin/releases/components     → newest image per component
GET /v1/admin/releases/{sha}          → one release manifest
```

**3. The `list_releases` agent tool** — Devy answers "what can I deploy?" in chat
(read-only; it reports, it never triggers a deploy).

> **One deployment prerequisite for the API + tool (not the CLI):** the proxy's
> instance role needs `ssm:GetParameter` + `ssm:GetParametersByPath` on
> `/devy/builds/*`. That's an IaC-managed grant — until it lands, the API returns
> `reachable: false` (empty) and the tool says so plainly, by design. The CLI is
> unaffected (it uses your own credentials).

The reader is **best-effort**: an unreadable ledger degrades to empty results, never
an exception that takes a surface down.

<details><summary>Raw AWS-CLI access (fallback, no tooling)</summary>

```bash
aws ssm get-parameter --name /devy/builds/by-branch/main/latest --query Parameter.Value --output text
aws ssm get-parameter --name /devy/builds/by-commit/<sha> --query Parameter.Value --output text | jq .
aws ssm get-parameters-by-path --path /devy/builds/components --recursive --query 'Parameters[].Value' --output text
```
</details>

---

## 3. How to deploy (CD)

`deploy.yml` is the **CD front door**. It resolves a release from the ledger, then runs
the `deploy/` Ansible play over SSM (quiesce → pull pinned images → migrate → `compose
up` → smoke → hold-or-done). **`plan` first, then `apply`** — `plan` runs the play
`--check --diff` (the "would ship/recreate" dry run) and posts a summary; `apply`
deploys for real.

```bash
# 1) Always plan first
gh workflow run deploy.yml -f source=newest-on-branch -f branch=main -f mode=plan

# 2) Then apply
gh workflow run deploy.yml -f source=newest-on-branch -f branch=main -f mode=apply

# Deploy a specific commit (also the deliberate ROLLBACK path — redeploy last-known-good)
gh workflow run deploy.yml -f source=specific-commit -f commit=<sha> -f mode=apply

# Deploy the newest image of EACH component (the ad-hoc dev-loop assembly; may span commits)
gh workflow run deploy.yml -f source=assembled-latest -f mode=apply
```

### Inputs

| Input | Type | Default | Meaning |
|---|---|---|---|
| `source` | choice | `newest-on-branch` | how to pick the release (see below) |
| `branch` | string | `main` | branch whose newest build to deploy (`source=newest-on-branch`) |
| `commit` | string | `""` | commit SHA to deploy (`source=specific-commit`; **also the rollback target**) |
| `targets` | choice | `role_platform` | which hosts. `role_edge`/`all` are Phase-2 — today only `role_platform` |
| `mode` | choice | `plan` | `plan` = `--check --diff` dry run (posts a summary) · `apply` = deploy for real |

### The three `source` intents

The dropdown is an **intent selector** (a GitHub dispatch form can't populate a live
list from SSM), and `resolve` turns the intent into concrete image pins:

| `source` | Resolves to | Use it for |
|---|---|---|
| `newest-on-branch` | `by-branch/<branch>/latest` → the `by-commit` manifest | the normal "ship what I just built on `main`" |
| `specific-commit` | the `by-commit/<sha>` manifest | pinning an exact release — **and rollback** (redeploy the previous good SHA) |
| `assembled-latest` | newest `components/<c>/latest` for each component | the dev loop: one fresh component + the rest at last-known-good (may span commits) |

**Rollback is deliberate and human-invoked** — there is no auto-failback. A failed
checkout **halts and holds** for inspection; you fix-forward (usually a config/ref-data
tweak, re-run) or you redeploy the last-known-good SHA with `specific-commit`.

### What the deploy needs (Actions variables)

The workflows carry **no account-specific identifiers in source** — they read them from
repo-level **Actions variables** (Settings → Secrets and variables → Actions →
Variables). Set once per account; a fork sets its own. None are secrets (ARNs, account
IDs, bucket names, private IPs — not credentials):

| Variable | Used by | What it is |
|---|---|---|
| `AWS_REGION` | all (optional) | defaults to `us-east-1` |
| `BUILD_ROLE_ARN` | `build.yml` | OIDC role assumed to build/push (ECR write) |
| `DEPLOY_ROLE_ARN` | `deploy.yml`, `host-mcp-deploy.yml` | OIDC role assumed to deploy (SSM + read secrets) |
| `SSM_TRANSFER_BUCKET` | deploy inventory | the `aws_ssm` file-staging bucket |
| `BLOBS_BUCKET` | deploy → `.env` → `${DEVY_BLOBS_BUCKET}` | S3 bucket for image attachments |
| `GRAFANA_URL` | deploy → `.env` → grafana-mcp + grounding | Grafana Cloud tenant URL |
| `EDGE_AL2023_MCP_URL`, `EDGE_UBUNTU_MCP_URL` | deploy → `.env` → `${EDGE_*_MCP_URL}` mounts | native edge host-MCP sidecar URLs (private IPs) |

Runtime provider/MCP **secrets** are never in the pipeline — they're read on-host from
AWS Secrets Manager by the instance's own identity. See [Security](security.md).

---

## 4. Migrations are part of the deploy

The play runs `agentic-devops db migrate` from the **new image against the live
Postgres** as a **gated pre-step** before the app tier cycles — the DB is never torn
down on an app deploy. Migrations are **expand/contract** (backward-compatible), which
is exactly what keeps an app-only rollback safe. Full treatment:
[db-migrations.md](db-migrations.md).

---

## 5. Deploying the host-MCP sidecar (separate cadence)

The host MCP is a **native systemd unit**, not an image, so it has its own workflow —
`host-mcp-deploy.yml` — with a different artifact and cadence. Change the allow-list →
re-run this; change the proxy → run `build` + `deploy`.

```bash
gh workflow run host-mcp-deploy.yml -f hosts=role_platform -f ref=main -f mode=plan
gh workflow run host-mcp-deploy.yml -f hosts=edge-al2023   -f ref=main -f mode=apply
# Enhanced (mutations) mode — opt-in, restart-bounded, self-reverts on next deploy
gh workflow run host-mcp-deploy.yml -f hosts=role_platform -f allow_mutations=true -f mode=apply
```

| Input | Type | Default | Meaning |
|---|---|---|---|
| `hosts` | choice | `role_platform` | `role_platform` · `role_edge` · `edge-al2023` · `edge-ubuntu` · `all` |
| `ref` | string | `main` | git ref to ship — **the checked-out source IS the SHA pin** (no image) |
| `profile` | choice | `diagnostic` | read profile the sidecar runs at: `read-only` < `diagnostic` < `elevated` |
| `allow_mutations` | bool | `false` | enhanced mode — gated reversible mutations; read only at startup |
| `mode` | choice | `plan` | `plan` = `--check --diff` · `apply` = install for real |

**Stopped hosts are skipped, not started** (the inventory is running-only, so CD never
starts a billable box). Start edge hosts by hand for a dev deploy, stop them after. Full
detail: [host-mcp.md](host-mcp.md).

---

## 6. Built to scale — one door in, many faces

The control plane was deliberately shaped as a **funnel**, so new user-facing surfaces
are cheap to add without ever forking the deploy logic (design: `deploy-design.md`
§7 / §18):

```
   L3  faces:   GitHub UI button   ·   web UI   ·   Slack bot   ·   Devy (deploying Devy)
                        │                │             │                    │
                        └────────────────┴──────┬──────┴────────────────────┘
                                                 ▼
   L2  one door:            repository_dispatch  (client_payload == the dispatch inputs)
                                                 ▼
   L1  primitive:           the workflow  (build.yml / deploy.yml / host-mcp-deploy.yml)
                                                 ▼
                            OIDC → Ansible-over-SSM   (the actual work)
```

- **L1 — the primitive (built).** Each workflow has strong, typed inputs and a
  plan/apply discipline. Runnable from the Actions tab or `gh workflow run`.
- **L2 — one door in (built for deploy + host-mcp).** Both CD workflows accept a
  **`repository_dispatch`** event whose `client_payload` **mirrors the dispatch inputs
  exactly** — so a webhook and a button are the *same call*, with no divergent logic:
  - `deploy.yml` ← event type **`devy-deploy`** (`{source, branch, commit, targets, mode}`)
  - `host-mcp-deploy.yml` ← event type **`devy-host-mcp-deploy`** (`{hosts, ref, profile, allow_mutations, mode}`)
- **L3 — many faces (open canvas).** Any surface that can send an authenticated webhook
  becomes a deploy button: a web console, a Slack `/deploy` command, or **Devy itself**
  (the dogfood beat — Devy proposing and, on human approval, triggering its own deploy).
  Each is *just a secure sender* of the L2 payload.

### Firing the webhook (L2)

```bash
curl -sf -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/DWestbury-PP/Devy-Agentic-DevOps/dispatches \
  -d '{
        "event_type": "devy-deploy",
        "client_payload": {
          "source": "newest-on-branch",
          "branch": "main",
          "targets": "role_platform",
          "mode": "plan"
        }
      }'
```

Any omitted `client_payload` field falls back to the same default as the dispatch form.

---

## 7. Honest gaps (scoped, not hidden)

The foundation is complete and proven; these are the clearly-bounded next rungs, called
out so nobody mistakes intent for reality:

| Gap | Today | The next rung |
|---|---|---|
| **Release browse layer** | ✅ **done** — `releases` CLI + `/v1/admin/releases` API + `list_releases` tool (§2) | consume it from a web/Slack surface |
| **SSM-read IAM grant** | ✅ **done** — the proxy instance role reads `/devy/builds/*`; the API + `list_releases` tool are live on `devy-platform` | — |
| **Build has no webhook** ⬅ *next* | `build.yml` is dispatch-only | add a `repository_dispatch` to `build.yml` symmetrical to the deploy ones |
| **Webhook auth model** | a token with `repo` scope | decide the durable model — a scoped fine-grained token, a GitHub App, or a small authenticated relay (`deploy-design.md` §16) |
| **Trigger from Devy** | Devy can *read* the ledger (`list_releases`) | a propose-only `request_deploy` under guarded actions (human-approved) — the Devy-deploys-Devy beat |
| **`deploy.yml` targets** | `role_platform` only | wire `role_edge` / `all` (Phase-2) |
| **Slack notifications** | dormant step in `build.yml` | set `SLACK_WEBHOOK_URL` to light up build notices |
| **Coverage audit** | fine at 3 hosts | reconcile-against-tags at fleet scale (`deploy-design.md` §12) |

---

## See also

- **[deploy-design.md](deploy-design.md)** — principles + decision log (ADRs D-001…D-016).
- **[remote-readiness.md](remote-readiness.md)** — the productionization checklist.
- **[host-mcp.md](host-mcp.md)** — the host-MCP sidecar and its deployment shapes.
- **[db-migrations.md](db-migrations.md)** — the app-owned, expand/contract migration model.
- **[security.md](security.md)** — OIDC, on-host secrets, the host-MCP boundary.
