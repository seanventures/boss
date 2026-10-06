#!/usr/bin/env bash
# BOSS checkup — is this install actually able to do its job?
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
# NAMED `checkup`, NOT `doctor`. The behaviour is the `consensus doctor` convention and the
# --probe flag is kept for exactly that reason, but "doctor" is now the name several other tools
# use for their own version of this, and a command that means something slightly different in
# every product is a command nobody trusts. This one word is ours. What --probe
# costs them. Without it, only free checks run; with it, a model is actually asked a question,
# which is the difference between "configured" and "working" and takes a few seconds and a few
# tokens to establish.
set -uo pipefail

PROBE=0
for arg in "$@"; do
  case "$arg" in
    --probe) PROBE=1 ;;
    -h|--help)
      echo "usage: checkup.sh [--probe]"
      echo "  --probe   also ask a model a question (costs a few tokens; proves it works)"
      exit 0 ;;
  esac
done

ENV_FILE="${BOSS_ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.env}"
# .env is Docker data, not shell code. A model key or operator-added value may
# contain shell metacharacters; sourcing it would execute them on the host.
# Read the way Docker Compose reads it (its dotenv parser; checked against `docker compose
# config` case by case in apps/api/tests/test_improve_boss_recheck4.py): an `export ` prefix,
# spaces or tabs around `=`, the LAST definition wins; a quoted value ends at its closing quote
# (and may span lines; `\"` escapes one inside double quotes); an unquoted value ends at ` #`
# (a SPACE then `#` — a tab before `#` is part of the value) and loses trailing blanks. A quote
# that never closes makes Compose refuse the whole file, so then no value is read at all and
# env_file_error names the line.
_env_awk='
  function unq(v, q,   out, i, c) {
    if (q == "\047") return v
    out = ""
    for (i = 1; i <= length(v); i++) {
      c = substr(v, i, 1)
      if (c == "\\" && i < length(v)) {
        i++; c = substr(v, i, 1)
        if (c == "n") c = "\n"; else if (c == "t") c = "\t"; else if (c != "\"" && c != "\\") c = "\\" c
      }
      out = out c
    }
    return out
  }
  function close_at(v, q,   i, c) {
    for (i = 1; i <= length(v); i++) {
      c = substr(v, i, 1)
      if (q == "\"" && c == "\\") { i++; continue }
      if (c == q) return i
    }
    return 0
  }
  {
    sub(/\r$/, "")
    if (open) {
      buf = buf "\n" $0
      i = close_at($0, q)
      if (i) { open = 0; buf = substr(buf, 1, length(buf) - length($0) + i - 1); if (name == k) { v = unq(buf, q); found = 1 } }
      next
    }
    line = $0; sub(/^[ \t]+/, "", line)
    if (substr(line, 1, 1) == "#") next
    sub(/^export[ \t]+/, "", line)
    eq = index(line, "="); if (!eq) next
    name = substr(line, 1, eq - 1); sub(/[ \t]+$/, "", name)
    val = substr(line, eq + 1); sub(/^[ \t]+/, "", val)
    q = substr(val, 1, 1)
    if (q == "\"" || q == "\047") {
      rest = substr(val, 2); i = close_at(rest, q)
      if (!i) { open = 1; buf = rest; start = NR; next }
      val = unq(substr(rest, 1, i - 1), q)
    } else {
      i = index(val, " #"); if (i) val = substr(val, 1, i - 1)
      sub(/[ \t]+$/, "", val)
    }
    if (name == k) { v = val; found = 1 }
  }
  END {
    if (open) { if (mode == "error") print start; exit }
    if (mode == "value" && found) print v
  }'
env_value() {
  [ -f "$ENV_FILE" ] || return 0
  awk -v k="$1" -v mode=value "$_env_awk" "$ENV_FILE" 2>/dev/null || true
}
# The line of a quote that never closes (Compose then refuses the whole .env), or nothing.
env_file_error() {
  [ -f "$ENV_FILE" ] || return 0
  awk -v k="" -v mode=error "$_env_awk" "$ENV_FILE" 2>/dev/null || true
}
for key in BOSS_API_PORT BOSS_WEB_PORT BOSS_DEFAULT_PROJECT_ID BOSS_API_KEY BOSS_PUBLIC_URL BOSS_API_BIND; do
  if [ -z "${!key:-}" ]; then
    value="$(env_value "$key")"
    [ -z "$value" ] || printf -v "$key" '%s' "$value"
  fi
done
export BOSS_ENV_FILE="$ENV_FILE"

API_PORT="${BOSS_API_PORT:-8000}"
WEB_PORT="${BOSS_WEB_PORT:-3000}"
PROJECT="${BOSS_DEFAULT_PROJECT_ID:-boss-dogfood}"
# 0.0.0.0 still answers on loopback. A specific --api-bind address does not.
API_BIND="${BOSS_API_BIND:-127.0.0.1}"
case "$API_BIND" in
  ""|127.0.0.1|0.0.0.0) API="http://127.0.0.1:${API_PORT}" ;;
  \[*) API="http://${API_BIND}:${API_PORT}" ;;
  *:*) API="http://[${API_BIND}]:${API_PORT}" ;;
  *) API="http://${API_BIND}:${API_PORT}" ;;
esac
KEY="${BOSS_API_KEY:-}"
AUTH=(); [ -n "$KEY" ] && AUTH=(-H "Authorization: Bearer $KEY")

G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'
fail=0
ok()   { printf "  ${G}ok${N}    %s\n" "$1"; }
warn() { printf "  ${Y}warn${N}  %s\n" "$1"; }
bad()  { printf "  ${R}FAIL${N}  %s\n" "$1"; fail=1; }

printf "\n${B}BOSS checkup${N}  (%s)\n\n" "$API"

# 0. .env itself: a quote that never closes makes Compose refuse the whole file.
_env_bad="$(env_file_error)"
[ -z "$_env_bad" ] || bad ".env line $_env_bad: a quote never closes — Compose refuses the file"

# 1. containers. FOUR are required; web and the bundled secret store are profiles, and a
# healthy install that runs neither is still a healthy install. The first version of this
# check hardcoded "expected 6" and failed a correct install — exactly the kind of wrong
# confident answer a checkup must not give.
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    DOCKER=()
  fi
fi
compiled=""
if [ "${#DOCKER[@]}" -eq 0 ]; then
  bad "containers: Docker is unavailable (start Docker or run this checkup with sudo)"
else
  missing=""
  for svc in db api worker; do
    "${DOCKER[@]}" ps --filter "name=boss-community-${svc}-" --format '{{.Names}}' 2>/dev/null \
      | grep -q . || missing="$missing $svc"
  done
  running=$("${DOCKER[@]}" ps --filter "name=boss-community" --format '{{.Names}}' 2>/dev/null | wc -l | tr -d ' ')
  if [ -z "$missing" ]; then
    ok "containers: $running running (db, api, worker required; web and secrets are optional)"
  else
    bad "containers: missing —$missing  (cd $(dirname "$ENV_FILE") && docker compose -f scripts/install/community-compose.yml --env-file .env up -d)"
  fi
fi

# 2. the API answers
if curl -fsS --max-time 10 "$API/health" >/dev/null 2>&1; then ok "api: /health answers on $API"
else bad "api: no answer on $API/health"; fi

# 3. the address compiled into the console — the one a browser must reach
#
# LABELLED AS WHAT IT IS, WHICH IS NOT THE CONSOLE'S OWN ADDRESS.
#
# This reads NEXT_PUBLIC_BOSS_API_URL: the API address baked into the web bundle. It was
# printed as `console: http://host:8000`, which reads as "the console is there" — so an
# operator opened :8000, got {"detail":"Not Found"} from the API, and reasonably concluded the
# install was broken. It was fine. That is the API port; the console is on WEB_PORT.
if [ "${#DOCKER[@]}" -gt 0 ]; then
  compiled=$("${DOCKER[@]}" exec boss-community-web-1 printenv NEXT_PUBLIC_BOSS_API_URL 2>/dev/null || echo "")
fi
if [ -z "$compiled" ]; then warn "console → API: could not read the address it was built with (web container not running?)"
elif printf '%s' "$compiled" | grep -q '127.0.0.1\|localhost'; then
  if [ -n "${BOSS_PUBLIC_URL:-}" ]; then
    bad "console → API: built with $compiled — a browser on another machine cannot reach that.
        Rebuild web with --public-url (see scripts/install/README.md)"
  elif [ -n "${SSH_CONNECTION:-}" ]; then
    warn "console → API: $compiled uses loopback; forward both console :$WEB_PORT and API :$API_PORT over SSH before opening it in a browser"
  else ok "console → API: $compiled (local install)"; fi
else ok "console → API: $compiled"; fi

# WHERE THE CONSOLE ACTUALLY IS, in the words of whoever is reading this.
#
# Every line below used to say http://127.0.0.1:3000. That is true on the box and false for the
# person reading it over SSH from a laptop, which is how this script is usually run. The host
# is taken from the address the console was built for, because that is already known to be one
# a browser can reach.
console_host=$(printf '%s' "$compiled" | sed -E 's#^[a-z]+://##; s#[:/].*$##')
if [ -n "$console_host" ] && ! printf '%s' "$console_host" | grep -q '127.0.0.1\|localhost'; then
  CONSOLE_URL="http://${console_host}:${WEB_PORT}"
else
  CONSOLE_URL="http://127.0.0.1:${WEB_PORT}"
fi

# 4. models: connected AND with capacity
models=$(curl -fsS --max-time 15 "${AUTH[@]}" "$API/v1/settings?project_id=$PROJECT" 2>/dev/null)
engines=$(printf '%s' "$models" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(" ".join(d.get("connected_engines") or []))' 2>/dev/null || echo "")
if [ -n "$engines" ]; then ok "models connected: $engines"
else
  # NOT A FAILURE ON A FRESH INSTALL — it is the next step, and saying "FAIL" to someone who
  # has just installed correctly teaches them to distrust this output.
  warn "models: none connected yet — open Mission Control ($CONSOLE_URL) and connect one"
fi

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
# Repo and models' own verdict (GET /v1/setup): the repository's last read/write/PR check.
setup=$(curl -fsS --max-time 15 "${AUTH[@]}" "$API/v1/setup?project_id=$PROJECT" 2>/dev/null)
repo_line=$(printf '%s' "$setup" | python3 -c '
import json, sys
try:
    repos = (json.load(sys.stdin).get("repository") or {}).get("repos") or []
except ValueError:
    repos = []
if not repos:
    print("none")
else:
    r = next((x for x in repos if x.get("default")), repos[0])
    p = r.get("pill") or {}
    print(("ok " if p.get("tone") in ("green", "amber", "busy") else "bad ") + str(r.get("full_name")) + " " + str(p.get("label") or ""))
' 2>/dev/null || echo none)
case "$repo_line" in
  ok\ *) ok "repository: ${repo_line#ok }" ;;
  bad\ *) bad "repository: ${repo_line#bad } — fix it in Repo and models" ;;
  *) warn "repository: not connected — builds cannot open a pull request" ;;
esac

# 7. can this machine run BOSS at all
if [ -n "$blocked" ]; then warn "host: $blocked"; else ok "host: scheduling BOSS normally"; fi

# NAMED ACCOUNTS (docs/community/IDENTITY_UPGRADE.md): the first visit to /claim creates the Owner
# account; the console password proves it is you, then works 30 days more as an Operator.
claim=""
if [ "${#DOCKER[@]}" -gt 0 ]; then
  claim=$("${DOCKER[@]}" exec boss-community-api-1 python -m boss_engine.identity.cli claim-status 2>/dev/null | tail -n 1 || echo "")
fi
case "$claim" in
  unclaimed) warn "identity: not claimed yet — open $CONSOLE_URL/claim once to create your Owner account (boss password proves it is you)" ;;
  claimed*) ok "identity: $claim" ;;
  *) : ;;   # an older image without the identity CLI: nothing to say
esac

# 8. configuration drift — how far this install has wandered from a stock one.
#
# ADVISORY, NEVER A FAILURE. Most drift is correct: an install needs its own credentials, its
# own repo, its own caps. The point is that it be VISIBLE. Every "works there, not here" bug
# this install has had was a line in this list that nobody knew was there — a CI timeout of 20
# seconds, a reviewer turn cap, three coding agents on a box that fits one.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if drift=$(python3 "$HERE/drift.py" 2>/dev/null); then
  n=$(printf '%s' "$drift" | sed 's/\x1b\[[0-9;]*m//g' | grep -c '^  ~ ' || true)
  unread=$(printf '%s' "$drift" | sed 's/\x1b\[[0-9;]*m//g' | grep -c '^  ? ' || true)
  if [ "$n" -eq 0 ]; then ok "config: identical to a stock install"
  else ok "config: $n setting(s) differ from stock — scripts/install/drift.py to see which"; fi
  [ "$unread" -gt 0 ] && warn "config: $unread BOSS_ variable(s) set that nothing reads (drift.py names them)"
else
  warn "config: could not compare against a stock install"
fi

# 9. what this install tells the BOSS project (docs/product/RUN_SCORECARDS.md). Said plainly
# every time. The words are the engine's ONE list (improve_boss.TELEMETRY_OFF_WORDS and
# DNT_ON_WORDS); test_the_off_words_are_one_list holds this case to it.
# Quotes are stripped: `BOSS_TELEMETRY="off"` in .env means off, as it does to the engine.
_unq() { tr -d "\"'" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }
_tel="$(env_value BOSS_TELEMETRY | _unq | tr '[:upper:]' '[:lower:]')"
_dnt="$(env_value DO_NOT_TRACK | _unq | tr '[:upper:]' '[:lower:]')"
_url="$(env_value BOSS_TELEMETRY_URL | _unq)"
_off=0
case "$_tel" in off|0|false|no|disabled|disable|none) _off=1 ;; esac
case "$_dnt" in 1|true|yes|on) _off=1 ;; esac
if [ "$_off" = 1 ]; then
  ok "sharing: off (BOSS_TELEMETRY=off or DO_NOT_TRACK=1) — nothing is sent"
elif [ -z "$_url" ]; then
  ok "sharing: no collector in .env — nothing is sent unless one is set in Govern › Sharing"
else
  ok "sharing: daily heartbeat to the collector in .env — opt out in Govern › Sharing or BOSS_TELEMETRY=off"
fi

printf "\n"
if [ "$fail" -eq 0 ]; then printf "  ${G}${B}BOSS is ready.${N}  Mission Control: %s\n\n" "$CONSOLE_URL"
else printf "  ${R}${B}Not ready.${N} Fix the FAIL lines above, then run this again.\n\n"; fi
exit "$fail"
