#!/usr/bin/env bash
# Idempotent installer for the NATIVE Linux host MCP sidecar (systemd).
#
# Run as root on the TARGET host (AL2023 / RHEL / Fedora / Ubuntu — anything with
# systemd). It is the reference the CD role mirrors, and a first-class manual path.
# Re-running it reconciles to the desired state (safe to run repeatedly).
#
# It:
#   1. creates the unprivileged `devy-hostmcp` system user (+ docker/systemd-journal
#      group membership — the entire privilege surface),
#   2. builds a venv at /opt/agentic-devops-host-mcp and pip-installs host-mcp from
#      a git-SHA-pinned source (immutability without an image), or a local path,
#   3. installs sysstat (for disk_io/iostat) and a ping_group_range sysctl (so
#      ping_host works with ZERO capabilities),
#   4. writes /etc/agentic-devops/host-mcp.env (0640, token never world-readable),
#   5. installs the hardened unit and `systemctl enable --now`s it.
#
# Inputs (env):
#   HOST_MCP_TOKEN     (required) bearer token the proxy will send
#   HOST_MCP_SOURCE    pip source. Default: the public repo @ the given ref, e.g.
#                      "git+https://github.com/DWestbury-PP/Devy-Agentic-DevOps@<sha>#subdirectory=host-mcp"
#                      May also be a local path to a host-mcp/ checkout.
#   HOST_MCP_REF       git ref/sha to pin when HOST_MCP_SOURCE is unset (default: main)
#   HOST_MCP_PORT      listen port (default 8781)
#   HOST_MCP_BIND      bind address (default 0.0.0.0 — front with NSG + bearer)
#   HOST_MCP_PROFILE   read profile (default diagnostic)
set -euo pipefail

SERVICE_USER="devy-hostmcp"
APP_DIR="/opt/agentic-devops-host-mcp"
VENV="$APP_DIR/venv"
ENV_DIR="/etc/agentic-devops"
ENV_FILE="$ENV_DIR/host-mcp.env"
UNIT="/etc/systemd/system/agentic-devops-host-mcp.service"
REPO_URL="https://github.com/DWestbury-PP/Devy-Agentic-DevOps"
HERE="$(cd "$(dirname "$0")" && pwd)"

: "${HOST_MCP_TOKEN:?set HOST_MCP_TOKEN (the bearer token the proxy sends)}"
HOST_MCP_REF="${HOST_MCP_REF:-main}"
HOST_MCP_SOURCE="${HOST_MCP_SOURCE:-git+${REPO_URL}@${HOST_MCP_REF}#subdirectory=host-mcp}"
HOST_MCP_PORT="${HOST_MCP_PORT:-8781}"
HOST_MCP_BIND="${HOST_MCP_BIND:-0.0.0.0}"
HOST_MCP_PROFILE="${HOST_MCP_PROFILE:-diagnostic}"

if [ "$(id -u)" != "0" ]; then echo "must run as root" >&2; exit 1; fi

log() { printf '\033[36m==>\033[0m %s\n' "$*"; }

# 1. package deps: python venv + sysstat (iostat for disk_io). Detect dnf vs apt.
log "installing OS deps (python3, sysstat)"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip sysstat >/dev/null
elif command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null && apt-get install -y python3 python3-venv python3-pip sysstat >/dev/null
else
  echo "no supported package manager (dnf/apt) found" >&2; exit 1
fi

# 2. unprivileged service user + group membership (create groups only if present).
log "ensuring service user ${SERVICE_USER}"
id "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
# journal read + docker socket — the ONLY privileges granted.
getent group systemd-journal >/dev/null 2>&1 && usermod -aG systemd-journal "$SERVICE_USER"
getent group docker          >/dev/null 2>&1 && usermod -aG docker "$SERVICE_USER"

# 3. venv + package (pinned source). --upgrade makes re-runs reconcile the version.
log "building venv + installing host-mcp from: ${HOST_MCP_SOURCE}"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
if [ -d "$HOST_MCP_SOURCE" ]; then
  # Local checkout: pip accepts "<path>[extra]".
  "$VENV/bin/pip" install --quiet --upgrade "${HOST_MCP_SOURCE}[http]"
else
  # Git (or any URL): PEP 508 direct reference — "<dist>[extra] @ <url>".
  "$VENV/bin/pip" install --quiet --upgrade "agentic-devops-host-mcp[http] @ ${HOST_MCP_SOURCE}"
fi
chown -R root:root "$APP_DIR"   # code owned by root, executed (read-only) by the service user

# 3b. let unprivileged ICMP sockets work (ping_host) WITHOUT CAP_NET_RAW.
log "enabling unprivileged ping (net.ipv4.ping_group_range)"
GID="$(id -g "$SERVICE_USER")"
echo "net.ipv4.ping_group_range = ${GID} ${GID}" > /etc/sysctl.d/99-agentic-devops-host-mcp.conf
sysctl -q --system || true

# 4. EnvironmentFile — token 0640 root:devy-hostmcp (readable by the service, not world).
log "writing ${ENV_FILE}"
install -d -m 0755 "$ENV_DIR"
umask 077
cat > "$ENV_FILE" <<EOF
# Managed by install-native-linux.sh — do not edit by hand except to toggle
# enhanced mode (see below). Regenerated on each install.
HOST_MCP_TRANSPORT=http
HOST_MCP_HOST=${HOST_MCP_BIND}
HOST_MCP_PORT=${HOST_MCP_PORT}
HOST_MCP_PROFILE=${HOST_MCP_PROFILE}
HOST_MCP_TOKEN=${HOST_MCP_TOKEN}
HOST_MCP_AUDIT=/var/log/agentic-devops-host-mcp/audit.jsonl
# IMMUTABILITY IS THE DEFAULT. To enable enhanced (mutating) mode, uncomment the
# next line and restart — it is read only at startup and needs a matching OS
# privilege grant (scoped polkit/sudoers) for systemctl verbs to actually run:
# HOST_MCP_ALLOW_MUTATIONS=true
EOF
chown "root:${SERVICE_USER}" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# 5. install the hardened unit + enable.
log "installing systemd unit"
install -m 0644 "$HERE/agentic-devops-host-mcp.service.example" "$UNIT"
systemctl daemon-reload
systemctl enable --now agentic-devops-host-mcp.service

log "done — status:"
systemctl --no-pager --lines=0 status agentic-devops-host-mcp.service || true
echo
log "verify: curl -sS -H \"Authorization: Bearer \$HOST_MCP_TOKEN\" http://127.0.0.1:${HOST_MCP_PORT}/mcp  (from the host)"
