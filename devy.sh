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
#                                then bring the DB to head (`db migrate`)
#   ./devy.sh migrate [args]     run schema migrations now (e.g. migrate --status)
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
# Flags (LOCAL dev only — the AWS deploy is the CD pipeline's job, driven by the
# self-contained docker-compose-aws.yml via Devy's `deploy/` role, NOT this wrapper):
#   default         docker-compose-local.yml + SSO overlay; LocalStack for secrets/S3
#   --no-auth       local base only, no SSO edge (password-mode bootstrap / break-glass)
#   --no-migrate    skip the automatic `db migrate` after `up`
set -euo pipefail
cd "$(dirname "$0")"   # always run from repo root so `.env` auto-loads

AUTH=1
MIGRATE=1

# Leading flags may precede the subcommand.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-auth) AUTH=0; shift ;;
    --no-migrate) MIGRATE=0; shift ;;
    *) break ;;
  esac
done

FILES=(-f docker-compose-local.yml)
[[ $AUTH == 1 ]] && FILES+=(-f docker-compose.auth.yml)

banner() { echo "▸ devy [auth=$([[ $AUTH == 1 ]] && echo sso || echo none)] :: docker compose ${FILES[*]}" >&2; }
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

# Auth-plane health. `authenticated:false` alone is normal (password mode, or nobody
# signed in), so it is NOT reported as a fault; a non-null identity_error is, because
# it means a credential arrived and could not be verified — the state that otherwise
# looks exactly like a healthy signed-out app. Best-effort: never fails `doctor`.
auth_check() {
  local body
  body="$(curl -fsS --max-time 3 http://localhost:8080/v1/whoami 2>/dev/null)" || {
    echo "auth: could not reach /v1/whoami on :8080 (stack down, or the edge is not up)"; return 0; }
  local mode auth err
  mode="$(printf '%s' "$body"  | sed -n 's/.*"mode"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  auth="$(printf '%s' "$body"  | sed -n 's/.*"authenticated"[[:space:]]*:[[:space:]]*\([a-z]*\).*/\1/p')"
  err="$(printf '%s'  "$body"  | sed -n 's/.*"identity_error"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  echo "auth: mode=${mode:-?} authenticated=${auth:-?}${err:+ identity_error=$err}"
  case "$err" in
    expired) echo "⚠  a credential reached Devy but had EXPIRED — users are silently anonymous.
   Check OAUTH2_PROXY_COOKIE_REFRESH is set (55m), then sign out and back in." >&2 ;;
    missing_token) echo "⚠  jwt mode but the edge forwarded no bearer — check
   OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER and nginx's Authorization pass-through." >&2 ;;
    invalid_issuer) echo "⚠  issuer rejected — auth.issuer must list BOTH Google forms." >&2 ;;
    invalid_audience) echo "⚠  audience rejected — auth.audience must equal OAUTH2_PROXY_CLIENT_ID." >&2 ;;
    ?*) echo "⚠  identity failed to verify ($err)." >&2 ;;
  esac
}

confirm() { read -rp "$1 Type 'yes' to proceed: " c; [[ "$c" == yes ]] || { echo "aborted" >&2; exit 1; }; }

# Bring the DB to head via the app-owned migration runner (docs/db-migrations.md).
# Runs INSIDE the proxy container (which has agentic-devops + waited for postgres
# healthy). Non-fatal: a failure warns but never blocks the stack. `up` calls this
# unless --no-migrate; also exposed as `./devy.sh migrate` (extra args pass through,
# e.g. `./devy.sh migrate --status`).
run_migrate() {
  local tries=0
  until dc exec -T proxy agentic-devops db migrate "$@"; do
    tries=$((tries + 1))
    if [[ $tries -ge 3 ]]; then
      echo "⚠  db migrate did not succeed after $tries attempts — run './devy.sh migrate' once the stack is healthy" >&2
      return 0
    fi
    echo "… waiting for the stack to be ready for migrations (attempt $tries) …" >&2
    sleep 3
  done
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  up)
    preflight; banner; dc up -d "$@"
    [[ $MIGRATE == 1 ]] && run_migrate ;;
  migrate)
    banner; run_migrate "$@" ;;
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
    banner; dc ps; echo; preflight; auth_check ;;
  mode)
    banner ;;
  help|-h|--help)
    sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' ;;
  *)
    # Passthrough: ps, exec, images, build, restart, config, pull, stop, start, …
    banner; dc "$cmd" "$@" ;;
esac
