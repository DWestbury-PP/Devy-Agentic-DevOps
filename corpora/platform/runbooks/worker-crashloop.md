# Runbook: Worker Crash-Loop / DB Pool Exhaustion

> Platform runbook for the Agentic DevOps demo stack. Pairs with the Phase 5
> incident showcase (the `demo-faulty` container).

**Symptom:** a worker container restarts repeatedly (crash-loop); `docker ps`
shows it cycling and its `RestartCount` climbs.

## Signature in the logs

The failure escalates over ~30 s before each crash:

1. `INFO` — normal request handling, DB pool usage creeping up.
2. `WARN  db pool pressure: active=N/20, requests queueing` — the pool is filling.
3. `ERROR could not acquire db connection: pool exhausted (20/20)` — requests now
   block on the pool and time out.
4. `FATAL out of memory … terminating` then exit `137` — queued work piles up in
   memory until the worker is OOM-killed; the restart policy brings it back and
   the cycle repeats.

## Diagnose

- `docker_ps` / `docker_ps_all` — confirm the restart cycling.
- `docker_logs` — read the WARN→ERROR→FATAL escalation; note the timestamps.
- `docker_inspect` — `RestartCount`, and `State.ExitCode` (137 ⇒ killed, often OOM).
- `docker_stats` — memory climbing toward the limit before each exit.
- Correlate the log timestamps with the restart times to confirm the pool
  pressure precedes the OOM (cause), not the reverse.

## Root cause (typical)

The DB connection pool is too small for the offered load (or a connection leak
holds connections open). Requests queue waiting for a connection; the backlog
grows in memory until the worker OOMs. The OOM is a *symptom* — the pool
exhaustion is the root cause.

## Mitigation

1. **Immediate:** raise the worker's memory limit to stop the OOM crash-loop and
   restore availability, and/or scale out workers to shed load.
2. **Fix the cause:** increase the DB connection pool size to match concurrency,
   or fix the connection leak (ensure connections are returned to the pool).
3. **Guardrail:** add backpressure — reject/queue with a bounded timeout instead
   of letting work accumulate unbounded in memory.

## Verify

- `RestartCount` stops climbing; no new `FATAL`/exit 137.
- Pool no longer reaches 20/20 under normal load; no `ERROR pool exhausted`.
