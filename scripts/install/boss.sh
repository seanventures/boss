#!/usr/bin/env bash
# boss — run one BOSS Community install's lifecycle from any directory.
#
# install.sh puts a small `boss` command on PATH that records the install directory and runs
# this file from that install, so the helper always matches the installed release. Every
# subcommand delegates to the reviewed helper that already does the job (checkup.sh,
# upgrade.sh, uninstall.sh, console_password.py, coding_subscriptions.py); this file only
# finds the install and chooses arguments. It never prints a credential.
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
  connect [WHAT]      Find model keys and subscriptions on this machine and connect them.
                      WHAT is codex, claude, or grok to connect one explicitly.
  upgrade [--to REF]  Back up, then move to the newest release (or REF). Never jumps to
                      main unless you installed from main or pass --to main.
                      Also: --dry-run, --backup-dir PATH, --web, --no-web
  update              Re-apply configuration on the installed release: rerun the installer
                      so newly exported model keys and flags take effect. Does not change
                      the release. Accepts installer flags such as --web or --connect-found.
  uninstall [--purge] Stop BOSS and keep data. --purge also deletes data after a backup and
                      a typed confirmation. Also: --yes, --dry-run, --backup-dir PATH
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

cmd_uninstall() {
  need_install
  local args=(--dir "$INSTALL_DIR")
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --purge|--purge-data) args+=(--purge-data); shift ;;
      --yes|-y|--dry-run|--skip-backup) args+=("$1"); shift ;;
      --backup-dir|--confirm-purge) args+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
      *) die "unknown uninstall option: $1 (see boss help)" ;;
    esac
  done
  bash "$INSTALL_DIR/scripts/install/uninstall.sh" "${args[@]}"
}

# Consent is one question per credential copy. Nothing is copied without a yes, or --yes
# together with --connect-found.
ask_yes() {
  local reply
  [[ -r /dev/tty ]] && (: </dev/tty) 2>/dev/null || return 1
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

# Claude Code and Grok sign in inside the BOSS container, as the boss user, through the
# provider's own flow. No host login file or token passes through BOSS.
connect_in_container() {
  local name="$1" flag="$2"; shift 2
  (: </dev/tty) 2>/dev/null || die "signing $name in needs a terminal. Run: boss connect $name"
  printf 'Signing %s in inside the BOSS API container (the provider opens its own login)...\n' "$name"
  compose exec -u boss api "$@" </dev/tty >/dev/tty || die "$name sign-in did not finish; nothing was recorded."
  run_subscriptions "$flag"
}

cmd_connect() {
  need_install
  local what="" assume=0 summary
  ACCEPT_DB=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      codex|claude|grok) what="$1"; shift ;;
      --yes|-y|--connect-found) assume=1; shift ;;
      --accept-database-fallback) ACCEPT_DB=1; shift ;;
      *) die "unknown connect option: $1 (see boss help)" ;;
    esac
  done
  case "$what" in
    codex) connect_codex "$assume"; return ;;
    claude) connect_in_container claude --connect-claude-local claude auth login; return ;;
    grok) connect_in_container grok --connect-grok-local grok login --device-auth; return ;;
  esac
  printf 'Looking for model keys and subscriptions on this machine (read only)...\n'
  summary="$(mktemp)"
  trap 'rm -f "$summary"' RETURN
  python3 "$INSTALL_DIR/scripts/install/discover_models.py" --summary "$summary"
  state() { grep -m 1 "^$1=" "$summary" | cut -d= -f2-; }
  if [[ "$(state codex_login)" == found ]]; then
    connect_codex "$assume"
  fi
  if [[ "$(state claude_login)" == found ]]; then
    printf '  next      Claude Code: BOSS signs in inside its own container. Run: boss connect claude\n'
  fi
  if [[ "$(state grok_login)" == found ]]; then
    printf '  next      Grok Build: BOSS signs in inside its own container. Run: boss connect grok\n'
  fi
  for key in openai anthropic google xai openrouter; do
    if [[ "$(state "${key}_key")" == found ]]; then
      printf '  next      Exported API keys are saved by: boss update\n'
      break
    fi
  done
  if [[ "$(state ollama)" == found ]]; then
    printf '  next      Ollama: add it in Mission Control as an OpenAI-compatible model (see docs/community/MODEL_CONNECTIONS.md)\n'
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
