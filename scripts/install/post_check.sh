#!/usr/bin/env bash
# THE INSTALLER'S POST-START CHECK, authenticated with a credential that does not retire.
#
# Usage: post_check.sh --dir INSTALL_DIR [--api-url URL] [--project ID] [--wait SECONDS]
#                      [-- COMPOSE COMMAND...]
#
# It used to be inline in install.sh and sent the shared BOSS_API_KEY. Identity retires that
# key on schedule (sys_legacy_console, LEGACY_CUTOFF_DAYS after its first use; IDENTITY.md §12,
# legacy.py), so 30 days after an install first used it, every upgrade and every rollback died
# here with "Edition probe returned HTTP 401" (authn: legacy_credential_retired), although BOSS
# was running. The retirement is intended; the installer must not depend on it.
#
# The designed machine credential for the host's own tools is the "Host tools" service account
# key (identity/host_tools.py), kept in .env as BOSS_HOST_API_KEY and minted from the host shell
# only (`python -m boss_engine.identity.cli host-key` inside the api container). This check:
#   1. waits for the public liveness route (/health);
#   2. keeps BOSS_HOST_API_KEY when the API accepts it, else issues a new one into .env
#      (only a 401/403 means "issue again", like boss.sh host_key);
#   3. proves the edition with that key (GET /v1/edition must say community).
# Only an API too old to issue a host key falls back to BOSS_API_KEY, while that still works.
#
# install.sh runs it after `compose up`; upgrade.sh runs the NEW release's copy to verify a
# rollback, because the previous release's own installer still probes with the retired key.
# No credential is ever printed or put on a command line (curl reads the header from stdin).
set -uo pipefail

DIR=""
API_URL=""
PROJECT="boss-dogfood"
WAIT=120
COMPOSE=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="${2:?--dir needs a path}"; shift 2 ;;
    --api-url) API_URL="${2:?--api-url needs a URL}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs an id}"; shift 2 ;;
    --wait) WAIT="${2:?--wait needs seconds}"; shift 2 ;;
    --) shift; COMPOSE=("$@"); break ;;
    *) printf 'post_check.sh: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ -n "$DIR" && -f "$DIR/.env" ]] || { echo 'post_check.sh: --dir must name an install with .env' >&2; exit 2; }
ENV_FILE="$DIR/.env"

ok() { printf '  \342\234\223 %s\n' "$1"; }
fail() { printf '  \342\234\227 %s\n' "$1" >&2; exit 1; }

env_value() { grep -m 1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true; }

# Same writer as boss.sh: the value travels in the environment, never argv; the file stays 0600.
set_env_value() {
  BOSS_ENV_VALUE="$2" python3 - "$ENV_FILE" "$1" <<'PY'
import os
import sys

path, name = sys.argv[1], sys.argv[2]
value = os.environ["BOSS_ENV_VALUE"]
with open(path, encoding="utf-8") as stream:
    lines = [line for line in stream.read().splitlines() if not line.startswith(name + "=")]
lines.append(f"{name}={value}")
temporary = f"{path}.writing.{os.getpid()}"
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    stream.write("\n".join(lines) + "\n")
os.replace(temporary, path)
PY
}

if [[ -z "$API_URL" ]]; then
  _bind="$(env_value BOSS_API_BIND)"; _port="$(env_value BOSS_API_PORT)"; _port="${_port:-8000}"
  case "$_bind" in
    ""|127.0.0.1|0.0.0.0) API_URL="http://127.0.0.1:$_port" ;;
    \[*) API_URL="http://$_bind:$_port" ;;
    *:*) API_URL="http://[$_bind]:$_port" ;;
    *) API_URL="http://$_bind:$_port" ;;
  esac
fi

if [[ ${#COMPOSE[@]} -eq 0 ]]; then
  if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then COMPOSE=(docker-compose)
  elif sudo -n docker --host unix:///var/run/docker.sock compose version >/dev/null 2>&1; then
    COMPOSE=(sudo -n docker --host unix:///var/run/docker.sock compose)
  fi
fi

# $1 path, $2 key ("" = none), $3 body file ("" = discard). stdout: the HTTP code (000 = none).
http_code() {
  local out="${3:-/dev/null}" code
  if [[ -n "$2" ]]; then
    code="$(printf 'Authorization: Bearer %s\n' "$2" | curl -sS -o "$out" -w '%{http_code}' \
      --max-time 15 -H @- "${API_URL}$1" 2>/dev/null)"
  else
    code="$(curl -sS -o "$out" -w '%{http_code}' --max-time 15 "${API_URL}$1" 2>/dev/null)"
  fi
  printf '%s' "${code:-000}"
}

# 1. Liveness, on the public route (no credential).
deadline=$(( $(date +%s) + WAIT ))
while :; do
  code="$(http_code /health "")"
  [[ "$code" != 200 ]] || break
  (( $(date +%s) < deadline )) || fail "The API did not answer ${API_URL}/health (HTTP $code)."
  sleep 2
done
ok "API answers /health"

# 2. The host tools' key: keep a working one, else issue one from the host shell.
issue_host_key() {
  [[ ${#COMPOSE[@]} -gt 0 ]] || return 1
  local key
  key="$(cd "$DIR" && "${COMPOSE[@]}" -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
    exec -T -u boss api python -m boss_engine.identity.cli host-key --quiet </dev/null 2>/dev/null | tail -n 1)"
  [[ "$key" == boss_sak_* ]] || return 1
  set_env_value BOSS_HOST_API_KEY "$key" || return 1
  printf '%s' "$key"
}

KEY="$(env_value BOSS_HOST_API_KEY)"
KEY_NAME="host tools key"
if [[ -n "$KEY" ]]; then
  code="$(http_code /v1/identity/me "$KEY")"
  if [[ "$code" == 401 || "$code" == 403 ]]; then KEY=""; fi
fi
if [[ -z "$KEY" ]]; then
  KEY="$(issue_host_key || true)"
  [[ -z "$KEY" ]] || ok "Issued the host tools key (kept in .env as BOSS_HOST_API_KEY)"
fi
if [[ -z "$KEY" ]]; then
  # An API too old to issue one (or no Docker access): the shared key, while it still works.
  KEY="$(env_value BOSS_API_KEY)"
  KEY_NAME="shared BOSS_API_KEY"
fi

# 3. The edition, authenticated.
BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT
code="$(http_code "/v1/edition?project_id=${PROJECT}" "$KEY" "$BODY")"
if [[ "$code" != 200 ]]; then
  if [[ "$KEY_NAME" != "host tools key" ]] && grep -q 'legacy_credential_retired' "$BODY" 2>/dev/null; then
    fail "Edition probe returned HTTP $code: the shared BOSS_API_KEY is retired (by design) and no host tools key could be issued. Run: boss identity host-key"
  fi
  fail "Edition probe returned HTTP $code with the $KEY_NAME."
fi
ok "GET /v1/edition → 200 ($KEY_NAME)"
grep -q '"community"' "$BODY" || fail "Edition response did not report Community."
ok "Edition reports community"
