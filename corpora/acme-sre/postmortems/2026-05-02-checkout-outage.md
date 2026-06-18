# Postmortem: Checkout Latency / Partial Outage — 2026-05-02

> **Fiction** — demo content for Acme Payments.

- **Date:** 2026-05-02
- **Severity:** SEV-1 (started SEV-2, escalated)
- **Duration:** 47 minutes (14:03–14:50 ET)
- **Authors:** on-call SRE, checkout service owner
- **Status:** Resolved; action items tracked

## Summary

A deploy of `acme-risk` introduced a synchronous call to a feature store that
had not been warmed, raising `acme-risk` p99 from ~40 ms to ~2.1 s. Because
`acme-checkout` calls `acme-risk` **synchronously** on the authorization path,
checkout p99 rose above 1200 ms and triggered `CheckoutAuthP99High`. Checkout
success rate dipped to 98.7% as upstream timeouts fired.

## Impact

- ~47 minutes of degraded checkout; estimated 2,300 failed/abandoned checkouts.
- No data loss; the ledger remained consistent.

## Timeline (ET)

- **13:58** — `acme-risk` v412 deployed.
- **14:03** — `CheckoutAuthP99High` fires (SEV-2). Primary acknowledges.
- **14:09** — On-call identifies `acme-risk` latency as the driver via the
  golden-signals dashboard (per `runbooks/checkout-latency.md`).
- **14:14** — Success rate drops below 99.9%; incident raised to SEV-1, manager
  paged, `#acme-incident` opened.
- **14:17** — Mitigation A applied: `RISK_FAIL_OPEN=true` set; `acme-risk` scaled
  to 12 replicas.
- **14:23** — Checkout p99 recovering; success rate climbing.
- **14:31** — Root cause confirmed: cold feature store in v412.
- **14:40** — `acme-risk` rolled back to v411.
- **14:48** — `RISK_FAIL_OPEN` reverted to false; risk scoring fully restored.
- **14:50** — SLOs stable for 10 min; incident resolved.

## Root cause

`acme-risk` v412 added a synchronous feature-store lookup without a warm cache
or a timeout budget, and the synchronous checkout→risk coupling turned a
single-service regression into a checkout-wide latency event.

## What went well

- The checkout-latency runbook's decision tree pointed to `acme-risk` quickly.
- Fail-open mitigation restored customer experience within ~6 minutes of being
  applied.

## What went poorly

- v412 shipped without a feature-store warm-up or load test against the inline
  path.
- The synchronous risk dependency has no built-in timeout/circuit breaker by
  default — it relied on a manual flag flip.

## Action items

1. **Add a default timeout + circuit breaker** to the checkout→risk call so risk
   slowness fails open automatically (owner: checkout team).
2. **Warm-up gate** in the `acme-risk` deploy pipeline before it takes traffic
   (owner: risk team).
3. **Load-test the inline risk path** in staging as a release gate (owner: SRE).
4. Document the `RISK_FAIL_OPEN` flag prominently in the runbook (done).
