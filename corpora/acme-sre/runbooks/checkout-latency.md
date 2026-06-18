# Runbook: Checkout Latency High

> **Fiction** — demo content for Acme Payments.

**Alert:** `CheckoutAuthP99High` — checkout authorization p99 > 1200 ms for 5 min.
**Severity:** SEV-2 (SEV-1 if success rate also drops below 99.9%).
**Owning service:** `acme-checkout`.

## Impact

Customers experience slow or timed-out checkouts. Sustained breach risks cart
abandonment and downstream retries that amplify load.

## First 5 minutes (triage)

1. Open Grafana **`Acme / Checkout Golden Signals`**. Confirm the latency rise
   is real (not a single-instance artifact) and note when it started.
2. Check the **`acme-risk` latency panel** on the same dashboard. The most
   common cause is `acme-risk` slowness propagating into checkout (they're
   synchronously coupled — see `architecture.md`).
3. Check **Aurora `payments-pg`** writer CPU and commit latency on
   `Acme / Aurora Health`.

## Decision tree

- **`acme-risk` p99 is the spike** → go to *Mitigation A*.
- **`payments-pg` writer CPU > 85% or commit latency elevated** → go to
  *Mitigation B*.
- **Neither — latency is in `acme-checkout` itself** → check a recent deploy
  (`kubectl rollout history deploy/acme-checkout`); if a deploy preceded the
  spike, roll back (*Mitigation C*).

## Mitigation

### A. acme-risk is slow

`acme-risk` failing open is acceptable for short windows — fraud scoring is
advisory, not authoritative, for low-value transactions.

1. Enable the risk **fail-open feature flag**:
   `acme-checkout` env `RISK_FAIL_OPEN=true` (Helm value `risk.failOpen`).
   This makes checkout skip risk scoring after a 150 ms timeout instead of
   blocking.
2. Scale `acme-risk`: `kubectl scale deploy/acme-risk --replicas=12`.
3. Watch checkout p99 recover within ~3 min.

### B. payments-pg writer saturated

1. Identify slow queries: check the `Aurora / Top SQL` panel.
2. If a runaway batch job is the cause, pause it (see `oncall-playbook.md`
   escalation to the data team).
3. If the writer is simply undersized for a traffic spike, fail over to a larger
   instance only as a last resort — see `runbooks/db-failover.md`.

### C. Bad deploy

```
kubectl rollout undo deploy/acme-checkout
```

Confirm p99 recovers; open an incident ticket and notify the checkout team.

## Verification

- Checkout p99 back under 800 ms for 10 consecutive minutes.
- Success rate restored above 99.9%.
- If you set `RISK_FAIL_OPEN=true`, **revert it** once `acme-risk` is healthy and
  note it in the incident timeline (running fail-open indefinitely is a risk-policy
  violation).

## Escalation

If not mitigated within 20 minutes, escalate to the secondary on-call and the
checkout service owner per `oncall-playbook.md`.
