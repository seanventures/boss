#!/usr/bin/env bash
# boss — run one BOSS Community install's lifecycle from any directory.
#
# install.sh puts a small `boss` command on PATH that records the install directory and runs
# this file from that install, so the helper always matches the installed release. Every
# subcommand delegates to the reviewed helper that already does the job (checkup.sh,
# upgrade.sh, uninstall.sh, console_password.py, coding_subscriptions.py, connect_ollama.py);
# this file finds the install, chooses arguments, and writes exported provider keys into .env.
# It never prints a credential and never takes one as an argument.
set -euo pipefail
umask 077

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${BOSS_INSTALL_DIR:-$(cd "$SELF_DIR/../.." && pwd)}"

usage() {
  cat <<'EOF'
boss — manage this BOSS Community install

Usage: boss [--dir PATH] COMMAND [options]

Commands:
  status              Install directory, release, services, and API health
  open                Print the Mission Control address
  password [reset]    Show the console password (terminal only), or rotate it
  doctor [--probe]    Check the install can do its job (runs checkup.sh)
  logs [-f] [SERVICE] Recent service logs (api, worker, web, db)
  connect [WHAT]      Find model keys, subscriptions, and Ollama on this machine and offer
                      to connect each one (one question each, terminal only).
                      WHAT connects one explicitly:
                        keys    save provider keys exported in this shell into .env and
                                restart api and worker (OPENAI_, ANTHROPIC_, GOOGLE_, XAI_,
                                OPENROUTER_API_KEY; values are never printed or taken as
                                arguments)
                        codex   copy the Codex ChatGPT login into BOSS
                        claude  sign Claude Code in inside the BOSS container (terminal)
                        grok    sign Grok Build in inside the BOSS container (terminal)
                        ollama  connect the Ollama on this machine as a BOSS model
                      --connect-found answers yes for everything found; a sign-in that
                      needs a person is still skipped without a terminal.
  upgrade [--to REF]  Back up, then move to the newest release (or REF). Never jumps to
                      main unless you installed from main or pass --to main.
                      Also: --dry-run, --backup-dir PATH, --web, --no-web
  update              Re-apply configuration on the installed release: rerun the installer
                      so newly exported model keys and flags take effect. Does not change
                      the release. Accepts installer flags such as --web or --connect-found.
  uninstall [--purge] Stop BOSS and keep data. --purge also deletes data after a backup and
                      a typed confirmation, then removes the boss command the installer
                      put on PATH for this install. Also: --yes, --dry-run, --backup-dir PATH
  version             Installed release and commit

Environment: BOSS_INSTALL_DIR selects another install.
EOF
}

die() { printf 'boss: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    -h|--help|help) usage; exit 0 ;;
    *) break ;;
  esac
done
[[ $# -gt 0 ]] || { usage; exit 0; }
COMMAND="$1"; shift

INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
[[ -d "$INSTALL_DIR" ]] || die "no BOSS install at $INSTALL_DIR. Install with the one-line installer, or pass --dir."
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
ENV_FILE="$INSTALL_DIR/.env"
COMPOSE_FILE="$INSTALL_DIR/scripts/install/community-compose.yml"

need_install() {
  [[ -f "$COMPOSE_FILE" && -f "$ENV_FILE" ]] ||
    die "$INSTALL_DIR is not an installed BOSS Community checkout (.env or compose file missing)."
}

# .env is data, not shell code.
env_value() { grep -m 1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true; }

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    die "Docker is unavailable to this user. Start Docker, or grant noninteractive sudo access to Docker."
  fi
}

compose() {
  docker_cmd
  local profiles=()
  [[ "$(env_value BOSS_COMMUNITY_WEB_ENABLED)" == 0 ]] || profiles+=(--profile web)
  [[ "$(env_value BOSS_COMMUNITY_SECRET_STORE)" != infisical ]] || profiles+=(--profile secrets)
  COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-boss-community}" \
    "${DOCKER[@]}" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ${profiles[@]+"${profiles[@]}"} "$@"
}

api_url() {
  local bind port
  bind="$(env_value BOSS_API_BIND)"; port="$(env_value BOSS_API_PORT)"; port="${port:-8000}"
  case "$bind" in
    ""|127.0.0.1|0.0.0.0) printf 'http://127.0.0.1:%s\n' "$port" ;;
    \[*) printf 'http://%s:%s\n' "$bind" "$port" ;;
    *:*) printf 'http://[%s]:%s\n' "$bind" "$port" ;;
    *) printf 'http://%s:%s\n' "$bind" "$port" ;;
  esac
}

release_line() {
  local ref channel commit
  ref="$(grep -m 1 '^BOSS_RELEASE_REF=' "$INSTALL_DIR/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
  channel="$(grep -m 1 '^BOSS_RELEASE_CHANNEL=' "$INSTALL_DIR/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
  commit="$(git -C "$INSTALL_DIR" rev-parse --short=12 HEAD 2>/dev/null ||
            head -c 12 "$INSTALL_DIR/.boss-shipped-commit" 2>/dev/null || echo unknown)"
  if [[ -n "$ref" ]]; then
    printf '%s (%s channel, commit %s)\n' "$ref" "${channel:-unknown}" "$commit"
  else
    printf 'not recorded (installed before releases were recorded; boss update records it), commit %s\n' "$commit"
  fi
}

cmd_version() {
  printf 'Install   %s\nRelease   %s\n' "$INSTALL_DIR" "$(release_line)"
}

cmd_open() {
  need_install
  [[ "$(env_value BOSS_COMMUNITY_WEB_ENABLED)" != 0 ]] ||
    die "Mission Control is not enabled on this install. Enable it with: boss update --web"
  local port origin
  port="$(env_value BOSS_WEB_PORT)"; origin="$(env_value BOSS_CONSOLE_PUBLIC_ORIGIN)"
  printf 'Mission Control on this machine: http://127.0.0.1:%s\n' "${port:-3000}"
  [[ -z "$origin" ]] || printf 'Console origin:                  %s\n' "$origin"
  printf 'Sign in with the password from: boss password\n'
}

cmd_status() {
  need_install
  cmd_version
  local url code
  url="$(api_url)"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url/health" 2>/dev/null || true)"
  if [[ "$code" == 200 ]]; then
    printf 'API       ✓ healthy at %s\n' "$url"
  else
    printf 'API       ✗ not answering at %s (HTTP %s). Try: boss logs api\n' "$url" "${code:-000}"
  fi
  printf '\nServices\n'
  compose ps </dev/null
  [[ "$code" == 200 ]]
}

cmd_password() {
  need_install
  local action="${1:-show}"
  case "$action" in show|reset) ;; *) die "password takes show or reset" ;; esac
  python3 "$INSTALL_DIR/scripts/install/console_password.py" "$action" --env-file "$ENV_FILE"
}

cmd_doctor() {
  need_install
  BOSS_ENV_FILE="$ENV_FILE" bash "$INSTALL_DIR/scripts/install/checkup.sh" "$@"
}

cmd_logs() {
  need_install
  local args=(logs --tail 200)
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -f|--follow) args+=(-f); shift ;;
      -*) die "unknown logs option: $1" ;;
      *) [[ "$1" =~ ^[a-z][a-z0-9-]*$ ]] || die "not a service name: $1"; args+=("$1"); shift ;;
    esac
  done
  compose "${args[@]}" </dev/null
}

cmd_upgrade() {
  need_install
  local args=(--dir "$INSTALL_DIR")
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --to|--ref) args+=(--ref "${2:?--to needs a ref}"); shift 2 ;;
      --backup-dir) args+=(--backup-dir "${2:?--backup-dir needs a path}"); shift 2 ;;
      --dry-run|--web|--no-web) args+=("$1"); shift ;;
      *) die "unknown upgrade option: $1 (see boss help)" ;;
    esac
  done
  bash "$INSTALL_DIR/scripts/install/upgrade.sh" "${args[@]}"
}

cmd_update() {
  need_install
  [[ -f "$INSTALL_DIR/install.sh" ]] || die "$INSTALL_DIR/install.sh is missing."
  local arg
  for arg in "$@"; do
    case "$arg" in
      --ref|--ref=*|--dir|--dir=*) die "update stays on the installed release. To change release use: boss upgrade --to REF" ;;
    esac
  done
  # The installer reads the remembered release from .boss-release, so this cannot move to main.
  bash "$INSTALL_DIR/install.sh" --dir "$INSTALL_DIR" --yes "$@"
}


# ---- uninstall -------------------------------------------------------------------------------

BOSS_COMMAND_MARKER="# BOSS Community command (generated by install.sh)"

# The launcher install.sh wrote for THIS install, if it is still on disk. Another program's
# `boss`, a symlink, or a launcher that manages another install is never returned.
own_launcher() {
  local candidate raw expected
  expected="$(printf '%q' "$INSTALL_DIR")"
  for candidate in "${BOSS_LAUNCHER:-}" "$(command -v boss 2>/dev/null || true)"; do
    [[ -n "$candidate" && -f "$candidate" && ! -L "$candidate" ]] || continue
    [[ "$(sed -n 2p "$candidate" 2>/dev/null)" == "$BOSS_COMMAND_MARKER" ]] || continue
    raw="$(grep -m 1 '^BOSS_INSTALL_DIR_DEFAULT=' "$candidate" 2>/dev/null | cut -d= -f2- || true)"
    [[ "$raw" == "$expected" ]] || continue
    printf '%s\n' "$candidate"
    return 0
  done
  return 1
}

cmd_uninstall() {
  need_install
  local args=(--dir "$INSTALL_DIR") purge=0 dry=0 launcher=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --purge|--purge-data) args+=(--purge-data); purge=1; shift ;;
      --dry-run) args+=("$1"); dry=1; shift ;;
      --yes|-y|--skip-backup) args+=("$1"); shift ;;
      --backup-dir|--confirm-purge) args+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
      *) die "unknown uninstall option: $1 (see boss help)" ;;
    esac
  done
  [[ "$purge" -eq 0 ]] || launcher="$(own_launcher || true)"
  # uninstall.sh asks for the typed confirmation. A refusal or a failure stops here, so the
  # boss command is removed only after the data purge itself succeeded.
  bash "$INSTALL_DIR/scripts/install/uninstall.sh" "${args[@]}"
  [[ "$purge" -eq 1 && -n "$launcher" ]] || return 0
  if [[ "$dry" -eq 1 ]]; then
    printf 'Would also remove the boss command: %s\n' "$launcher"
    return 0
  fi
  rm -f -- "$launcher"
  printf 'Removed the boss command: %s\n' "$launcher"
}

# ---- connect ---------------------------------------------------------------------------------

have_tty() { [[ -r /dev/tty ]] && (: </dev/tty) 2>/dev/null; }

# Consent is one question per change. Nothing is copied or connected without a yes, or --yes
# together with --connect-found.
ask_yes() {
  local reply
  have_tty || return 1
  printf '%s [y/N] ' "$1" >/dev/tty
  read -r reply </dev/tty || return 1
  [[ "$reply" == y || "$reply" == Y || "$reply" == yes ]]
}

run_subscriptions() {
  local key
  key="$(env_value BOSS_API_KEY)"
  [[ -n "$key" ]] || die "BOSS_API_KEY is missing from $ENV_FILE; rerun the installer."
  (cd "$INSTALL_DIR" && BOSS_API_KEY="$key" python3 scripts/install/coding_subscriptions.py \
     --api-url "$(api_url)" "$@" </dev/null)
}

connect_codex() {
  local assume="$1" args=(--connect-codex)
  if [[ "$(env_value BOSS_COMMUNITY_SECRET_STORE)" != infisical ]]; then
    if [[ "$assume" -eq 1 && "${ACCEPT_DB:-0}" -ne 1 ]]; then
      printf '  skipped   Codex: this install keeps credentials in its database. Add --accept-database-fallback to allow that.\n'
      return 0
    fi
    if [[ "$assume" -eq 0 ]] && ! ask_yes "Copy your Codex ChatGPT login (~/.codex/auth.json) into BOSS's own database on this machine?"; then
      printf '  skipped   Codex (nothing was copied)\n'
      return 0
    fi
    args+=(--accept-database-fallback)
  elif [[ "$assume" -eq 0 ]] && ! ask_yes "Copy your Codex ChatGPT login (~/.codex/auth.json) into this install's secret store?"; then
    printf '  skipped   Codex (nothing was copied)\n'
    return 0
  fi
  run_subscriptions "${args[@]}"
}

PROVIDER_KEYS=(OPENAI_API_KEY ANTHROPIC_API_KEY GOOGLE_API_KEY XAI_API_KEY OPENROUTER_API_KEY)

# Write the provider keys exported in this shell into the install's private .env. Values pass
# through one python process's environment, never argv, and only names are printed. The file is
# replaced atomically with mode 0600. Status: 0 .env changed, 1 nothing to change, 2 error.
save_exported_keys() {
  local report state name changed=1
  report="$(python3 - "$ENV_FILE" "${PROVIDER_KEYS[@]}" <<'PY'
import os
import sys

path, names = sys.argv[1], sys.argv[2:]
with open(path, encoding="utf-8") as stream:
    lines = stream.read().splitlines()
changed = False
for name in names:
    value = os.environ.get(name, "").strip()
    if not value:
        continue
    if any(char in value for char in "\r\n\0"):
        print(f"refused {name}")
        continue
    wanted = f"{name}={value}"
    current = [index for index, line in enumerate(lines) if line.startswith(name + "=")]
    if [lines[index] for index in current] == [wanted]:
        print(f"unchanged {name}")
        continue
    print(f"{'updated' if current else 'saved'} {name}")
    if current:
        lines[current[0]] = wanted
        for index in reversed(current[1:]):
            del lines[index]
    else:
        lines.append(wanted)
    changed = True
if changed:
    temporary = f"{path}.writing.{os.getpid()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
PY
)" || { printf '  ✗ Could not update %s; nothing was changed.\n' "$ENV_FILE"; return 2; }
  while read -r state name; do
    case "$state" in
      saved) printf '  saved     %s in .env (value not shown)\n' "$name"; changed=0 ;;
      updated) printf '  updated   %s in .env (value not shown)\n' "$name"; changed=0 ;;
      unchanged) printf '  kept      %s (already in .env)\n' "$name" ;;
      refused) printf '  ✗ %s contains a line break and was not saved\n' "$name" ;;
    esac
  done <<<"$report"
  return "$changed"
}

# Recreate only api and worker so they read the new .env. The database and console keep running.
apply_keys() {
  local url i
  printf 'Restarting api and worker so they read the saved keys...\n'
  if ! compose up -d --no-deps --no-build --force-recreate api worker </dev/null; then
    printf '  ✗ api and worker were not restarted. The keys are saved; apply them with: boss update\n'
    return 1
  fi
  url="$(api_url)"
  for i in $(seq 1 40); do
    if curl -fsS --max-time 5 "$url/health" >/dev/null 2>&1; then
      printf '  ✓ Keys applied; API healthy at %s\n' "$url"
      return 0
    fi
    [[ "$i" -eq 40 ]] || sleep 3
  done
  printf '  ✗ API did not answer after the restart. See: boss logs api\n'
  return 1
}

connect_keys() {
  local rc=0 name exported=0
  for name in "${PROVIDER_KEYS[@]}"; do
    [[ -z "${!name:-}" ]] || exported=1
  done
  if [[ "$exported" -eq 0 ]]; then
    printf '  none      No provider key is exported in this shell (%s).\n' "${PROVIDER_KEYS[*]}"
    return 0
  fi
  save_exported_keys || rc=$?
  case "$rc" in
    0) apply_keys ;;
    1) printf '  nothing   .env already holds these keys; no restart needed\n' ;;
    *) return 1 ;;
  esac
}

claude_blocked() {
  if [[ -n "$(env_value ANTHROPIC_API_KEY)" ]]; then
    printf '  ✗ Claude Code: .env holds ANTHROPIC_API_KEY, which would be used instead of the subscription. Remove that line from %s, run boss update, then: boss connect claude\n' "$ENV_FILE"
    return 0
  fi
  if [[ -n "$(env_value CLAUDE_CODE_OAUTH_TOKEN)" ]]; then
    printf '  ✗ Claude Code: .env holds a legacy CLAUDE_CODE_OAUTH_TOKEN. Remove that line from %s, run boss update, then: boss connect claude\n' "$ENV_FILE"
    return 0
  fi
  return 1
}

# 0 signed in inside the API container, 3 not signed in, 4 BOSS cannot say (for example, down).
container_login_state() {
  local rc=0
  run_subscriptions --local-status "$1" >/dev/null 2>&1 || rc=$?
  return "$rc"
}

# Claude Code and Grok sign in inside the BOSS container, as the boss user, through the
# provider's own flow. No host login file or token passes through BOSS, and nothing is printed
# from the session. Status: 0 connected, 1 failed, 2 skipped (needs a person at a terminal).
sign_in() {
  local name="$1" label flag state=0
  local login=()
  case "$name" in
    claude) label="Claude Code"; flag=--connect-claude-local; login=(claude auth login) ;;
    grok) label="Grok Build"; flag=--connect-grok-local; login=(grok login --device-auth) ;;
  esac
  if [[ "$name" == claude ]] && claude_blocked; then
    return 1
  fi
  container_login_state "$name" || state=$?
  if [[ "$state" -ne 0 ]]; then
    if ! have_tty; then
      printf '  skipped   %s sign-in needs a person at a terminal. Run: boss connect %s\n' "$label" "$name"
      return 2
    fi
    printf 'Signing %s in inside the BOSS API container. %s runs its own login; nothing is copied from this machine.\n' "$label" "$label"
    if ! compose exec -u boss api "${login[@]}" </dev/tty >/dev/tty; then
      printf '  ✗ %s sign-in did not finish; nothing was recorded. Retry: boss connect %s\n' "$label" "$name"
      return 1
    fi
  else
    printf '  %s is already signed in inside BOSS; recording it.\n' "$label"
  fi
  if ! run_subscriptions "$flag"; then
    printf '  ✗ %s was not recorded; the reason is above. Retry: boss connect %s\n' "$label" "$name"
    return 1
  fi
  printf '  ✓ %s signed in inside BOSS and recorded (model use is verified by the first governed job)\n' "$label"
}

# A login found on this machine: one question, then the in-container sign-in. Already signed in
# inside BOSS means nothing to ask. $2 is 1 when --connect-found (or --yes) answered for the user.
offer_sign_in() {
  local name="$1" assume="$2" label state=0
  case "$name" in claude) label="Claude Code" ;; grok) label="Grok Build" ;; esac
  if [[ "$name" == claude ]] && claude_blocked; then
    return 1
  fi
  container_login_state "$name" || state=$?
  if [[ "$state" -eq 0 ]]; then
    printf '  ✓ %s is already signed in inside BOSS\n' "$label"
    return 0
  fi
  if ! have_tty; then
    printf '  next      %s: signing in needs a person at a terminal. Run: boss connect %s\n' "$label" "$name"
    return 2
  fi
  if [[ "$assume" -ne 1 ]] &&
     ! ask_yes "Sign $label in inside the BOSS container now? $label runs its own login there; nothing is copied from this machine."; then
    printf '  skipped   %s. Later: boss connect %s\n' "$label" "$name"
    return 2
  fi
  sign_in "$name"
}

# Ollama on this machine, reached from the containers as host.docker.internal (Compose maps it
# with host-gateway; nothing is published). Registered through POST /v1/setup/model, the same
# route as Mission Control's "Anything OpenAI-compatible" form. Status: 0 connected, 1 failed,
# 2 skipped by the user.
connect_ollama() {
  local assume="$1" host_url="${BOSS_OLLAMA_URL:-http://127.0.0.1:11434}" port container key
  if [[ "$host_url" =~ ^http://(127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})/?$ ]]; then
    port="${BASH_REMATCH[2]}"
  else
    printf '  ✗ BOSS_OLLAMA_URL must be http://127.0.0.1:PORT on this machine; nothing was changed.\n'
    return 1
  fi
  container="http://host.docker.internal:$port"
  if [[ "$assume" -ne 1 ]] &&
     ! ask_yes "Connect the Ollama on this machine as a BOSS model? The BOSS containers reach it at $container; no port is opened."; then
    printf '  skipped   Ollama (nothing was changed). Later: boss connect ollama\n'
    return 2
  fi
  if ! compose exec -T api python -c \
       'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5).read(1)' \
       "$container/api/tags" </dev/null >/dev/null 2>&1; then
    printf '  ✗ The BOSS API container cannot reach Ollama at %s. On Linux, Ollama listens on 127.0.0.1 only unless configured otherwise; see docs/community/MODEL_CONNECTIONS.md. Nothing was changed.\n' "$container"
    return 1
  fi
  key="$(env_value BOSS_API_KEY)"
  [[ -n "$key" ]] || { printf '  ✗ BOSS_API_KEY is missing from %s; rerun the installer.\n' "$ENV_FILE"; return 1; }
  BOSS_API_KEY="$key" BOSS_OLLAMA_URL="$host_url" python3 \
    "$INSTALL_DIR/scripts/install/connect_ollama.py" --api-url "$(api_url)" \
    --container-url "$container/v1" </dev/null
}

cmd_connect() {
  local what="" assume=0 offer=0 summary key
  ACCEPT_DB=0
  need_install
  while [[ $# -gt 0 ]]; do
    case "$1" in
      codex|claude|grok|ollama|keys) what="$1"; shift ;;
      --yes|-y|--connect-found) assume=1; shift ;;
      --accept-database-fallback) ACCEPT_DB=1; shift ;;
      --offer) offer=1; shift ;;  # the installer: ask first, as for a found login
      *) die "connect does not take that argument (see boss help). Model keys are read from the environment, never from arguments, so the argument is not repeated here." ;;
    esac
  done
  case "$what" in
    codex) connect_codex "$assume"; return ;;
    keys) connect_keys; return ;;
    ollama) connect_ollama "$([[ "$offer" -eq 1 ]] && echo "$assume" || echo 1)"; return ;;
    claude|grok)
      if [[ "$offer" -eq 1 ]]; then
        offer_sign_in "$what" "$assume"
      else
        sign_in "$what"
      fi
      return ;;
  esac
  printf 'Looking for model keys and subscriptions on this machine (read only)...\n'
  summary="$(mktemp)"
  trap 'rm -f "$summary"' RETURN
  python3 "$INSTALL_DIR/scripts/install/discover_models.py" --summary "$summary"
  state() { grep -m 1 "^$1=" "$summary" | cut -d= -f2-; }
  for key in openai anthropic google xai openrouter; do
    if [[ "$(state "${key}_key")" == found ]]; then
      connect_keys || true
      break
    fi
  done
  if [[ "$(state codex_login)" == found ]]; then
    connect_codex "$assume" || printf '  ✗ Codex login not connected; the reason is above. Retry: boss connect codex\n'
  fi
  if [[ "$(state claude_login)" == found ]]; then
    offer_sign_in claude "$assume" || true
  fi
  if [[ "$(state grok_login)" == found ]]; then
    offer_sign_in grok "$assume" || true
  fi
  if [[ "$(state ollama)" == found ]]; then
    connect_ollama "$assume" || true
  fi
}

case "$COMMAND" in
  status) cmd_status "$@" ;;
  open) cmd_open "$@" ;;
  password) cmd_password "$@" ;;
  doctor|checkup) cmd_doctor "$@" ;;
  logs) cmd_logs "$@" ;;
  connect) cmd_connect "$@" ;;
  upgrade) cmd_upgrade "$@" ;;
  update) cmd_update "$@" ;;
  uninstall) cmd_uninstall "$@" ;;
  version|--version) cmd_version ;;
  *) printf 'boss: unknown command %s\n\n' "$COMMAND" >&2; usage >&2; exit 2 ;;
esac
