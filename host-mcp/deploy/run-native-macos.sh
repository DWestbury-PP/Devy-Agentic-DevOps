#!/bin/sh
# Run the Agentic DevOps host MCP NATIVELY on the host (macOS or Linux) so its
# host checks see the REAL machine — `log show` / `last reboot` / `df` — instead
# of the Linux-container view a bundled compose host-mcp reports. A containerized
# proxy dials this over the Docker gateway at http://host.docker.internal:8781/mcp
# (point its mcp_servers url there).
#
# Reads HOST_MCP_TOKEN from the repo-root .env (the same token the proxy sends).
# Used as-is by the launchd LaunchAgent (com.agentic-devops.host-mcp.plist).
set -eu

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

# Robustly extract just the token line (don't `source` .env — values may not be
# shell-safe). Never echo it.
TOKEN="$(grep -E '^HOST_MCP_TOKEN=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"'\r')"
if [ -z "${TOKEN:-}" ]; then
  echo "run-native-macos.sh: HOST_MCP_TOKEN not found in $REPO/.env" >&2
  exit 1
fi

export HOST_MCP_TOKEN="$TOKEN"
export HOST_MCP_TRANSPORT="${HOST_MCP_TRANSPORT:-http}"
export HOST_MCP_PORT="${HOST_MCP_PORT:-8781}"
export HOST_MCP_PROFILE="${HOST_MCP_PROFILE:-diagnostic}"
# Timed audit (check, args, argv, exit, duration_ms) per call — cheap, and the
# observability you want when Devy runs diagnostics against a real host.
export HOST_MCP_AUDIT="${HOST_MCP_AUDIT:-/tmp/agentic-devops-host-mcp-audit.jsonl}"
# Editable install + a space in the repo path is flaky; pin the package source.
export PYTHONPATH="$REPO/host-mcp/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$REPO/.venv/bin/python" -m host_mcp.cli
