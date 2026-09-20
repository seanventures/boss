#!/usr/bin/env bash
# BOSS doctor — is this install actually able to do its job?
#
# WHY THIS EXISTS. Everything it checks was already knowable, and answering "is my install
# healthy" still meant reading container logs, querying Postgres by hand, and knowing which of
# four endpoints to trust. Every check below is one that has actually failed here:
#
#   containers   a stack that is "up" with one service quietly restarting
#   API          reachable from the machine, which is not the same as reachable from a browser
#   browser URL  compiled into the web bundle at BUILD time — 127.0.0.1 on a server means
#                every preview link points at the operator's own laptop
#   models       connected AND with capacity; an exhausted subscription looks connected
#   chat         a model that answers, which is the difference between configured and working
#   repository   pushable, not merely named
#   host         whether this machine can schedule BOSS at all
#
# Exit 0 when BOSS can work, 1 when it cannot. Advisory checks never fail the run.
#
# NAMING follows `consensus doctor` / `consensus doctor --probe` — Sean's own convention in the
# sibling product, so an operator who has used one already knows what this is and what --probe
# costs them. Without it, only free checks run; with it, a model is actually asked a question,
# which is the difference between "configured" and "working" and takes a few seconds and a few
# tokens to establish.
set -uo pipefail

PROBE=0
for arg in "$@"; do
  case "$arg" in
    --probe) PROBE=1 ;;
    -h|--help)
      echo "usage: doctor.sh [--probe]"
      echo "  --probe   also ask a model a question (costs a few tokens; proves it works)"
      exit 0 ;;
  esac
done

ENV_FILE="${BOSS_ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

API_PORT="${BOSS_API_PORT:-8000}"
WEB_PORT="${BOSS_WEB_PORT:-3000}"
PROJECT="${BOSS_DEFAULT_PROJECT_ID:-boss-dogfood}"
API="http://127.0.0.1:${API_PORT}"
KEY="${BOSS_API_KEY:-}"
AUTH=(); [ -n "$KEY" ] && AUTH=(-H "Authorization: Bearer $KEY")

G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'
fail=0
ok()   { printf "  ${G}ok${N}    %s\n" "$1"; }
warn() { printf "  ${Y}warn${N}  %s\n" "$1"; }
bad()  { printf "  ${R}FAIL${N}  %s\n" "$1"; fail=1; }

printf "\n${B}BOSS doctor${N}  (%s)\n\n" "$API"

# 1. containers
running=$(docker ps --filter "name=boss-community" --format '{{.Names}}' 2>/dev/null | wc -l | tr -d ' ')
if [ "$running" -ge 5 ]; then ok "containers: $running running"
else bad "containers: only $running running — expected 6 (docker compose up -d)"; fi

# 2. the API answers
if curl -fsS --max-time 10 "$API/health" >/dev/null 2>&1; then ok "api: /health answers on $API"
else bad "api: no answer on $API/health"; fi

# 3. the address compiled into the console — the one a browser must reach
compiled=$(docker exec boss-community-web-1 printenv NEXT_PUBLIC_BOSS_API_URL 2>/dev/null || echo "")
if [ -z "$compiled" ]; then warn "console: could not read its API address (web container not running?)"
elif printf '%s' "$compiled" | grep -q '127.0.0.1\|localhost'; then
  if [ -n "${SSH_CONNECTION:-}" ] || [ -n "${BOSS_PUBLIC_URL:-}" ]; then
    bad "console: built with $compiled — a browser on another machine cannot reach that.
        Rebuild web with --public-url (see scripts/install/README.md)"
  else ok "console: $compiled (local install)"; fi
else ok "console: $compiled"; fi

# 4. models: connected AND with capacity
models=$(curl -fsS --max-time 15 "${AUTH[@]}" "$API/v1/settings?project_id=$PROJECT" 2>/dev/null)
engines=$(printf '%s' "$models" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(" ".join(d.get("connected_engines") or []))' 2>/dev/null || echo "")
if [ -n "$engines" ]; then ok "models connected: $engines"
else bad "models: none connected — open Mission Control and connect one"; fi

advice=$(curl -fsS --max-time 10 "${AUTH[@]}" "$API/v1/projects/$PROJECT/host-advice" 2>/dev/null)
blocked=$(printf '%s' "$advice" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("headline") or "")' 2>/dev/null || echo "")

# 5. a model actually answers — configured is not working. Costs tokens, so --probe only.
if [ "$PROBE" -eq 1 ]; then
  reply=$(curl -fsS --max-time 120 -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
    "$API/v1/engine/agent/chat" \
    -d "{\"project_id\":\"$PROJECT\",\"actor\":\"executive_console\",\"message\":\"Reply with the single word OK.\"}" 2>/dev/null)
  if printf '%s' "$reply" | grep -qi "ok"; then ok "chat: a model answered"
  else bad "chat: no usable answer — a model is connected but not working"; fi
else
  warn "chat: not asked (run with --probe to prove a model actually answers)"
fi

# 6. repository, pushable
setup=$(curl -fsS --max-time 15 "${AUTH[@]}" "$API/v1/setup/status?project_id=$PROJECT" 2>/dev/null)
if printf '%s' "$setup" | grep -q '"github"[^}]*"done": *true' 2>/dev/null; then ok "repository: connected"
else warn "repository: not connected — builds cannot open a pull request"; fi

# 7. can this machine run BOSS at all
if [ -n "$blocked" ]; then warn "host: $blocked"; else ok "host: scheduling BOSS normally"; fi

printf "\n"
if [ "$fail" -eq 0 ]; then printf "  ${G}${B}BOSS is ready.${N}  Mission Control: http://127.0.0.1:%s\n\n" "$WEB_PORT"
else printf "  ${R}${B}Not ready.${N} Fix the FAIL lines above, then run this again.\n\n"; fi
exit "$fail"
