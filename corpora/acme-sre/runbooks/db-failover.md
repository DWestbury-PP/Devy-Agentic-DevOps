# Runbook: Aurora payments-pg Failover

> **Fiction** — demo content for Acme Payments.

**Applies to:** `payments-pg` (Aurora PostgreSQL, 1 writer + 2 readers).
**Severity:** SEV-1 (checkout writes depend on the writer).

## When to use

- Writer instance is unhealthy/unreachable, OR
- Writer is saturated and a larger instance is needed and other mitigations in
  `runbooks/checkout-latency.md` (B) were insufficient.

Aurora failover promotes a reader to writer and re-points the cluster writer
endpoint. Expect **30–90 seconds** of write unavailability during promotion.

## Pre-checks

1. Confirm at least one **healthy reader** exists (`Acme / Aurora Health` →
   replica status). Never fail over with no promotable reader.
2. Check **replica lag** is low (< 1 s). Failing over to a lagging replica risks
   acknowledged-write loss.
3. Announce in `#acme-incident`: failover starting, ~60 s write blip expected.

## Procedure

1. Trigger managed failover (preferred — picks the healthiest reader):

   ```
   aws rds failover-db-cluster --db-cluster-identifier payments-pg
   ```

2. Watch the writer endpoint re-point. `acme-checkout` uses the cluster writer
   endpoint, so it reconnects automatically once DNS updates (TTL 5 s). No app
   redeploy needed.
3. If checkout connections are stuck on stale connections, restart the pods:
   `kubectl rollout restart deploy/acme-checkout`.

## Verification

- New writer is serving; old writer is now a reader or being replaced.
- Checkout success rate recovers above 99.9%.
- No ledger write errors (`acme-ledger` uses a separate cluster, but confirm).

## Post-failover

- Open a postmortem if this was triggered by an incident (template in
  `postmortems/`).
- If the old writer was undersized, file a capacity ticket to right-size the
  instance class permanently rather than relying on failover.
