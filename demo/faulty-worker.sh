#!/bin/sh
# Demo "faulty" worker for the Phase 5 RCA showcase (opt-in `demo` compose
# profile). It simulates a payments worker whose DB connection pool fills up and
# then OOMs — exiting non-zero so compose's restart policy crash-loops it. That
# gives Devy a *real running* container to investigate live via the host MCP
# (docker_ps shows it restarting; docker_logs shows the escalation; docker_inspect
# shows the climbing RestartCount and exit code 137).
#
# The log line shapes (timestamped, WARN→ERROR→FATAL) are what a real
# investigation correlates; cross-reference corpora/platform/runbooks.
i=0
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) INFO  payments-worker starting (db pool max=20)"
while : ; do
  i=$((i + 1))
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if [ "$i" -lt 6 ]; then
    echo "$ts INFO  handled request id=$i in 24ms (db pool active=$i/20)"
  elif [ "$i" -lt 11 ]; then
    echo "$ts WARN  db pool pressure: active=$((14 + i))/20, requests queueing"
  elif [ "$i" -lt 15 ]; then
    echo "$ts ERROR could not acquire db connection: pool exhausted (20/20) after 5000ms"
  else
    echo "$ts FATAL out of memory: worker heap exceeded limit; terminating"
    exit 137
  fi
  sleep 2
done
