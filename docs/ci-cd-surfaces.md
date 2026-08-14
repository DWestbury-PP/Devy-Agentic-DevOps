# Building a CI/CD surface (read + trigger)

> **Status: SCAFFOLD (2026-08-13).** The *contracts* a surface binds to are stable and
> filled in below (the release-ledger read API and the `repository_dispatch` event
> types + payloads). The **auth model is decided** — [§3](#3-authentication--who-may-fire-a-trigger),
> ADR D-017: the authenticated relay. The *how-to-build-a-surface* sections are
> deliberately skeletal — they firm up as the first real surfaces (web console, Slack,
> Devy itself) get built.

This is the **surface developer's** guide: how to build an L3 face (a web console, a
Slack command, Devy itself) on top of the CI/CD foundation. For the *operator's* map of
the seam — the workflows, the SSM ledger schema, rollback — read
[`ci-cd.md`](ci-cd.md) first; this guide assumes it.

A surface does one or both of two things:

| Half | Question | Foundation it binds to |
|---|---|---|
| **Read** | "What can I deploy?" | the **release ledger** (SSM `/devy/builds/*`) via one read layer |
| **Trigger** | "Deploy *this*." | the workflows' **`repository_dispatch`** doors (one per workflow) |

Both halves are **already built and proven**; a surface is *just a face* over them.
Nothing here re-derives the ledger schema or re-implements a workflow — that's the
whole point of the funnel (`ci-cd.md §6`).

---

## 1. The read side — "what can I deploy?"

The read layer is **done** (`ReleaseLedger`, `proxy/releases.py`) — a single reader over
the SSM build ledger, exposed through three surfaces you can bind to directly:

| Surface | Interface | Auth | Use when |
|---|---|---|---|
| **CLI** | `agentic-devops releases ls\|show\|latest\|components` | your ambient/SSO AWS creds | terminal / scripting |
| **Admin API** | `GET /v1/admin/releases[/{sha}\|/latest\|/components]` | RBAC-admin (bearer) | a web/Slack surface |
| **Agent tool** | `list_releases` (Devy asks the ledger) | in-agent (tier-gated) | conversational |

The API responses are the parsed, typed `Release`/`Component` shapes (see
`ci-cd.md §2` for the JSON). A read surface is a thin client over `GET
/v1/admin/releases*` — **no AWS credentials on the surface**, the proxy's instance role
reads SSM.

- `show <sha>` accepts a **full or abbreviated** SHA (resolves like `git`).
- **`latest <branch>`** is the newest-on-branch pointer (may be a *partial* set);
  **`components`** is the coherent per-component latest (the `assembled-latest` deploy
  target). A read surface offering a "deploy latest" button should default to
  **`components`** (assembled), not the branch pointer — see the deploy-discipline note
  in `ci-cd.md §1`.

> **TODO (flesh when the first read surface lands):** a minimal client example
> (fetch → render a release picker), pagination/limit handling, and how a surface
> distinguishes `complete` vs `partial` in the UI.

---

## 2. The trigger side — the dispatch contracts

Every workflow has **two doors**: the UI/`gh` (`workflow_dispatch`) and the GitHub API
(`repository_dispatch`). A surface uses the **API door**. The `client_payload` mirrors
the dispatch inputs **exactly**, so a webhook and a button are the *same call*.

| Workflow | Event type | `client_payload` (all fields optional) | Defaults |
|---|---|---|---|
| `build.yml` | **`devy-build`** | `{ref, components, components_advanced, push}` | ref = default-branch HEAD · components = all · push = true |
| `deploy.yml` | **`devy-deploy`** | `{source, branch, commit, targets, mode}` | source per form · targets = role_platform · mode = plan |
| `host-mcp-deploy.yml` | **`devy-host-mcp-deploy`** | `{hosts, ref, profile, allow_mutations, mode}` | hosts = role_platform · ref = main · mode = plan |

The raw call (see `ci-cd.md §6` for a full example):

```bash
curl -sf -X POST \
  -H "Authorization: Bearer $CREDENTIAL" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/dispatches \
  -d '{"event_type":"devy-deploy","client_payload":{"source":"assembled-latest","mode":"plan"}}'
```

**Two safety invariants every trigger surface must honor:**
- **`mode: plan` first.** Deploys are plan/apply — a surface should default to `plan`
  (a `--check` dry run that posts a summary) and require an explicit, separate action
  to `apply`. Never make `apply` the one-click default.
- **`repository_dispatch` always runs on the default branch.** `build.yml` resolves the
  actual commit from `client_payload.ref` (a branch/tag whose tip it builds), defaulting
  to default-branch HEAD. A surface that means "build *this* branch" must **send `ref`**.

> **TODO (flesh per workflow):** the full input enumerations + valid value sets
> (`source` intents, `targets` roster, `profile` tiers), and what a `plan` run posts
> back (the summary shape a surface can render).

---

## 3. Authentication — who may fire a trigger

**✅ DECIDED (2026-08-13) — the authenticated relay (model C); [ADR D-017](deploy-design.md#17-decisions-to-record-as-adrs-seed-for-docsdecisionsmd).**
**⚠️ AMENDED (2026-08-14) — the relay is a *separate service*, NOT the proxy.** The shape
below is unchanged; only its host moved. See "Where the relay runs" after the table.

A surface **never holds a GitHub credential.** It authenticates against the **existing
RBAC/SSO plane**, and **the relay** fires the `repository_dispatch` with **one credential
of its own**. So the raw `dispatches` call in §2 is the **relay's internal last hop**, not
something a surface makes.

The candidates considered (retained for context):

| Model | Who holds the GitHub credential | Surface-facing auth | Outcome |
|---|---|---|---|
| A — scoped fine-grained PAT | each surface (or one shared token) | the PAT itself | rejected as surface-facing: broader-than-"trigger" scope, personal-identity-bound, per-surface secret handling |
| B — GitHub App | the App (short-lived install tokens) | mint per call | rejected as surface-facing (heaviest setup) — but the **last hop inside** the relay |
| **C — authenticated relay** ✅ | **the relay only** (one credential) | **the RBAC/SSO plane** | **chosen** — surfaces get zero GitHub scope; wraps A *or* B for the last hop |

**Why C:** surfaces get zero GitHub scope; one credential to rotate; the PAT→App swap is
invisible to surfaces; and every trigger flows through an identified principal
(traced/logged) before GitHub sees it.

### Where the relay runs — a small standalone service

The original decision put the relay **inside the proxy**, reasoning that "the proxy must be
up to fire" was a non-issue for Devy-centric surfaces. That weighed availability only, and
availability turned out to be the weaker of two concerns.

- **Blast radius.** The proxy's GitHub access is read-only *by construction*. A dispatch
  credential is the first credential it would hold that **changes production** — inside the
  one process that executes model-directed code. Prompt injection, a compromised dependency,
  or an RCE reaches that credential without ever passing through the approval flow.
  Propose-only and never-self-approve are controls on the **agent**, not on the **process**.
- **Attribution.** One shared credential makes every run in GitHub's audit log the same
  actor. That was this section's own unresolved TODO, and it is unresolvable while the
  relay's identity is the only identity GitHub sees. The fix is to have the relay inject the
  resolved principal into the `client_payload` so the workflow can echo it into its run
  summary — which requires the relay to *have* a principal, i.e. to authenticate callers.

Availability is genuinely the lesser issue and should not be over-weighted:
`workflow_dispatch` never goes away, so a relay outage degrades convenience rather than
stopping a release. That is a property to **preserve deliberately**, not an accident — see
§6.

So: keep model C, host it in a **small, boring service** with no agent in it. The assistant
becomes one of its clients (§4), alongside a Slack command or a web console. Practical
consequences:

- **Its own credential and its own identity plane verification** — it verifies the
  forwarded id_token itself rather than delegating to the assistant's proxy. Independence is
  the point.
- **A GitHub App, not a PAT, once there is exactly one holder.** With the credential
  centralized in one service, an App's per-repo `actions: write` scope and its own audit
  identity cost roughly fifty lines of installation-token minting, and a PAT would otherwise
  bind the release path to one person's account.
- **It must not be deployable *by* itself.** Whatever pipeline ships the relay has to remain
  reachable without the relay.

---

## 4. Devy as a client of the relay

Devy is **a surface, not the relay** (§3). Its job is the part it is genuinely good at —
reading the ledger, working out *what* should ship and whether it is safe — and then
handing off. It holds **no firing credential**, so "never self-approve" is structural in
the strongest available sense: the capability simply is not in the process.

```
Devy ── reads the ledger (list_releases) ──▶ proposes a concrete deploy
                                                     │
                              a human decides (RBAC: elevated)
                                                     │
                                   the RELAY fires repository_dispatch
                                    (its own credential; principal recorded)
                                                     │
                          GitHub Actions (plan → summary; apply on a second approval)
```

Two ways a surface can hand off, and they differ in more than ergonomics:

- **Compose, don't fire.** Devy emits the exact ready-to-run trigger — a `gh workflow run`
  invocation or a prefilled Actions link — and a human fires it **as themselves**. No new
  credential anywhere, and GitHub's audit trail names the actual person. This is the
  cheapest correct surface and a good first one.
- **Call the relay.** For surfaces that need a real button (Slack, a web console), the
  surface calls the relay with the caller's identity and the relay fires. Attribution is
  preserved by the principal-injection in §3.

Devy's read side needs no relay at all — `list_releases` and the release API are already
sufficient for "what can I deploy?", and the ledger lives in SSM rather than GitHub, so
that half keeps working even during a GitHub incident.

> **TODO (flesh when the first trigger surface is built):**
> - the propose-only tool shape (mirror `request_action`) and its `safety_tier`
> - what a human sees before approving (workflow, event type, resolved `client_payload`,
>   plan-vs-apply)
> - the relay's caller-auth contract and how it maps a caller to a principal
> - the trace/audit trail on both sides (the relay's own record, and the principal echoed
>   into the GitHub run summary)

---

## 5. Worked examples (per surface)

> **TODO — scaffold, fill as each surface is built:**
> - **Web console** — a release picker (read) + a plan/apply deploy button (trigger via
>   the relay). The account for `plan`-first + the approval step.
> - **Slack** — a `/devy deploy` slash command → relay → the plan summary posted back to
>   the channel; an "Apply" button that requires a second, role-gated click.
> - **Devy (chat)** — propose→hand-off end to end: Devy reads the ledger, proposes a
>   concrete deploy, and either composes the ready-to-run trigger for a human to fire as
>   themselves or calls the relay on their behalf (§4).

---

## 6. Safety & guardrails (the non-negotiables a surface inherits)

Whatever the surface, these are structural — a surface may not weaken them:

- **Plan before apply.** Default to `mode: plan`; `apply` is a separate, explicit action.
- **RBAC tiering.** Triggering a deploy is an **`elevated`** capability (like guarded
  host mutations) — gate the surface's apply path on it.
- **No self-approval.** For the Devy surface, propose and approve are different
  principals by construction (guarded actions).
- **Attributable + logged.** Every trigger flows through an identifiable path (a named
  credential or, under the relay, an authenticated principal) — no anonymous fires. Because
  the relay's own credential is what GitHub sees, the principal must be carried in the
  `client_payload` and echoed into the run summary (§3).
- **Never the sole path.** `workflow_dispatch` stays live and documented as break-glass, and
  **a surface may hold no capability GitHub's own UI lacks**. This is what keeps a convenience
  layer from quietly becoming a dependency an organization cannot ship without — it erodes
  silently otherwise, so prove the fallback rather than assuming it.

---

## See also

- [`ci-cd.md`](ci-cd.md) — the operator's map of the CI→CD seam (workflows, the SSM
  ledger schema, rollback, the L1→L2→L3 funnel). **Read this first.**
- [`deploy-design.md`](deploy-design.md) — the CD design + ADRs (§16 open questions,
  §17 decisions). The auth model lands here as **D-017**.
- [`security.md`](security.md) — the two-plane secrets model the relay credential joins.
