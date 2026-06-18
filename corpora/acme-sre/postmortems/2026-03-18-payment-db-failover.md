# Postmortem: payments-pg Writer Saturation & Failover — 2026-03-18

> **Fiction** — demo content for Acme Payments.

- **Date:** 2026-03-18
- **Severity:** SEV-1
- **Duration:** 34 minutes (09:12–09:46 ET)
- **Authors:** on-call SRE, data team lead
- **Status:** Resolved

## Summary

A nightly analytics backfill job was misconfigured to run against the
**`payments-pg` writer** instead of a reader. It saturated writer CPU (sustained
> 95%), driving checkout commit latency up and tripping `CheckoutAuthP99High`,
then a success-rate breach. The on-call paused the backfill and performed a
managed Aurora failover to a healthy reader to clear stuck connections.

## Impact

- 34 minutes of elevated checkout latency; ~1,100 slow/failed checkouts.
- No data loss. The failover caused a ~50 s write blip as expected.

## Timeline (ET)

- **09:05** — Misconfigured backfill job starts against `payments-pg` writer.
- **09:12** — `CheckoutAuthP99High` fires. Primary acknowledges.
- **09:18** — Aurora dashboard shows writer CPU > 95%; latency is DB-side, not
  `acme-risk` (per the checkout-latency decision tree → Mitigation B).
- **09:24** — Runaway backfill identified via `Aurora / Top SQL`; escalated to
  `@acme-data`, who killed the job.
- **09:29** — Writer CPU still pinned by queued work and stuck connections;
  decision to fail over per `runbooks/db-failover.md`.
- **09:31** — Replica lag confirmed < 1 s; managed failover triggered.
- **09:32** — ~50 s write unavailability during promotion.
- **09:36** — `acme-checkout` pods restarted to drop stale connections.
- **09:46** — SLOs stable for 10 min; resolved.

## Root cause

The analytics backfill's connection string pointed at the cluster **writer
endpoint** rather than a **reader endpoint** — a config error that code review
missed. The writer had no workload isolation from ad-hoc analytics queries.

## What went well

- Decision tree correctly distinguished a DB-side cause from `acme-risk`.
- Failover pre-checks (healthy reader, low lag) were followed; no write loss.

## What went poorly

- A non-critical batch job could directly impact the checkout writer.
- No alert on "non-application principal connected to writer".

## Action items

1. **Force analytics jobs onto reader endpoints** via a dedicated read-only DB
   user with no writer access (owner: data team).
2. **Alert** when an unexpected principal opens connections to the writer
   (owner: SRE).
3. **Right-size** the writer and evaluate Aurora's I/O-optimized class for
   headroom (owner: data team).
4. Add a connection-string lint to CI for analytics repos (owner: data team).
