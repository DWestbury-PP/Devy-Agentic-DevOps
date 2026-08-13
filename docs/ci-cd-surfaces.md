# Building a CI/CD surface (read + trigger)

> **Status: SCAFFOLD (2026-08-13).** The *contracts* a surface binds to are stable and
> filled in below (the release-ledger read API and the `repository_dispatch` event
> types + payloads). The **auth model is decided** — [§3](#3-authentication--who-may-fire-a-trigger),
> ADR D-017: the authenticated relay. The *how-to-build-a-surface* sections are
> deliberately skeletal — they firm up as the first real surfaces (web console, Slack,
> `request_deploy`) get built.

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
A surface **never holds a GitHub credential.** It authenticates against **Devy's
existing RBAC/SSO plane** (the same auth every other Devy capability uses), and the
**proxy** fires the `repository_dispatch` with **one vault-held credential**
(`devy/github/dispatch`) — a **fine-grained PAT initially, upgradeable to a GitHub App**
with **zero surface change** (the credential is centralized behind the proxy, so the swap
is an internal detail).

So the raw `dispatches` call in §2 is the **proxy's internal last hop**, not something a
surface makes — a surface calls the **proxy relay** (§4), and the proxy makes that call.

The candidates considered (retained for context):

| Model | Who holds the GitHub credential | Surface-facing auth | Outcome |
|---|---|---|---|
| A — scoped fine-grained PAT | each surface (or one shared token) | the PAT itself | rejected as surface-facing: broader-than-"trigger" scope, personal-identity-bound, per-surface secret handling |
| B — GitHub App | the App (short-lived install tokens) | mint per call | rejected as surface-facing (heaviest setup) — but the likely future **last hop inside** the relay |
| **C — authenticated relay (proxy)** ✅ | **the proxy only** (one vault credential) | **Devy's RBAC/SSO plane** | **chosen** — surfaces get zero GitHub scope; wraps A *or* B for the last hop |

**Why C:** surfaces get zero GitHub scope; one credential to rotate, in the vault; the
PAT→App swap is invisible to surfaces; every trigger flows through a **Devy principal**
(traced/logged) before GitHub sees it; and the relay *is* `request_deploy` (§4) on the
existing guarded-actions framework. The one trade-off — the proxy must be up to fire — is
a non-issue for Devy-centric surfaces, which already talk to the proxy.

---

## 4. The Devy relay path (`request_deploy`) — the reference surface

The first trigger surface is **Devy itself**, and it doubles as the reference
implementation of the relay (model C): Devy *proposes* a deploy, a human *approves*, and
the **proxy** fires the webhook with its vault-held credential. Devy has **no
directly-firing tool** — "never self-approve" stays structural, exactly like the
guarded host actions.

This reuses the existing **guarded-actions** framework (`request_action` → human
approval → proxy executes; `CLAUDE.md § Guarded actions`), so `request_deploy` is
mostly composition, not new machinery:

```
Devy  ──request_deploy(propose)──▶  pending_actions (CAS + TTL)
                                          │
                                    human approves (RBAC: elevated)
                                          │
                              proxy fires repository_dispatch  ← the one vault credential
                                          │
                                    GitHub Actions (plan → summary; apply on a second approval)
```

> **TODO (flesh when `request_deploy` is built):**
> - the tool's `safety_tier` + propose-only shape (mirror `request_action`)
> - the approval payload a human sees (workflow, event type, resolved `client_payload`,
>   plan-vs-apply)
> - where the proxy's GitHub credential lives (vault ref, e.g. `devy/github/dispatch`)
>   and how it's minted (PAT now / App token later — the model-C internal detail)
> - the trace/audit trail (every trigger attributable to a Devy principal)

---

## 5. Worked examples (per surface)

> **TODO — scaffold, fill as each surface is built:**
> - **Web console** — a release picker (read) + a plan/apply deploy button (trigger via
>   the relay). The account for `plan`-first + the approval step.
> - **Slack** — a `/devy deploy` slash command → relay → the plan summary posted back to
>   the channel; an "Apply" button that requires a second, role-gated click.
> - **Devy (chat)** — the `request_deploy` propose→approve→fire flow end to end.

---

## 6. Safety & guardrails (the non-negotiables a surface inherits)

Whatever the surface, these are structural — a surface may not weaken them:

- **Plan before apply.** Default to `mode: plan`; `apply` is a separate, explicit action.
- **RBAC tiering.** Triggering a deploy is an **`elevated`** capability (like guarded
  host mutations) — gate the surface's apply path on it.
- **No self-approval.** For the Devy surface, propose and approve are different
  principals by construction (guarded actions).
- **Attributable + logged.** Every trigger flows through an identifiable path (a named
  credential or, under the relay, a Devy principal) — no anonymous fires.

> **TODO:** reconcile these with the ratified auth model (§3) once chosen — e.g. how the
> relay maps a Devy principal onto the GitHub-side actor in the run's audit trail.

---

## See also

- [`ci-cd.md`](ci-cd.md) — the operator's map of the CI→CD seam (workflows, the SSM
  ledger schema, rollback, the L1→L2→L3 funnel). **Read this first.**
- [`deploy-design.md`](deploy-design.md) — the CD design + ADRs (§16 open questions,
  §17 decisions). The auth model lands here as **D-017**.
- [`security.md`](security.md) — the two-plane secrets model the relay credential joins.
