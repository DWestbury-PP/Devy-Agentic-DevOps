#!/bin/sh
# Run the Agentic DevOps host MCP NATIVELY on a Linux host, BY HAND (dev / test).
#
# This is the convenience wrapper for an interactive run from a repo checkout —
# the sibling of run-native-macos.sh. The PRODUCTION path is the hardened systemd
# unit (agentic-devops-host-mcp.service.example), installed by
# install-native-linux.sh / the CD role, which execs the venv python directly with
# no shell wrapper. Use this only for a quick local run.
#
# Reads HOST_MCP_TOKEN from the repo-root .env (the same token the proxy sends).
# Never echoes it. Everything else falls back to sane defaults.
set -eu

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

# Robustly extract just the token line (don't `source` .env — values may not be
# shell-safe). Never echo it.
TOKEN="$(grep -E '^HOST_MCP_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"'\r')"
if [ -z "${TOKEN:-}" ]; then
  echo "run-native-linux.sh: HOST_MCP_TOKEN not found in $REPO/.env" >&2
  exit 1
fi

export HOST_MCP_TOKEN="$TOKEN"
export HOST_MCP_TRANSPORT="${HOST_MCP_TRANSPORT:-http}"
export HOST_MCP_PORT="${HOST_MCP_PORT:-8781}"
export HOST_MCP_PROFILE="${HOST_MCP_PROFILE:-diagnostic}"
# Timed audit (check, args, argv, exit, duration_ms) per call — cheap observability.
export HOST_MCP_AUDIT="${HOST_MCP_AUDIT:-/tmp/agentic-devops-host-mcp-audit.jsonl}"
# Editable install + a space in the repo path is flaky; pin the package source.
export PYTHONPATH="$REPO/host-mcp/src${PYTHONPATH:+:$PYTHONPATH}"

# Immutability is the default. To test enhanced mode by hand, append the switch:
#   HOST_MCP_ENHANCED=1 sh host-mcp/deploy/run-native-linux.sh
EXTRA=""
if [ "${HOST_MCP_ENHANCED:-}" = "1" ]; then
  EXTRA="--allow-mutations"
fi

# Prefer a repo venv if present, else the system python3.
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" -m host_mcp.cli $EXTRA
