#!/usr/bin/env bash
# devy.sh — canonical wrapper for the Devy compose stack.
#
# Assembles the correct compose files + mode env for you, so you never forget the
# SSO overlay. (Running `docker compose up -d` WITHOUT docker-compose.auth.yml drops
# the proxy's OAUTH2_PROXY_CLIENT_ID → the JWT `audience` check fails "Audience
# doesn't match" → login silently breaks. This wrapper prevents that.)
#
# Usage:
#   ./devy.sh up                 start the stack (dev + SSO edge). alias for: up -d
#   ./devy.sh rebuild <svc>      rebuild + restart one service (up -d --build <svc>)
#   ./devy.sh logs [svc]         follow logs
#   ./devy.sh ps                 list services
#   ./devy.sh restart [svc]      restart service(s)
#   ./devy.sh exec <svc> <cmd>   exec into a service
#   ./devy.sh psql               psql into the app DB (agentic)
#   ./devy.sh doctor             ps + a mode/.env preflight
#   ./devy.sh mode               print the active mode + compose files
#   ./devy.sh config|images|build|down|prune|<any docker compose subcommand> …
#
# Modes & flags:
#   dev (default)   base + SSO overlay; LocalStack for secrets/S3
#   --prod          adds docker-compose.prod.yml (real AWS via IAM, no LocalStack,
#                   secure cookies). SCAFFOLD — validated with the Terraform deploy.
#   --dev           force dev (overrides $DEVY_MODE)
#   --no-auth       base only, no SSO edge (password-mode bootstrap / break-glass)
#   $DEVY_MODE      dev|prod (env; a --prod/--dev flag wins)
set -euo pipefail
cd "$(dirname "$0")"   # always run from repo root so `.env` auto-loads

MODE="${DEVY_MODE:-dev}"
AUTH=1

# Leading flags may precede the subcommand.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prod) MODE=prod; shift ;;
    --dev)  MODE=dev;  shift ;;
    --no-auth) AUTH=0; shift ;;
    *) break ;;
  esac
done

FILES=(-f docker-compose.yml)
[[ $AUTH == 1 ]] && FILES+=(-f docker-compose.auth.yml)
if [[ $MODE == prod ]]; then
  [[ -f docker-compose.prod.yml ]] || { echo "✗ prod mode but docker-compose.prod.yml is missing" >&2; exit 1; }
  FILES+=(-f docker-compose.prod.yml)
fi
export DEVY_MODE="$MODE"

banner() { echo "▸ devy [mode=$MODE auth=$([[ $AUTH == 1 ]] && echo sso || echo none)] :: docker compose ${FILES[*]}" >&2; }
dc()     { docker compose "${FILES[@]}" "$@"; }

# Preflight: the .env keys the selected mode needs (catches the silent-break class).
preflight() {
  [[ -f .env ]] || { echo "⚠  no .env in repo root — compose defaults will be used" >&2; return; }
  if [[ $AUTH == 1 ]]; then
    for k in OAUTH2_PROXY_CLIENT_ID OAUTH2_PROXY_CLIENT_SECRET OAUTH2_PROXY_COOKIE_SECRET; do
      grep -q "^$k=" .env || echo "⚠  SSO mode but $k missing from .env — JWT audience/login will fail" >&2
    done
  fi
}

confirm() { read -rp "$1 Type 'yes' to proceed: " c; [[ "$c" == yes ]] || { echo "aborted" >&2; exit 1; }; }

cmd="${1:-help}"; shift || true
case "$cmd" in
  up)
    preflight; banner; dc up -d "$@" ;;
  rebuild)
    [[ $# -gt 0 ]] || { echo "usage: ./devy.sh rebuild <service>" >&2; exit 1; }
    banner; dc up -d --build "$@" ;;
  down)
    # `down -v` drops the postgres volume — all conversation history + the KB.
    if [[ " $* " == *" -v "* || " $* " == *" --volumes "* ]]; then
      confirm "⚠  'down -v' DESTROYS the postgres volume (all history + knowledge base)."
    fi
    banner; dc down "$@" ;;
  prune)
    confirm "⚠  prune stops the stack and removes dangling images."
    banner; dc down --remove-orphans; docker image prune -f ;;
  psql)
    dc exec postgres psql -U agentic -d agentic "$@" ;;
  logs)
    banner; dc logs -f "$@" ;;
  doctor|status)
    banner; dc ps; echo; preflight ;;
  mode)
    banner ;;
  help|-h|--help)
    sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' ;;
  *)
    # Passthrough: ps, exec, images, build, restart, config, pull, stop, start, …
    banner; dc "$cmd" "$@" ;;
esac
