# Roadmap

> **Forward-looking.** Phases 0–9 are built, tested, and live-verified (see the
> [README status](../../README.md#status--roadmap)). Most later phases are still
> **planned** — a dependency-ordered chain where each phase unlocks the next — but a
> few have been **delivered ahead of the chain** and are marked **✅ shipped** inline:
> **Grafana MCP** (Phase 13), **Identity & access / SSO + RBAC** (Phase 10), and the
> product-defining **guarded actions** (Phase 16). Numbering continues from the
> completed **Phase 9** (admin control plane).

The thread runs from *foundation* → *expanded reach* → *the leap from observing to
acting*. The product-defining destination **guarded actions** (Phase 16) has now
shipped; the remaining headline is **proactive / ChatOps** (Phase 17). Everything
around them is the platform and safety groundwork that makes acting trustworthy on
real infrastructure.

Several phases depend on systems that only exist in a real (often work) deployment —
a cloud account, a Grafana tenant, an SSO provider. Those are marked
**🏗 needs a real deployment**: the design lands when the project is pulled into an
environment that has them.

```
10 Identity & access ─┬─▶ 11 Observability & eval ─▶ 12 Extended retrieval
                      │                                      │
                      └──────────────┬───────────────────────┘
                                     ▼
            13 Reach (BYO-MCP + observability adapters) ─▶ 14 DB-broker MCP
                                     │
                                     ▼
                       15 Hosting hardening ─▶ 16 Guarded actions ─▶ 17 Proactive / ChatOps
```

---

## Phase 10 — Identity & access (real SSO + RBAC)

**Why first:** every multi-user, auditable, or action-taking feature downstream
keys off *real* identity. Today identity is honor-system (`X-User-Id`) and admin is
an interim password — both are deliberate seams, so this is the lowest-friction,
highest-leverage first move.

- **Real SSO** replacing the interim admin password: a JWT verifier (Google /
  Cloudflare+Okta) drops into the existing `require_admin` dependency; `user_id`
  comes from a trusted `email` claim, set via the `authHeaders()` client seam.
- **RBAC** on top of authn: roles/scopes that gate *who may admin*, *who may use
  the `elevated` host-MCP profile*, and *which corpora a user/team can see*. The
  honor-system `user_id` becomes a real principal.

**Unlocks:** trustworthy audit (Phase 15), per-identity budgets (15), and the
authority model for guarded actions (16). 🏗 *SSO provider needed for the JWT half;
RBAC primitives can land against the local auth first.*

## Phase 11 — Observability & evaluation

**Why next:** instrument before you expand. You can't safely grow what the agent
does — or trust its answers — without seeing its reasoning and measuring its
quality. Builds directly on nothing but the existing harness/tracing seam.

- **LLM observability:** waterfall tracing of every harness cycle and model/tool
  call through **LangSmith** (the `tracing.py` seam already abstracts this), plus
  proxy-level metrics (latency, tokens, tool mix) for ops.
- **Evaluation & feedback harness:** a golden Q&A set run against the proxy, answer
  feedback (👍/👎 in the surfaces), and a regression gate so a model/prompt/RAG
  change can't silently degrade answers. Traces become eval fixtures.

**Unlocks:** every later phase ships behind a quality gate; the eval set proves
whether a new retrieval backend (12) or MCP (13) actually helps. 🏗 *LangSmith
account for the hosted-tracing half; local JSONL tracing already works.*

## Phase 12 — Extended retrieval & fresh knowledge

**Why now:** the eval harness (11) lets you *prove* each new retrieval mode earns
its place rather than adding noise. Extends today's vector + full-text hybrid.

- New **find_tools-discovered retrieval backends**, fused into the existing hybrid
  philosophy: **file search** (grep-like over mounted docs), **structured / JSONB
  search** (query metadata and config blobs), **web search** (Tavily / Brave for
  current external context), and optionally **graph search** (entity/relationship
  traversal over services and incidents).
- **Knowledge freshness:** scheduled / git-webhook **re-ingest** so corpora (the
  repo, runbooks) don't go stale — reusing the idempotent, embed-recipe-aware
  ingest pipeline.

**Unlocks:** richer grounding for RCA and for the observability adapters in 13.

## Phase 13 — Reach: BYO-MCP hardening + observability adapters

**Why now:** RBAC (10) governs who may mount/use which server; tracing (11) makes
the new external tool calls debuggable. Today you *can* mount any MCP server — this
phase makes that a first-class, governed experience and ships reference adapters.

- **BYO-MCP hardening:** per-server profile/scoping, admin-UI configuration and
  health, and vetting guidance — mounting a third-party MCP is treated like adding
  a dependency.
- **Observability MCP adapters** (reference integrations): **Grafana MCP** ✅ —
  the official `grafana/mcp-grafana` mounted read-only via the S-4 registry (bundled
  `grafana-mcp` compose sidecar; header-auth with a vault-mastered service-account
  token; see [Extending → Observability](../extending.md#observability)). Still
  ahead: **AWS CloudWatch / CloudTrail** (metrics, logs, and API-call correlation)
  and **AWS auto-discovery** populating the host registry (instance-id / account /
  region → SSM targeting).

**Unlocks:** real signal sources for RCA and proactive investigation (17).
🏗 *Grafana MCP delivered against a live tenant; AWS adapters pending an account.*

## Phase 14 — DB-broker MCP (safe query plane)

**Why now:** it reuses the allow-list pattern proven by the host MCP, plus RBAC
(10) and audit (15). This is the SecOps-friendly answer to "don't hand the agent
the database."

- A **fixed, parameterized set of queries and functions** — never raw SQL, never
  full DB access. The read-side twin of the host-MCP allow-list: a CISO can read
  the broker's manifest and know the complete set of things the agent can ask the
  database.
- Profile-gated and audited like every other MCP; mutating broker calls (if ever
  enabled) flow through the guarded-action path (16).

**Unlocks:** data-layer grounding without the blast radius that terrifies SecOps.

## Phase 15 — Hosting hardening

**Why now:** the production-deployment tax — and the CISO story — once Devy is
shared and reaching real systems. Builds on RBAC (10) and observability (11).

- **Secrets backend:** Vault / AWS Secrets Manager / SSM Parameter Store instead of
  `.env` files (natural alongside the AWS work in 13).
- **Cost & rate controls:** per-user / per-team token budgets and spend caps.
- **Data redaction:** scrub secrets / PII from tool output (logs, configs) *before*
  it reaches the model/embedding provider.
- **Unified audit trail:** a queryable, exportable record of *who asked what, which
  tools ran, and what data left for the provider* — consolidating the host-MCP
  audit log into one compliance surface.

**Unlocks:** the safety substrate guarded actions (16) record into and depend on.

## Phase 16 — Guarded actions (observe → act) ✅ shipped

**The product-defining leap — delivered.** Devy can now *propose* a reversible
remediation and a human *approves* it before anything runs; the proxy (never the
agent) then executes it on the host MCP.

- Propose-only `request_action` tool (restart a service/container, reload config,
  prune images — Tier-A reversible verbs only); a human **approves / denies in the
  UI** before execution.
- **Triple-gated:** a host-MCP deployment switch (`HOST_MCP_ALLOW_MUTATIONS`), the
  RBAC **`elevated`** tier, and per-action human approval (CAS + TTL). Devy has no
  directly-mutating tool ("never self-approve" is structural), and any mounted
  write-tool is withheld from it by a `readOnlyHint` filter. Fail-closed unless
  `auth.mode: jwt` (or an explicit dev opt-in). DB-broker mutations (14) will follow
  the same shape.

**Delivered:** Devy stops being a copilot that only *tells* you what's wrong and
starts helping *fix* it — under human control.

## Phase 17 — Proactive & ChatOps (the teammate leap)

**Why last:** a proactive investigation can end in a *proposed guarded action* (16),
and running unattended is only trustworthy on top of tracing (11) and audit (15).

- **Triggered mode:** a webhook ingress (Alertmanager / PagerDuty / CloudWatch
  alarm) kicks off an automatic RCA; Devy investigates and posts a cited summary
  rather than waiting to be asked.
- **ChatOps surface:** a **Slack / Teams** thin client (another dumb client of the
  same API) so Devy lives in the incident channel where SREs already work.

**Unlocks:** Devy as a teammate — present in the channel, reacting to alerts,
proposing fixes for a human to approve. 🏗 *Best validated against a real
alerting/ChatOps stack.*

---

## Notes

- This is a *plausible* order, not a contract — phases can reorder as real needs
  surface, and the 🏗 ones are explicitly gated on having the right environment.
- Each phase still follows the repo's **design-before-code** convention: a design
  proposal (and its forks) gets signed off before the branch/PR work begins. See
  [CONTRIBUTING](../../CONTRIBUTING.md).
