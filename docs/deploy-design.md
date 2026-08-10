# Devy CD — deployment design (breadcrumb)

The app-delivery layer that sits on top of the foundation: **`aws-terraform`** provisions hosts,
**this repo (`aws-ansible`)** configures their base state, and **this design** adds *versioned
software delivery* — reusing the same GitHub-Actions-over-SSM control plane rather than inventing a
new one. Demo deployable: the **Devy Agentic DevOps** platform onto the 3-host fleet.

> **Status: Phase-1 built & live-verified (2026-08-09).** The `deploy/` role, the `deploy.yml` CD
> workflow, the `build.yml` CI, and the AWS prod config (`deploy/config/config.aws.yaml`) all exist
> and deploy the platform stack to the dev fleet over SSM — chat, host diagnostics, attachments, and
> Grafana all verified live on `devy-platform`. Dev account only; **stage/prod are breadcrumbs** (same
> as [`multi-environment.md`](multi-environment.md)). A **versioned config-retrieval service** remains
> parked (§13) — but the per-env AWS config now ships with the role; the example placeholder is no
> longer used.

---

## 1. Principles (the spine)

These come straight from the release discipline of coordinated, planned deployments — **not** web-app
GitOps:

1. **No auto-deploy.** Every deployment is a deliberate, coordinated, reversible decision. Merging a
   PR does not ship anything. Even Dev is dispatched, just with lighter ceremony.
2. **A release is a coherent *set*, pinned by a manifest.** Components build independently but
   **cannot deploy in isolation** — they move together (the trading analogy: an SBE wire-format
   change forces every Aeron participant to release together, or you get silent incompatibility).
3. **Uniform DX, governance layered on.** Dev is easy/fast (iterate, fail fast); Prod is
   structured/coordinated/trusted — but they are the *same* workflow and grammar. The difference is
   **declarative gates** (GitHub Environment protection rules), not a different tool or UX. A small
   team drives either without night-and-day divergence.
4. **Fix-forward first; deliberate rollback.** A failed checkout **halts and holds** for inspection —
   it does **not** auto-revert. The operator iterates (often a config/ref-data tweak, not a new
   image) and re-runs; rollback is an explicit human call that, when made, must be **fast and boringly
   reliable**. No automated failback — "what can be known MUST be known and reliable; what can't be
   known yet gets a few sincere attempts before we revert to known-good."
5. **Two axes, kept separate.** A deployment = **manifest (images, immutable)** + **config overlay
   (env/secrets/ref-data, revvable)**. This separation is what makes fix-forward possible without a
   rebuild, and is the seam a versioned-config service plugs into later (§13).

## 2. Topology — the fleet already maps 1:1 onto Devy

| Fleet host | Role tag / group | Devy services |
|---|---|---|
| `devy-platform` (t3.large, AL2023) | `role_platform` | `proxy` (FastAPI brain), `chat-ui` (nginx), `oauth2-proxy` (edge), `grafana-mcp`, Postgres/pgvector — **plus the host MCP as a native systemd unit** (`:8781`), not a container |
| `edge-al2023`, `edge-ubuntu` | `role_edge` | the host MCP as a **native systemd unit** (`:8781`) — one diagnostic agent per host |

The host MCP runs **natively** (a hardened systemd unit) on **every** host — deployed by
[`host-mcp-deploy.yml`](../.github/workflows/host-mcp-deploy.yml), not the app-tier `deploy.yml`.
The platform proxy reaches its co-located sidecar over the Docker host gateway
(`host.docker.internal:8781`) and the edge sidecars across the subnet at `:8781` — which cashes in
the **platform→edge SG rule** (inbound `:8781` from the platform SG only). The Ansible inventory
groups `role_platform` / `role_edge`, so `--limit` targeting is free. Full treatment:
[The host MCP](host-mcp.md).

## 3. Two planes

```
BUILD  (Devy repo)   push/tag → GH Actions → docker build ×3 → per-component ECRs (immutable tags)
                                            → generate + publish a release manifest
DEPLOY (this repo)   dispatch(manifest, env, targets, action, mode)
                       → OIDC assume <project>-gha-ansible-<env>  (per-env deploy role)
                       → aws_ec2 inventory --limit <targets>       (groups already exist)
                       → devy role over SSM: quiesce → pull@tag → up → smoke → halt-or-done
```

We are **not building a deployment mechanism** — Ansible-over-SSM already is one. A deploy is an
Ansible play parameterized by an image manifest + a host limit + an environment.

### Required GitHub Actions variables

The CI/CD workflows carry **no account-specific identifiers in source** — they read them from
repo-level Actions variables (Settings → Secrets and variables → Actions → Variables). Set these
once for your account; a fork sets its own:

| Variable | Used by | Example |
|---|---|---|
| `BUILD_ROLE_ARN` | `build.yml` | `arn:aws:iam::<account>:role/<project>-gha-build-<env>` |
| `DEPLOY_ROLE_ARN` | `deploy.yml` | `arn:aws:iam::<account>:role/<project>-gha-deploy-<env>` |
| `SSM_TRANSFER_BUCKET` | `deploy.yml` → `inventory/aws_ec2.yml` | the `aws_ssm` file-staging bucket |
| `BLOBS_BUCKET` | `deploy.yml` → `.env` → config's `${DEVY_BLOBS_BUCKET}` | S3 bucket for image attachments |
| `GRAFANA_URL` | `deploy.yml` → `.env` → grafana-mcp sidecar + `deployment_context` | Grafana Cloud tenant URL |
| `AWS_REGION` | both (optional) | defaults to `us-east-1` if unset |

None are secrets (ARNs/account IDs/bucket names aren't credentials) — repo **variables**, not secrets.
For a by-hand deploy, `export DEVY_SSM_TRANSFER_BUCKET=<bucket>` before running the play.

## 4. Registry & image tagging

- **Per-component ECRs:** `devy-proxy`, `devy-chat-ui`. **Immutable** tags. (The host MCP is
  **not** an ECR image anymore — it deploys as a native systemd unit via `host-mcp-deploy.yml`;
  the stale `devy-host-mcp` repo should be retired in Terraform so registry-driven `all` builds
  stop matching it.)
- **Tag scheme (all four coordinates):** `<component>:<release-or-branch>-<shortsha>-<utc-timestamp>`
  — e.g. `devy-proxy:v0.9.1-9f8e7d6-20260726T1500Z`. Human-legible in the ECR console, globally unique.
- **Keyless pull** via the instance-role permission-set menu (extends the `alloy-secrets` pattern
  with an ECR-pull set). No registry credentials on hosts or in the pipeline.
- **New `ecr` Terraform module** — cashes in the planned "v1 self-service primitive."

## 5. The release manifest (keystone)

A **manifest** is a small pinned set — `{ proxy: <tag>, host-mcp: <tag>, chat-ui: <tag> }` — naming
one coherent, tested Devy stack. It is the coordination artifact *and* the record of "known-good."

**Rendering a deployment = topology + versions + config, with no duplication:**

| Layer | Source | Owns |
|---|---|---|
| **Topology** | Devy repo `docker-compose.yml` (+ `./devy.sh`) | services, wiring, ports, volumes, healthchecks |
| **Versions** | the manifest (external) | `component → image tag` |
| **Config** | per-env overlay + Secrets Manager | endpoints, `DEVY_MODE`, secret IDs |

The Devy repo adds a **`docker-compose.deploy.yml`** overlay where each service is
`image: ${DEVY_*_IMAGE}` (pulled, **no `build:`**); the manifest supplies those variables. **Local
laptop dev is untouched** — it keeps using the `build:` base compose. Servers use the deploy overlay
+ manifest + env overlay, rendered and run by Ansible.

Two rules keep releases trustworthy:
- **Official manifests are generated by the build, never hand-typed** (the build just pushed those
  exact tags — it knows them).
- **Promotion never rebuilds.** Stage and Prod deploy the *same* immutable manifest that Dev soaked,
  so "tested in Dev" is bit-identical to "what hits Prod."

## 6. Promotion ladder

| Rung | Build source | Manifest | Target |
|---|---|---|---|
| **Local** | `./devy.sh up` (build) | none | laptop |
| **Dev host** | one component, feature branch → ECR | assembled ad hoc: new tag + others at last-known-good | AWS dev host |
| **Release candidate** | all components at a release ref → ECR | **frozen + generated** by the release build | dev (final soak) |
| **Stage → Prod** | *no rebuild* — promote the frozen manifest | immutable, attached to a GitHub Release | stage, then prod |

## 7. Control plane — brass tacks first, then widen the funnel

- **L1 — the primitive:** a dedicated `deploy` workflow with the *same* UX as our Ansible workflow
  (plan/apply + strong dispatch inputs). Auto-triggerable *and* manual. Inputs:
  `manifest`/`version`, `environment` (dev|stage|prod), `targets` (→ `--limit role_platform|role_edge|host|all`),
  `action` (deploy|rollback), `mode` (plan|apply).
- **L2 — one door in:** expose the same workflow via `repository_dispatch`, accepting the *full*
  parameter set. Webhook and button become the same call — no divergent logic.
- **L3 — many faces:** web UI, the **Devy agent itself** (Devy deploying Devy — the dogfood/demo
  beat), a Slack bot — each just a secure sender of that one webhook.

**Repo home (revised — see D-016):** CD lives **in the Devy repo** (`deploy/`), next to CI — Devy
owns its whole delivery lifecycle. The organizing principle is **ownership × concern**: `aws-ansible`
is the *ops* fleet-baseline repo (OS/packages, DevOps/SecOps-owned) and must not hold app-release
logic; app teams already own the code + CI, so CD belongs with them. Devy carries its own small
Ansible-over-SSM plumbing (`deploy/{ansible.cfg,inventory,requirements.yml,roles/devy}`) and a scoped
`<project>-gha-devy-deploy-<env>` OIDC role (sibling to the build role). The genuinely shared primitives
are AWS *resources* (SSM transfer bucket, OIDC provider), provisioned in `aws-terraform` and merely
referenced. If the ~40 lines of plumbing ever get copied across many apps, extract a **template**
(cookiecutter), not a runtime-shared library — copy-and-own beats a lowest-common-denominator framework.

## 8. Deploy sequence (the `devy` Ansible play)

Reuses the proven docker-role discipline (quiesce cleanly, don't thrash):

1. **Quiesce** the running stack cleanly (`--restart=no`, `compose stop`) — don't tear down volumes.
2. **Pull** the manifest-pinned images from ECR (keyless, instance role).
3. **Render** deploy overlay + manifest + env overlay; **`compose up`**.
4. **Smoke** — the 3-tier suite (§9).
5. **Halt-and-hold on failure** — freeze in place, surface exactly what failed. **No auto-revert.**
   The play is **idempotent and re-runnable** so a fix-forward (change config/ref-data, re-run) is a
   tight loop, in Prod as in Dev.

## 9. Smoke suite — a domain-agnostic 3-tier shape

The *structure* of a trading checkout transfers even though the probes don't; the tier a deploy
fails at informs the fix-forward-vs-rollback call.

| Tier | Trading checkout (reference shape) | Devy checkout (this demo) | Signal on failure |
|---|---|---|---|
| **1 — Liveness** | processes up, buses connected | containers healthy: proxy `/healthz`, host-mcp TCP :8780, chat-ui via edge, postgres `pg_isready`, oauth2-proxy up | usually **rollback** |
| **2 — Reach / credentials** | market-data symbol access, venue sessions | Devy admin token-tests called **programmatically** — every provider key valid; DB, Secrets Manager, grafana-mcp reachable | usually **fix-forward** (config/credential) |
| **3 — Functional round-trip** | accept order/amend/cancel; out-of-band order to exchanges, immediately cancelled | "Hello, how are you?" prompt round-trips proxy→LLM; **plus** proxy→edge `host-mcp` diagnostic round-trip (proves the platform→edge mesh + docker.sock introspection) | **investigate** |

Tier-3's host-mcp round-trip is the Devy analog of a safe, reversible out-of-band transaction — it
proves the *whole mesh*, not just liveness.

## 10. Rollback & the deploy record

- **Rollback is deliberate and human-invoked** — redeploy the last-known-good manifest; fast +
  reliable; no cleverness.
- **Record of current + last-known-good, per environment:** a **git-tracked manifest + per-env
  pointer** (auditable, reviewable, diffable, and itself the rollback target) — "what MUST be known
  is known." A **GitHub Deployment** is created as the *reporting projection* (§11): git is the
  truth, the Deployment is the readout.

## 11. Release reporting — GitHub-native, so it's near-free

The `custom-monitoring-metrics` collector is a **pull-based observer of GitHub**, not a push target.
Emit GitHub-native signals and reporting largely falls out:
- A `deploy` workflow run is **already** captured as `github_workflow_runs_total{kind="deploy"}`
  (its `workflow-kinds.yaml` classifies `*deploy*`/`*release*`) → DORA deployment-frequency, **zero
  new code**.
- First-class **GitHub Releases/Deployments** would need only a small `ReleasesCollector` clone of
  the existing `WorkflowRunsCollector`. **No bespoke push endpoint** is built.

## 12. Coverage audit — reconcile the reconciled set (document-now, implement-later)

**The gap is not the no-op — it's the absence of an expectation.** A tag-based dynamic inventory
reports *reality* (what's running now), never *expectation* (what should have been reconciled). A
no-op across the correct set is honest success (idempotency *should* report "did nothing"); the
failure mode is that **"0 of an unknown expected set" is indistinguishable from "0 of 0."** This is
invisible when targeting an individual host, and surfaces at **fleet scale**, where unexpected host
states inevitably make updates skip or fail on some members of a large run.

Two classes of miss, only one of which the run self-reports:

- **Visible** — the host was enumerated and the PLAY RECAP records it: `failed>0` (failed on task/block
  X for reason Y) or `unreachable>0`.
- **Invisible** — the host was **never enumerated** (stopped / untagged / filtered out) or silently
  skipped (offline, too busy to respond). This is the dangerous class and the source of the false-green.

**The approach: an independent post-run reconciliation.**

- **Expected roster** = query **resource tags** for the targeted *category* (Environment / Region /
  Project / Team / Role) — crucially **in any instance state**, so a stopped-but-expected host still
  counts as owed.
- **Actual set** = the PLAY RECAP (hosts with `unreachable=0, failed=0`).
- **Audit** = `expected − actual` → the **uncovered set**; fail (or warn), **naming names**:
  *"category dev/Project=devy: expected {devy-platform, edge-al2023, edge-ubuntu}, reconciled {…},
  uncovered {…}."*

Properties that make it correct rather than a crude guard:

- **Named-set, not a count floor** — catches *partial* coverage (2 of 3), not just the all-empty case.
- **Category-parameterized** — Dev/Prod/Region/Project/Team *are* our tag dimensions + `keyed_groups`,
  so the audit derives its roster from them for free.
- **Authority** — **tags-as-truth** by default (self-maintaining; new hosts auto-join); a **declared
  roster** layered on for high-assurance categories (Prod) to catch a host that was never
  provisioned/tagged at all.
- **Must live *outside* the host loop** — an empty play runs no tasks, so the audit cannot be an
  in-play assertion (it wouldn't fire in the exact case it guards). It is a **separate job/step** that
  independently sees expected (tags) and actual (recap).

**One primitive, both planes.** Identical for an Ansible converge (*"all N hosts in category X
reconciled"*) and a CD deploy (*"manifest R reached all N hosts in category X"*); its output (coverage
%, uncovered hosts) feeds release reporting. It is the enactment of the ethos: **coverage is knowable,
so it must be asserted, not assumed** — a run isn't "done" until coverage is *proven*, not merely
un-erroring.

**Scope: documented, not built.** Unnecessary at today's 3-host footprint (individual/small-batch
targeting makes any gap self-evident); it becomes necessary at fleet scale, where silent skips hide
inside large runs. Implement then, against this shape.

## 13. Config axis — per-env config shipped; versioned retrieval still parked

**Update (2026-08-09):** the per-env config now ships — `deploy/config/config.aws.yaml` is a real,
committed AWS prod config (correct tiers, host+grafana mounts, `assistant_role=operator`, AWS
`deployment_context`) and the role's `devy_config_file` points at it (no longer `config.example.yaml`).
Account-specific values stay out of source via `${DEVY_BLOBS_BUCKET}`/`${GRAFANA_URL}` env expansion.

Devy's demo-grade config = that **per-env config file + AWS Secrets Manager** (fetched on-host via
IAM — the Alloy pattern). Kept **separate from the manifest**, so a fix-forward config change re-runs
the deploy with new values and **no rebuild** — the demo-grade equivalent of "publish a config
dot-release, restart to adopt." What remains parked is only the *versioned config-retrieval service*:

A full **versioned config-retrieval service** (entrypoint pulls a versioned file by name, injected
before the core service starts; restart-with-flag to adopt a new dot-release) is a **flagged future
workstream with its own dedicated brainstorm** — it slots in *behind* the overlay step without
changing the deploy flow. Not built for Devy. See the `reference-versioned-config-service` memory.

## 14. What's net-new to build (work inventory)

- **`aws-terraform`:** the `ecr` module (×3 repos), an instance-role **ECR-pull permission set**, and
  the **platform→edge :8780 SG rule**.
- **Devy repo:** the build workflow (matrix — whole-release *or* single-component), a
  `docker-compose.deploy.yml` overlay, and entrypoint/tagging conventions.
- **`aws-ansible` (this repo):** the `devy` deploy role, the `deploy.yml` workflow, a `manifests/`
  area + per-env pointer, and the smoke-test tasks.
- **`custom-monitoring-metrics` (optional):** a `ReleasesCollector`.

## 15. Phase plan

1. **Phase 1 — prove the primitive.** Build→ECR + deploy the platform services to the dev host,
   manual, with tier-1/2 smoke. Proves keyless ECR pull + manifest/overlay render.
2. **Phase 2 — parameterize.** Edges (`host-mcp`), full inputs, rollback, tier-3 smoke, the promotion
   ladder, generated manifests.
3. **Phase 3 — widen.** L2 webhooks + L3 UIs, release reporting, stage/prod breadcrumbs.

## 16. Open questions & assumptions to revisit at build time

- **Manifest format & storage:** a compose `.env` of `IMAGE` vars vs a YAML file; git-tracked +
  attached as a GitHub Release asset for official releases.
- **Webhook security model** (L2): scoped token vs GitHub App vs a small authenticated relay.
- **Config-versioning approach:** the per-env config file ships (§13); only the *versioned
  config-retrieval service* remains parked — its own session.
- **Coverage audit:** the reconcile-against-tags shape is documented (§12); implement at fleet scale,
  not at today's 3-host footprint.
- **Smoke depth:** the Devy indicative set is defined above; trading-grade depth is deferred (role
  not yet started).
- **Stage environment:** breadcrumb only — no account yet (mirrors `multi-environment.md`).
- **Assumption:** the Devy build/deploy is reshapeable (user owns the Devy project).
- **Assumption:** bundled Postgres/pgvector on `devy-platform` (EBS-backed) for the demo; RDS/Aurora
  is the prod breadcrumb.

## 17. Decisions to record as ADRs (seed for `docs/decisions.md`)

- **D-001** App delivery reuses the Ansible-over-SSM control plane; no new deploy mechanism.
- **D-002** A release is a pinned **manifest** (coherent component set); promotion never rebuilds.
- **D-003** Per-component **ECRs**, immutable tags `<component>:<ref>-<sha>-<utc>`; keyless
  instance-role pull.
- **D-004** Topology (Devy compose) / versions (manifest) / config (env overlay) are three separate
  layers; manifest and config are independent axes.
- **D-005** Uniform DX; environment differences are **declarative gates**, not divergent UX.
- **D-006** **Halt-and-hold + fix-forward**; rollback is deliberate, fast, reliable — **no auto-failback**.
- **D-007** Deploy record = git-tracked manifest + per-env pointer; GitHub Deployment as reporting
  projection.
- **D-008** ~~Deploy control plane starts in `aws-ansible`; extraction-ready for a dedicated CD repo.~~
  **Reversed by D-016.**
- **D-009** Success ≠ coverage: an independent **post-run coverage audit** reconciles the reconciled
  host set against the **tag-derived expected roster** for the targeted category (any instance state);
  lives outside the host loop; documented now, implemented at fleet scale.
- **D-010** Deploy = **Actions-orchestrated, Ansible-executed** over SSM (reusable `workflow_call` from
  the Devy repo; no raw SSM shell).
- **D-011** Manifest handoff = **SSM Parameter Store keyed by commit sha**; only **whole-platform builds**
  are deployable; value = complete component URI set + git ref + timestamp + build run-id.
- **D-012** **Ship rendered compose + `.env` over SSM; no git/source on the host.**
- **D-013** Migrations are **app-owned, expand/contract**, run as a **gated pre-step from the NEW image
  against the LIVE Postgres** (DB stays up); bootstrap + incremental unify via a `schema_migrations` table.
- **D-014** Rollback = redeploy the previous manifest (**app-only**; schema stays forward); **halt-and-hold
  default, auto-failback opt-in** (refines D-006).
- **D-015** Deployment strategy: **recreate now, blue/green target**; reject GitOps-pull &
  auto-progressive-delivery (philosophy); adopt **image signing** (cosign/SBOM) as future hardening.
- **D-016** **App CD lives in the app repo, not the ops repo** (reverses D-008). Ownership × concern:
  `aws-ansible` = fleet-baseline (OS/packages), ops-owned; Devy = code + CI + CD, app-owned. Devy carries
  its own `deploy/` Ansible-over-SSM plumbing + a scoped `gha-devy-deploy` OIDC role; shared primitives
  (SSM transfer bucket, OIDC provider) stay as `aws-terraform` resources, referenced not copied. Reuse
  across apps, if ever needed, is a **template** (cookiecutter), never a runtime-shared deploy framework —
  deploy logic is where apps differ most; a central LCD would be a leaky abstraction.

## 18. Deploy execution — the detailed CD mechanics (worked out 2026-07-26)

This section deepens §8 with the execution design settled for Step 3. Where §8 sketched, this specifies.

### 18.1 Orchestrate in Actions, execute in Ansible
The operator-facing control plane is a **GitHub Actions workflow** (same tab/UX as the build — DX
cohesion). The deploy **logic** is an **Ansible play over SSM**, reusing the inventory, OIDC role,
`aws_ssm` connection, `become_user`, and `block`/`rescue` — all carried in Devy's own `deploy/` tree.
Wiring (revised — D-016): a self-contained deploy workflow **in the Devy repo** drives the `deploy/`
play directly; operator and logic both live in Devy, the ops repo is uninvolved. Not raw SSM shell — Ansible gives
`become_user` (file-ownership safety), idempotency, and `block`/`rescue` (halt-and-hold vs rollback) for free.

### 18.2 The manifest handoff — SSM Parameter Store, commit-keyed
On a successful **whole-platform** build, CI writes an SSM Parameter Store entry: **key = the commit sha**
of the built ref, **value = the COMPLETE set of component image URIs + git ref + timestamp + build run-id**.
CD takes the commit sha as input, looks it up, and renders the `.env` of URIs — and **"key missing ⇒ CI
never succeeded"** is a free precondition. **Coherence rule:** only whole-platform builds produce
deployable manifests (a deploy needs a complete, coherent set — components move together); single-component
builds are CI-validation-only. Store is **per-account** (dev/prod param stores) → environment isolation for
free; a **GitHub Deployment** (+ optional git record) is the audit/reporting projection.

### 18.3 Ship rendered artifacts — no git on the host
The host needs only the **deploy compose file + the `.env` + Docker** — never a git clone, source, or repo
creds. The orchestrator (already has the repo checked out) renders the compose + `.env` and **ships them
over SSM** (Ansible copy/template). The host stays dumb and low-attack-surface.

### 18.4 Host targeting — dynamic inventory, not a static list
Reuse the tag-based `aws_ec2` inventory. Dropdown = stable **group** choices (`role_platform` /
`role_edge` / `all`) + an optional **specific-host override validated against the live inventory** at run
time. New hosts auto-join; nothing to maintain.

### 18.5 State & migrations — app-owned, expand/contract, DB stays up
Migrations are **app-owned**: Devy ships `db migrate` (+ a `schema_migrations` tracking table), run as a
**gated pre-step from the NEW image against the LIVE Postgres**. **Postgres transactional DDL** ⇒ atomic
migration with auto-rollback on migration-time failure. The rule that actually protects rollback:
**expand/contract (backward-compatible)** — the old app must still work against the new schema, so a
post-migration deploy failure rolls the **app** back safely (schema stays forward; destructive/contract
changes land a later release). With expand/contract + fix-forward, schema "down" migrations are
rare-to-never. **Bootstrap + incremental unify:** a fresh DB (empty tracking table) applies all migrations
from `0`; an existing DB applies only the delta — same code path. **The database is NOT cycled on an app
deploy** — only the app tier stops/replaces.

### 18.6 The deploy sequence (migration-gated)
```
record current deployed manifest (rollback target — a per-env/host "deployed" pointer in SSM/git)
→ resolve manifest: look up commit-sha in SSM Param Store → render .env
→ ship deploy compose + .env to the host (over SSM; become_user ec2-user/ubuntu)
→ if pending migrations: run `db migrate` from the NEW image against the LIVE Postgres — gate on success
→ stop the APP tier (leave Postgres up)
→ compose up the new app tier (pinned ECR images)
→ tier-1/2 (/3) smoke
→ pass ⇒ update the deployed pointer  |  fail ⇒ halt-and-hold (DEFAULT) or opt-in app-only auto-failback
→ report state to the orchestrator either way
```

### 18.7 Deployment strategy — recreate now, blue/green target
Single platform host: **recreate** (brief downtime in a planned window) is honest for the demo. **Target =
blue/green** (new stack on alternate ports, smoke live, flip the oauth2-proxy edge, keep old warm for
instant rollback) — the zero-downtime + instant-rollback pattern, and only *truly* instant **because**
expand/contract keeps the shared DB compatible across the flip.

### 18.8 Deliberate divergence from some "state of the art"
**Rejected** (philosophy, not ignorance): **GitOps pull** (Argo/Flux — continuous/automatic, the opposite
of deliberate release) and **automated progressive delivery** (auto-promote on metrics) — we keep the
*principle* (smoke/metrics gate promotion) but the **human pulls the trigger**. **Adopted as future
hardening:** **supply-chain integrity** — image signing (cosign/sigstore) + SBOM + "only signed,
provenance-verified images deploy." We already have provenance tags; signing is the next rung
(compliance-relevant for a trading firm).

### 18.9 Devy productionization is the prerequisite
Devy has only ever been built locally (`./devy.sh`). Making it remotely deployable is its own refactor —
tracked as a checklist in the Devy repo (`docs/remote-readiness.md`), which doubles as a reusable
"local → remote-managed" coaching template. That's the tactical detour before Step 3's deploy role.
