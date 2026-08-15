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
| `deploy.yml` | **`devy-deploy`** | `{source, branch, commit, targets, mode}` **+ `{requested_by, request_id}`** | source per form · targets = role_platform · mode = plan |
| `host-mcp-deploy.yml` | **`devy-host-mcp-deploy`** | `{hosts, ref, profile, allow_mutations, mode}` | hosts = role_platform · ref = main · mode = plan |

The raw call (see `ci-cd.md §6` for a full example):

```bash
curl -sf -X POST \
  -H "Authorization: Bearer $CREDENTIAL" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/dispatches \
  -d '{"event_type":"devy-deploy","client_payload":{"source":"assembled-latest","mode":"plan"}}'
```

**Attribution fields (`deploy.yml` today; the others follow the same shape when they need it).**
A relay fires with one shared credential, so the provider's actor field can only ever show the
relay — **the requester has to travel in the payload or it is lost** (§3). Two optional fields
carry it; both absent means unchanged behaviour:

- **`requested_by`** — the principal the relay authenticated. It appears in the **run title** and
  the job summary, so a human is named on the run itself. A manual `workflow_dispatch` falls back
  to `github.actor`, so every run names someone either way.
- **`request_id`** — the caller's correlation handle, echoed into the run title. This is not a
  nicety: **`POST /repos/{owner}/{repo}/dispatches` returns `204` with no body**, so a surface
  cannot learn the run id from the call it just made. Matching on the run title is how it finds
  its own run.

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
  centralized in one service, an App's per-repo scopes and its own audit identity cost
  roughly fifty lines of installation-token minting, and a PAT would otherwise bind the
  release path to one person's account. The scope `repository_dispatch` needs is
  **`Contents: write`** — GitHub files it under Contents, not Actions. Worth stating
  plainly, because it is broader than it sounds: `Contents: write` also permits pushing
  code. (`Actions: read` is separate, and is what lets the relay find the run it fired.)
- **It must not be deployable *by* itself.** Whatever pipeline ships the relay has to remain
  reachable without the relay.

---

## 3b. Requesting is not authorizing

§3 settles that a surface holds no provider credential. That leaves a second question it
does **not** answer: once the relay *can* fire, what stops any authenticated caller from
firing anything?

The tempting answer is to give the relay an approval gate. That is the same mistake §3 just
backed out of, one layer along — **an approval gate is an authority mechanism**, and putting
authority in the relay means one component both decides who may change production and holds
the credential that does it.

So split the two:

| Step | Who | What they contribute |
|---|---|---|
| **Request** | any surface | a human, asserted as strongly as that surface can |
| **Authorize** | the provider | whether that person may actually change this repository |
| **Execute** | the relay | firing the trigger, once both of the above are settled |

### Surfaces are not equally trustworthy, and that is fine

A surface behind SSO can forward a verified `id_token`. A chat integration can assert only
"this chat user, whose profile email is X". A ticket automation is weaker still. Requiring one
bar either excludes the weak surfaces or over-trusts them.

**Record the strength instead.** Carry a `strong`/`weak` claim alongside the asserted
principal, and put it in front of whoever approves. This grants nothing — it is a label, not a
permission — but it makes a weak claim *harmless* rather than excluded, because a weak claim
cannot act alone. The payoff is that the surface contract collapses to almost nothing: a new
surface authenticates itself and asserts a user. No user auth plane, no elevated role, no
credential that can change production.

This is also why a plain "pass everything through" relay does not work. A passthrough must
*trust* its callers, and trust is exactly the property that varies per surface.

### Pin the release when it is requested, not when it runs

"Deploy the latest from a branch" is a legitimate intent and a *moving* one. Resolve it to a
concrete manifest **at request time**, so that approving and shipping cannot be two different
things — and so a retry after a failure is provably the same artifact rather than whatever
landed in the meantime.

> **Resolve against whatever registry records what is actually deployable — not against the
> version control ref.** These are different sets: a docs-only commit never runs a build, so a
> branch tip frequently has no artifact. Pinning the ref rather than the built artifact
> produces an approval that cannot ship, and it fails *after* someone has approved it. Whenever
> two systems both key on "commit", check they mean the same set.

Then the identity of a request is `(manifest, target)`, and the consequences follow:

- **A different manifest or a different target is a new request** — that is the tracking
  boundary.
- **Approval binds the pairing, not the attempt.** A retry of the same artifact to the same
  place needs no fresh approval: it changes nothing about the security posture, and a process
  that demands ceremony per retry is one an engineer recovering an outage will route around —
  fragmenting the audit trail exactly when it matters most.
- **Success is terminal.** A succeeded request accepts no further attempts.
- **Every attempt is its own record.** "Who fired the one that actually shipped" is the first
  question an incident review asks, and a single mutable row cannot answer it.

### Let the provider answer "who may approve"

The approver's authority is a question the **provider** already knows the answer to — ask it
rather than reimplementing it:

```
GET /repos/{owner}/{repo}/collaborators/{login}/permission   →   write | admin
```

For a GitHub App this needs only **`Metadata: read`**, which every App holds mandatorily — so
delegating the authority question adds *no* scope to the credential that can change
production. It also gives per-team scoping transitively and for free: teams grant repository
access, so a platform team with write across an estate can approve anything, while an
application team with write on its own repositories can approve only those.

> **Do not design around a team-membership call.** A workflow's default token is
> repository-scoped and has no organization/team read scope, so that route needs a second
> credential in the release path and buys nothing the permission check does not already give.

Two failure modes worth naming, because both are easy to get backwards:

- **A refused approval must not move the request.** If it did, anyone holding a token could
  kill somebody else's release just by failing to approve it.
- **"The provider could not be reached" is not "permission denied."** It is a failure to
  decide. Report it as such and retry; rendering it as a refusal turns an outage into a
  confident, wrong answer.

### Where the platform offers this natively, prefer configuration

Deployment environments with required reviewers do all of the above with no code: the relay
dispatches and the run simply waits at the gate. **Check your plan tier before designing on
it** — required-reviewer protection rules are not available on private repositories under
every plan, and discovering that after building for it is expensive. Where they are
unavailable, the fallback above is worth keeping deliberately thin, because it is a
stand-in for a platform feature rather than a thing to grow.

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
  principals by construction (guarded actions). Under a request/approve split (§3b) the
  same rule is structural for *every* surface: the requester may not authorize their own
  request.
- **Attributable + logged.** Every trigger flows through an identifiable path (a named
  credential or, under the relay, an authenticated principal) — no anonymous fires. Because
  the relay's own credential is what GitHub sees, the principal must be carried in the
  `client_payload` and echoed into the run summary (§3).
- **Never the sole path.** `workflow_dispatch` stays live and documented as break-glass, and
  **a surface may hold no capability GitHub's own UI lacks**. This is what keeps a convenience
  layer from quietly becoming a dependency an organization cannot ship without — it erodes
  silently otherwise, so prove the fallback rather than assuming it.
- **Every relay-supplied field is optional, with a correct fallback.** A workflow here must
  work, and read sensibly, for someone running it by hand with no relay at all — that is why
  `requested_by` falls back to `github.actor` rather than to an empty string or a placeholder.
  The failure this prevents: a workflow that is inert, broken, or merely confusing without a
  component the reader does not have and cannot obtain. A field that degrades to a *correct*
  value is a seam; one that degrades to nothing useful is a dead end. The same test applies to
  documentation — describe the *pattern* so a reader can build their own, never a particular
  deployment's instance of it.

---

## See also

- [`ci-cd.md`](ci-cd.md) — the operator's map of the CI→CD seam (workflows, the SSM
  ledger schema, rollback, the L1→L2→L3 funnel). **Read this first.**
- [`deploy-design.md`](deploy-design.md) — the CD design + ADRs (§16 open questions,
  §17 decisions). The auth model lands here as **D-017**.
- [`security.md`](security.md) — the two-plane secrets model the relay credential joins.
