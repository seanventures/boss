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
  metatron [options]  Open Metatron, the BOSS terminal, on this machine (colours, keys and
                      the terminal's size work natively). Talks to the local API with the
                      host tools' key, never printed. --print 'COMMAND' [--json] runs one
                      command for scripts; --plain is plain text. First run sets up Rich
                      and prompt_toolkit in ~/.local/share/boss/metatron-venv
  password [reset]    Show the console password (terminal only), or rotate it
  doctor [--probe]    Check the install can do its job (runs checkup.sh). --repair lists the
                      known remedy for each failed check; --repair --apply runs the safe ones
                      (start what is stopped, restart a silent API) and checks again
  logs [-f] [SERVICE] Recent service logs (api, worker, web, db)
  connect [WHAT]      The Models step: find model keys, subscriptions, coding CLIs, and
                      Ollama on this machine, offer to connect each one (one question
                      each, terminal only), and when no model is usable offer to sign in
                      to a subscription or set up a free local model. Ends with a table.
                      --list               print what was found, change nothing
                      --summary            print the Models table only
                      --install-ollama     set up a local model end to end: install
                                           Ollama, start it, make it reachable from the
                                           containers, pull the model, connect it
                      --local-model NAME   the model to pull (default qwen2.5-coder:7b)
                      --no-local-model     never offer the local model
                      --rebind-ollama      allow moving an Ollama you already had from
                                           127.0.0.1 to the Docker bridge (Linux)
                      WHAT connects one explicitly:
                        keys    save provider keys exported in this shell into .env and
                                restart api and worker (OPENAI_, ANTHROPIC_, GOOGLE_, XAI_,
                                OPENROUTER_API_KEY; values are never printed or taken as
                                arguments)
                        codex   sign Codex in inside the BOSS container (terminal)
                        claude  sign Claude Code in inside the BOSS container (terminal)
                        grok    sign Grok Build in inside the BOSS container (terminal)
                        ollama  connect the Ollama on this machine as a BOSS model
                      A subscription signs in through the provider's own login inside
                      BOSS: open the link it prints and enter the code (Claude Code shows
                      a code to paste back). Then BOSS proves it with one test turn.
                      Nothing is copied from this machine.
                      --connect-found answers yes for everything found; a sign-in that
                      needs a person is still skipped without a terminal. --yes asks
                      nothing and installs nothing unless --install-ollama is given.
  upgrade [--to REF]  Back up, then move to the newest release (or REF). Never jumps to
                      main unless you installed from main or pass --to main. If the new
                      release cannot start, the previous one is put back (--no-rollback: don't).
                      Also: --dry-run, --backup-dir PATH, --web, --no-web,
                      --channel stable|rc (follow final releases only, or release
                      candidates too; remembered in .boss-release)
  rollback            Put back the release that ran before the last upgrade, from the images
                      upgrade kept (prev-<tag>), in seconds and without a build. Refuses when
                      the upgrade changed the database schema, unless --restore-backup PATH
                      (the backup the upgrade printed) restores the pre-upgrade database first.
                      Also: --dry-run
  self-fix COMMAND    BOSS fixes itself: install the fixes you approved (scripts/install/
                      self_fix_installer.py). seed (mirror BOSS's source for its fixes),
                      enroll (this host's installer key, kept 0600, never printed),
                      watch [--once] (poll for approved installs; --install-service runs
                      it at login, --remove-service stops that), install SFX (run one
                      now), remove SFX / remove-all (run an approved removal), status,
                      plan-upgrade --to REF
  update              Re-apply configuration on the installed release: rerun the installer
                      so newly exported model keys and flags take effect. Does not change
                      the release. Accepts installer flags such as --web or --connect-found.
  uninstall [--purge] Stop BOSS and keep data. --purge also deletes data after a backup and
                      a typed confirmation, then removes the boss command the installer
                      put on PATH for this install. Also: --yes, --dry-run, --backup-dir PATH
  prune [--dedicated] Free disk BOSS used: stopped previews, unused preview images, old BOSS
                      image tags; build cache too above 75% disk on a BOSS-only host
                      (--dedicated). Runs after every upgrade and update. Also: --dry-run
  host-sample         macOS: post this Mac's swap and biggest memory users to BOSS's system
                      doctor, which cannot see past Docker Desktop's VM
  identity CMD        Named accounts, from the host shell:
                        setup-link            a new one-hour link to claim this BOSS (no Owner yet)
                        claim-status          claimed or not, and when the shared password stops
                        recover-owner --email E   a locked-out Owner gets back in (15-minute link)
                        host-key              issue a new key for these host tools (kept in .env)
                        legacy status         the shared credentials still in use, with end dates
                        posture | audit verify | migrate
  toolchains [list]   Build toolchains: which this install has (Python and Node always)
  toolchains enable java|security | disable java|security
                      Turn an opt-in toolchain on or off: saves BOSS_TOOLCHAINS in .env,
                      rebuilds the api and worker images and restarts them. Java adds a
                      pinned Temurin JDK 21, Maven and Gradle (about 0.5 GB); security adds
                      Semgrep, OSV-Scanner and OWASP ZAP on top of Java. See
                      docs/community/TOOLCHAINS.md
  clone PROJECT [DIR] [--branch B] [--open]
                      A working copy of a local project's code (BOSS's git remote,
                      http://localhost:3000/git/PROJECT.git), in DIR (default ~/code/PROJECT).
                      Your personal access token (boss_pat_…: BOSS_TOKEN, the one boss login
                      stored, or asked once on the terminal; never the host tools' key) goes
                      to git's credential helper (the keychain on macOS), never into the URL
                      or .git/config. --open opens it in VS Code (or Finder).
  login [--with-token]
                      Keep your personal access token (make one in Account › Access tokens)
                      for this BOSS: asked hidden on the terminal, or read from stdin with
                      --with-token. BOSS checks it is a person's; it goes to git's credential
                      helper and is never printed.
  review comment N FILE:LINE "TEXT" [--severity must|should|nit]
  review list N | review reply N ID "TEXT" | review resolve N ID
                      Line comments on local change #N, in the same store as the console.
                      Run inside a working copy (the project is read from its remote) or
                      pass --project PROJECT. Signed by a person: the token boss clone or
                      boss login stored (or BOSS_TOKEN); refused without one.
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

# ---- scoped sudo Docker fallback ------------------------------------------
# Keep this small boundary identical in the installer and lifecycle helpers.
# sudo resets selection variables and uses root's Docker configuration.
boss_sudo_scope_safe() {
  _boss_docker_os="$(uname -s)" || return 1
  [ "$_boss_docker_os" = Linux ] || return 1
  [ -z "${DOCKER_HOST:-}${DOCKER_CONTEXT:-}${DOCKER_CONFIG:-}${DOCKER_TLS:-}${DOCKER_TLS_VERIFY:-}${DOCKER_CERT_PATH:-}" ] || return 1
  if [ "${2:-}" != restore ]; then
  [ "${COMPOSE_PROJECT_NAME:-boss-community}" = boss-community ] || return 1
  [ "${BOSS_COMMUNITY_IMAGE_TAG:-boss-community}" = boss-community ] || return 1
  if [ -f "$1" ]; then
    [ -r "$1" ] || return 1
    awk '
      NR == 1 { sub(/^\357\273\277/, "") }
      /^[[:space:]]*(export[[:space:]]+)?(COMPOSE_PROJECT_NAME|BOSS_COMMUNITY_IMAGE_TAG)([[:space:]]|=|:|$)/ {
        if ($0 !~ /^(COMPOSE_PROJECT_NAME|BOSS_COMMUNITY_IMAGE_TAG)=(boss-community)?$/) exit 1
        split($0, field, "="); if (seen[field[1]]++) exit 1
      }
    ' "$1" || return 1
  fi
  fi
  _boss_docker_context="$(docker context show 2>/dev/null)" || return 1
  [ "$_boss_docker_context" = default ] || return 1
  _boss_docker_endpoint="$(docker context inspect default --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" || return 1
  [ "$_boss_docker_endpoint" = unix:///var/run/docker.sock ]
}
# ---- end scoped sudo Docker fallback --------------------------------------

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif boss_sudo_scope_safe "$ENV_FILE" && command -v sudo >/dev/null 2>&1 && sudo -n docker --host unix:///var/run/docker.sock info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker --host unix:///var/run/docker.sock)
  else
    die "Docker is unavailable. Cannot safely use sudo for this Docker selection. Use direct Docker access for custom projects, contexts or connection settings."
  fi
}

compose() {
  docker_cmd
  local profiles=()
  [[ "$(env_value BOSS_COMMUNITY_WEB_ENABLED)" == 0 ]] || profiles+=(--profile web)
  [[ "$(env_value BOSS_COMMUNITY_SECRET_STORE)" != infisical ]] || profiles+=(--profile secrets)
  # The local test inbox runs only on an install that opted in (install.sh --with-mailpit).
  [[ -z "$(env_value BOSS_EMAIL_DEV_INBOX)" ]] || profiles+=(--profile mail)
  # The images are tagged with the compose project name (install.sh); without the same
  # default here Compose looks for boss-community-api:local, which was never built.
  COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-boss-community}" \
  BOSS_COMMUNITY_IMAGE_TAG="${BOSS_COMMUNITY_IMAGE_TAG:-${COMPOSE_PROJECT_NAME:-boss-community}}" \
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

# THE HOST TOOLS' KEY. Before named accounts every host tool used the shared BOSS_API_KEY, which
# loses people-and-access changes when the first Owner claims BOSS and stops working when the
# shared credentials end. These tools now use their own key: service account "Host tools", issued
# from the host shell and kept in .env as BOSS_HOST_API_KEY. A missing or refused key is issued
# again. Never printed, never on a command line (curl reads the header from stdin).
host_key() {
  local key code
  key="$(env_value BOSS_HOST_API_KEY)"
  if [[ -n "$key" ]]; then
    code="$(printf 'Authorization: Bearer %s\n' "$key" | curl -s -o /dev/null -w '%{http_code}' \
      --max-time 5 -H @- "$(api_url)/v1/identity/me" 2>/dev/null || true)"
    # 200, or the API is down or busy: keep it. Only a refusal means "issue a new one".
    [[ "$code" == 401 || "$code" == 403 ]] || { printf '%s' "$key"; return 0; }
  fi
  key="$(issue_host_key 2>/dev/null || true)"
  if [[ "$key" == boss_sak_* ]]; then
    printf '%s' "$key"
    return 0
  fi
  # An API too old to issue one, or no Docker access: the shared key, while it still works.
  env_value BOSS_API_KEY
}

issue_host_key() {
  local key
  key="$(compose exec -T -u boss api python -m boss_engine.identity.cli host-key --quiet </dev/null | tail -n 1)"
  [[ "$key" == boss_sak_* ]] || return 1
  set_env_value BOSS_HOST_API_KEY "$key"
  printf '%s' "$key"
}

release_line() {
  local ref channel commit follow
  ref="$(grep -m 1 '^BOSS_RELEASE_REF=' "$INSTALL_DIR/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
  channel="$(grep -m 1 '^BOSS_RELEASE_CHANNEL=' "$INSTALL_DIR/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
  # BOSSAPP-895: the releases a release install follows (stable | rc), when the operator chose.
  follow="$(grep -m 1 '^BOSS_CHANNEL=' "$INSTALL_DIR/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
  commit="$(git -C "$INSTALL_DIR" rev-parse --short=12 HEAD 2>/dev/null ||
            head -c 12 "$INSTALL_DIR/.boss-shipped-commit" 2>/dev/null || echo unknown)"
  if [[ -n "$ref" ]]; then
    printf '%s (%s channel%s, commit %s)\n' "$ref" "${channel:-unknown}" \
      "${follow:+, follows $follow}" "$commit"
  else
    printf 'not recorded (installed before releases were recorded; boss update records it), commit %s\n' "$commit"
  fi
}

cmd_version() {
  printf 'Install   %s\nRelease   %s\n' "$INSTALL_DIR" "$(release_line)"
}

# What Repo and models says (scripts/install/setup_state.py): `ready`, or the repository line.
setup_state() {
  BOSS_API_URL="$(api_url)" BOSS_API_KEY="$(host_key)" \
    python3 "$INSTALL_DIR/scripts/install/setup_state.py" "$1" 2>/dev/null </dev/null
}

cmd_open() {
  need_install
  [[ "$(env_value BOSS_COMMUNITY_WEB_ENABLED)" != 0 ]] ||
    die "Mission Control is not enabled on this install. Enable it with: boss update --web"
  local port origin path=""
  port="$(env_value BOSS_WEB_PORT)"; origin="$(env_value BOSS_CONSOLE_PUBLIC_ORIGIN)"
  # UNTIL SETUP IS DONE, THE ADDRESS IS REPO AND MODELS: paste a token, connect a model, open a
  # first pull request. Once ready, the console's own front page.
  [[ "$(setup_state ready || true)" == ready ]] || path="/?area=setup"
  if [[ -n "$path" ]]; then
    printf 'Finish setup:                    http://127.0.0.1:%s%s\n' "${port:-3000}" "$path"
  else
    printf 'Mission Control on this machine: http://127.0.0.1:%s\n' "${port:-3000}"
  fi
  [[ -z "$origin" ]] || printf 'Console origin:                  %s%s\n' "${origin%/}" "$path"
  # NOT CLAIMED YET: say how, with the command, so nobody has to find the link in a log.
  case "$(identity_cli claim-status 2>/dev/null | tail -n 1 || true)" in
    unclaimed)
      # BOSSAPP-813: from the server's own network (LAN, tailnet) the console's https address IS
      # the claim — the first visitor sets up the Owner on the site. The setup link is for
      # everywhere else (and for an install with no https origin).
      if [[ -n "$origin" ]]; then
        printf '\nNot claimed yet. Open %s to claim this BOSS (from your own network);\n' "${origin%/}"
        printf 'from elsewhere run, on this server: boss identity setup-link\n'
        printf '(it prints a one-hour link; open it in the browser you will sign in with).\n'
      else
        printf '\nNot claimed yet: nobody owns this BOSS. To become its Owner, run\n\n'
        printf '  boss identity setup-link\n\n'
        printf 'and open the one-hour link it prints, in the browser you will sign in with.\n'
      fi ;;
    *) printf 'Sign in with your own account. Teammates without one yet: boss password, until it stops.\n' ;;
  esac
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
  BOSS_ENV_FILE="$ENV_FILE" BOSS_HOST_API_KEY="$(host_key 2>/dev/null || true)" \
    bash "$INSTALL_DIR/scripts/install/checkup.sh" "$@"
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
      --channel) args+=(--channel "${2:?--channel needs stable or rc}"); shift 2 ;;
      --dry-run|--web|--no-web|--no-rollback) args+=("$1"); shift ;;
      --without) args+=(--without "${2:?--without needs a self-fix id}"); shift 2 ;;
      *) die "unknown upgrade option: $1 (see boss help)" ;;
    esac
  done
  bash "$INSTALL_DIR/scripts/install/upgrade.sh" "${args[@]}"
  # The host tools' own key, ready before the shared key stops (best effort: the next boss
  # command issues it otherwise).
  [[ -n "$(env_value BOSS_HOST_API_KEY)" ]] || issue_host_key >/dev/null 2>&1 || true
  post_deploy_prune
}

# EVERY DEPLOY LEAVES ~1.3 GB BEHIND (build cache, the previous images). Best effort: a prune
# that fails must never fail the update that preceded it.
post_deploy_prune() {
  [[ -x "$INSTALL_DIR/scripts/install/prune.sh" ]] || return 0
  BOSS_DEDICATED_HOST="$(env_value BOSS_DEDICATED_HOST)" \
    bash "$INSTALL_DIR/scripts/install/prune.sh" || printf 'boss: prune skipped\n' >&2
}

# THE MAC UNDER DOCKER DESKTOP, which BOSS cannot see from inside its VM (stability spec §4c):
# swap and the biggest memory users, posted to the system doctor so its card can say "most of
# it is not BOSS" with numbers. macOS only; nothing is posted elsewhere. Run it on a schedule
# (e.g. every 5 min from launchd) to keep the reading fresh; it expires after 10 minutes.
cmd_host_sample() {
  need_install
  [[ "$(uname -s)" == Darwin ]] || die "host-sample reads macOS memory; on Linux BOSS measures the machine itself."
  local key url body
  key="$(host_key)"; [[ -n "$key" ]] || die "no API key for the host tools: run boss identity host-key"
  url="$(api_url)"
  body="$(python3 - <<'PY'
import json, re, subprocess
swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
def mb(name):
    m = re.search(name + r" = ([0-9.]+)M", swap)
    return float(m.group(1)) if m else 0.0
rows = subprocess.run(["ps", "-axo", "rss=,comm="], capture_output=True, text=True).stdout
apps = {}
for line in rows.splitlines():
    parts = line.strip().split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        continue
    name = parts[1].split("/Contents/")[0].rsplit("/", 1)[-1].replace(".app", "")
    name = "Docker" if "com.docker" in parts[1] or "Docker" in name else name
    apps[name] = apps.get(name, 0) + int(parts[0]) / 1024
top = sorted(({"name": k[:40], "mb": round(v)} for k, v in apps.items()), key=lambda r: -r["mb"])[:6]
print(json.dumps({"swap_used_mb": mb("used"), "swap_total_mb": mb("total"), "top": top}))
PY
)"
  printf 'Authorization: Bearer %s\n' "$key" | curl -fsS -m 10 -X POST "$url/v1/self/host-sample" -H @- \
    -H 'Content-Type: application/json' --data "$body" >/dev/null &&
    printf 'boss: host sample posted (%s)\n' "$(printf '%s' "$body" | head -c 80)…"
}

cmd_rollback() {
  need_install
  [[ -f "$INSTALL_DIR/scripts/install/rollback.sh" ]] ||
    die "this release has no rollback.sh; use: boss upgrade --to REF"
  local args=(--dir "$INSTALL_DIR")
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --restore-backup) args+=(--restore-backup "${2:?--restore-backup needs a path}"); shift 2 ;;
      --dry-run) args+=("$1"); shift ;;
      *) die "unknown rollback option: $1 (see boss help)" ;;
    esac
  done
  bash "$INSTALL_DIR/scripts/install/rollback.sh" "${args[@]}"
}

cmd_prune() {
  need_install
  BOSS_DEDICATED_HOST="$(env_value BOSS_DEDICATED_HOST)" \
    bash "$INSTALL_DIR/scripts/install/prune.sh" "$@"
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
  # The installer builds the release; the local BOSS self-fixes are re-applied on it.
  local stack_ref ref
  stack_ref="$(grep -m 1 '^BOSS_STACK_REF=' "$INSTALL_DIR/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
  ref="$(grep -m 1 '^BOSS_RELEASE_REF=' "$INSTALL_DIR/.boss-release" 2>/dev/null | cut -d= -f2- || true)"
  if [[ -n "$stack_ref" && -n "$ref" && -f "$INSTALL_DIR/scripts/install/self_fix_installer.py" ]]; then
    BOSS_SELF_FIX_API="$(api_url)" python3 "$INSTALL_DIR/scripts/install/self_fix_installer.py" \
      --dir "$INSTALL_DIR" restack --to "$ref" ||
      printf 'The release runs without the local BOSS fixes; BOSS re-ports them.\n' >&2
  fi
  post_deploy_prune
}


# ---- toolchains ------------------------------------------------------------------------------

KNOWN_TOOLCHAINS=(java security)

cmd_toolchains() {
  need_install
  local action="${1:-list}" name="${2:-}" current wanted
  current="$(env_value BOSS_TOOLCHAINS)"; current="${current:-none}"
  case "$action" in
    list|status)
      printf 'Configured in .env: BOSS_TOOLCHAINS=%s\n' "$current"
      printf 'Always installed:   python, node\n'
      printf 'In the running image: '
      compose exec -T api cat /opt/toolchains/manifest.json </dev/null 2>/dev/null \
        || printf '(api not running)\n'
      return 0 ;;
    enable|disable) ;;
    *) die "unknown toolchains action: $action (list, enable NAME, disable NAME)" ;;
  esac
  [[ " ${KNOWN_TOOLCHAINS[*]} " == *" $name "* ]] \
    || die "unknown toolchain: ${name:-(none given)}. Known: ${KNOWN_TOOLCHAINS[*]}"
  if [[ "$action" == enable ]]; then wanted="$name"; else wanted=none; fi
  if [[ "$current" == "$wanted" ]]; then
    printf 'BOSS_TOOLCHAINS is already %s; nothing to change.\n' "$wanted"
    return 0
  fi
  set_env_value BOSS_TOOLCHAINS "$wanted"
  printf 'Saved BOSS_TOOLCHAINS=%s in .env. Rebuilding the api and worker images...\n' "$wanted"
  if ! compose build api worker </dev/null; then
    printf '  ✗ The image build failed. .env says BOSS_TOOLCHAINS=%s; fix the cause and run: boss update\n' "$wanted"
    return 1
  fi
  apply_env "Toolchains ($wanted)"
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
  key="$(host_key)"
  [[ -n "$key" ]] || die "no API key for the host tools: run boss identity host-key"
  (cd "$INSTALL_DIR" && BOSS_API_KEY="$key" python3 scripts/install/coding_subscriptions.py \
     --api-url "$(api_url)" "$@" </dev/null)
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
apply_env() {
  local what="${1:-Settings}" url i
  printf 'Restarting api and worker so they read the updated .env...\n'
  if ! compose up -d --no-deps --no-build --force-recreate api worker </dev/null; then
    printf '  ✗ api and worker were not restarted. The change is saved in .env; apply it with: boss update\n'
    return 1
  fi
  url="$(api_url)"
  for i in $(seq 1 40); do
    if curl -fsS --max-time 5 "$url/health" >/dev/null 2>&1; then
      printf '  ✓ %s applied; API healthy at %s\n' "$what" "$url"
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
  if [[ "$rc" -eq 0 ]] && [[ -n "${OPENAI_API_KEY:-}" ]] &&
     python3 "$INSTALL_DIR/scripts/install/local_model.py" chat-hosted "$ENV_FILE" </dev/null; then
    printf '  Chat and the other OPENAI_* surfaces move from the local Ollama to the OpenAI key\n'
  fi
  [[ "$rc" -ne 0 ]] || release_local_default
  case "$rc" in
    0) apply_env Keys ;;
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

provider_label() {
  case "$1" in codex) printf 'Codex' ;; claude) printf 'Claude Code' ;; grok) printf 'Grok Build' ;; esac
}

# THE ONE SUBSCRIPTION ROUTE (BOSSAPP-769). Codex, Claude Code and Grok Build sign in inside the
# BOSS container, as the boss user, through the provider's own login: coding_subscriptions.py
# --sign-in asks BOSS to start it (POST /v1/setup/sign-in), prints the link and code, waits, and
# then connects and proves it (POST /v1/setup/models). No host login file or token passes
# through BOSS, and nothing from the session is printed. Status: 0 connected and proved,
# 1 failed, 2 skipped (needs a person at a terminal).
sign_in() {
  local name="$1" label state=0 rc=0
  label="$(provider_label "$name")"
  if [[ "$name" == claude ]] && claude_blocked; then
    return 1
  fi
  if ! have_tty; then
    container_login_state "$name" || state=$?
    if [[ "$state" -ne 0 ]]; then
      printf '  skipped   %s sign-in needs a person at a terminal. Run: boss connect %s\n' "$label" "$name"
      return 2
    fi
  fi
  run_subscriptions --sign-in "$name" || rc=$?
  case "$rc" in
    0) printf '  ✓ %s is signed in inside BOSS and connected: it answered one test turn\n' "$label" ;;
    2) return 2 ;;
    *) printf '  ✗ %s was not connected; the reason is above. Retry: boss connect %s\n' "$label" "$name"
       return 1 ;;
  esac
}

# A login found on this machine: one question, then the in-container sign-in. Already signed in
# inside BOSS means nothing to ask. $2 is 1 when --connect-found (or --yes) answered for the user.
offer_sign_in() {
  local name="$1" assume="$2" label state=0
  label="$(provider_label "$name")"
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
  if [[ "$assume" -ne 1 && "${NO_QUESTIONS:-0}" -eq 1 ]]; then
    printf '  next      %s login found, not signed in (an unattended run signs nothing in without --connect-found). Run: boss connect %s\n' "$label" "$name"
    return 2
  fi
  if [[ "$assume" -ne 1 ]] &&
     ! ask_yes "Sign $label in inside the BOSS container now? $label runs its own login there; nothing is copied from this machine."; then
    printf '  skipped   %s. Later: boss connect %s\n' "$label" "$name"
    return 2
  fi
  sign_in "$name"
}

# ---- Ollama: connect one that is running, or set up a local model end to end ------------------

OLLAMA_DEFAULT_MODEL="qwen2.5-coder:7b"
OLLAMA_INSTALLER_URL="https://ollama.com/install.sh"

host_os() { uname -s 2>/dev/null || echo unknown; }

# Root for a system service change: directly as root, through sudo at a terminal, and only
# noninteractive sudo otherwise. Never used for the per-user parts.
as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif have_tty; then
    sudo "$@"
  else
    sudo -n "$@"
  fi
}

# Where Ollama answers on this machine: the environment, then .env (set when BOSS bound Ollama to
# the Docker bridge), then the default loopback address.
ollama_url() {
  local url="${BOSS_OLLAMA_URL:-}"
  [[ -n "$url" ]] || url="$(env_value BOSS_OLLAMA_URL)"
  [[ -n "$url" ]] || url="$(boss_dropin_url || true)"
  printf '%s\n' "${url:-http://127.0.0.1:11434}"
}

# The address an earlier BOSS install bound Ollama to (its own systemd drop-in), so a reinstall
# after a purge finds the Ollama that is still running there. Only BOSS's drop-in is read.
boss_dropin_url() {
  local file hostport
  file="${BOSS_OLLAMA_DROPIN_DIR:-/etc/systemd/system/ollama.service.d}/boss-docker-bridge.conf"
  [[ -r "$file" ]] || return 1
  hostport="$(sed -n 's/^Environment="OLLAMA_HOST=\([0-9.]*:[0-9]*\)"$/\1/p' "$file" | head -n 1)"
  [[ -n "$hostport" ]] || return 1
  printf 'http://%s\n' "$hostport"
}

ollama_port() {
  local url
  url="$(ollama_url)"
  [[ "$url" =~ ^http://[^/]+:([0-9]{1,5})/?$ ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

ollama_hostport() { local url; url="$(ollama_url)"; url="${url#http://}"; printf '%s\n' "${url%/}"; }

# The models Ollama has pulled, one per line; status 1 when no Ollama answers on this machine.
ollama_models() {
  python3 "$INSTALL_DIR/scripts/install/discover_models.py" --ollama-models "$(ollama_url)" \
    </dev/null 2>/dev/null
}

ollama_up() { ollama_models >/dev/null; }

valid_model_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$ ]]; }

gigabytes() { awk -v b="$1" 'BEGIN { printf "%.1f", b / 1073741824 }'; }

# A value that is not a secret (an address) into .env, replacing its line. Atomic, mode 0600.
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

# Can the BOSS API container read Ollama's model list (and see MODEL, when given)?
container_sees_ollama() {
  compose exec -T api python -c '
import json, sys, urllib.request
body = json.load(urllib.request.urlopen(sys.argv[1] + "/api/tags", timeout=5))
names = [m.get("name") for m in body.get("models", []) if isinstance(m, dict)]
sys.exit(0 if len(sys.argv) < 3 or sys.argv[2] in names else 3)' "$@" </dev/null >/dev/null 2>&1
}

install_ollama() {
  local script
  if command -v ollama >/dev/null 2>&1; then
    printf '  ✓ Ollama is installed\n'
    return 0
  fi
  case "$(host_os)" in
    Linux)
      printf 'Installing Ollama with its official installer (%s)...\n' "$OLLAMA_INSTALLER_URL"
      script="$(mktemp)"
      # Download first: a failed or partial download never runs.
      if ! curl -fsSL --proto '=https' --tlsv1.2 "$OLLAMA_INSTALLER_URL" -o "$script" ||
         [[ "$(head -c 2 "$script" 2>/dev/null)" != '#!' ]]; then
        rm -f "$script"
        printf '  ✗ Could not download the Ollama installer from %s. Nothing was installed.\n' "$OLLAMA_INSTALLER_URL"
        return 1
      fi
      if have_tty; then sh "$script" </dev/tty; else sh "$script" </dev/null; fi || {
        rm -f "$script"
        printf '  ✗ The Ollama installer failed; the reason is above. Retry: boss connect --install-ollama\n'
        return 1
      }
      rm -f "$script"
      ;;
    Darwin)
      if ! command -v brew >/dev/null 2>&1; then
        printf '  ✗ Install the Ollama app from https://ollama.com/download and open it once, then run: boss connect --install-ollama\n'
        return 1
      fi
      printf 'Installing Ollama with Homebrew (brew install ollama)...\n'
      brew install ollama </dev/null || {
        printf '  ✗ brew install ollama failed; the reason is above. Retry: boss connect --install-ollama\n'
        return 1
      }
      ;;
    *)
      printf '  ✗ BOSS can set up Ollama on Linux and macOS only. Install it from https://ollama.com/download, then: boss connect ollama\n'
      return 1
      ;;
  esac
  hash -r
  if ! command -v ollama >/dev/null 2>&1; then
    printf '  ✗ Ollama was installed, but the ollama command is not on PATH. Open a new shell, then: boss connect --install-ollama\n'
    return 1
  fi
  printf '  ✓ Ollama installed\n'
}

# Keep a non-default Ollama address in .env so the next command and the containers use it.
remember_ollama_url() {
  local url
  url="$(ollama_url)"
  [[ "$url" != http://127.0.0.1:11434 && -z "$(env_value BOSS_OLLAMA_URL)" && -f "$ENV_FILE" ]] || return 0
  set_env_value BOSS_OLLAMA_URL "$url"
}

start_ollama_serve() {
  mkdir -p "$HOME/.ollama"
  printf '  Running ollama serve in the background (log: ~/.ollama/boss-serve.log). It does not start again after a reboot.\n'
  OLLAMA_HOST="$(ollama_hostport)" nohup ollama serve >"$HOME/.ollama/boss-serve.log" 2>&1 </dev/null &
}

start_ollama() {
  local i
  if ollama_up; then
    printf '  ✓ Ollama is running at %s\n' "$(ollama_url)"
    remember_ollama_url
    return 0
  fi
  printf 'Starting Ollama...\n'
  case "$(host_os)" in
    Linux)
      if command -v systemctl >/dev/null 2>&1 && systemctl cat ollama.service >/dev/null 2>&1; then
        as_root systemctl enable --now ollama.service </dev/null || true
      else
        start_ollama_serve
      fi
      ;;
    Darwin)
      if command -v brew >/dev/null 2>&1 && brew list --formula ollama >/dev/null 2>&1; then
        brew services start ollama </dev/null >/dev/null || true
      elif [[ -d /Applications/Ollama.app ]]; then
        open -a Ollama || true
      else
        start_ollama_serve
      fi
      ;;
    *) start_ollama_serve ;;
  esac
  for i in $(seq 1 30); do
    if ollama_up; then
      printf '  ✓ Ollama is running at %s\n' "$(ollama_url)"
      remember_ollama_url
      return 0
    fi
    [[ "$i" -eq 30 ]] || sleep 1
  done
  printf '  ✗ Ollama did not start. Start it (ollama serve), then: boss connect --install-ollama\n'
  return 1
}

# The containers reach the host as host.docker.internal. Docker Desktop forwards that to the
# host's loopback. Docker Engine on Linux maps it to the Docker bridge gateway, where an Ollama
# listening on 127.0.0.1 cannot be reached. With $1=1 or 2 (the full setup), BOSS then binds the
# Ollama service to the bridge gateway: reachable from this machine and its containers, not from
# the network. Never 0.0.0.0. Without it, the fix is printed and nothing changes.
reach_ollama() {
  local rebind="$1" port container gateway dropin i
  port="$(ollama_port)" || { printf '  ✗ BOSS_OLLAMA_URL must look like http://127.0.0.1:11434\n'; return 1; }
  container="http://host.docker.internal:$port"
  if container_sees_ollama "$container"; then
    return 0
  fi
  if [[ "$rebind" -eq 0 || "$(host_os)" != Linux ]] || ! command -v systemctl >/dev/null 2>&1 ||
     ! systemctl cat ollama.service >/dev/null 2>&1; then
    printf '  ✗ The BOSS containers cannot reach Ollama at %s. Ollama must listen on an address the Docker bridge reaches; on Linux run: boss connect --install-ollama (it binds Ollama to the bridge address). See docs/community/MODEL_CONNECTIONS.md. Nothing was changed.\n' "$container"
    return 1
  fi
  docker_cmd
  gateway="$("${DOCKER[@]}" network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' </dev/null 2>/dev/null || true)"
  if [[ ! "$gateway" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ || "$gateway" == 0.0.0.0 ]]; then
    printf '  ✗ Could not read the Docker bridge address. Set OLLAMA_HOST=<bridge address>:%s for the Ollama service, then: boss connect ollama\n' "$port"
    return 1
  fi
  dropin="${BOSS_OLLAMA_DROPIN_DIR:-/etc/systemd/system/ollama.service.d}"
  # $1=2: this Ollama was here before this run. Other programs may use it on 127.0.0.1, so
  # moving it needs a yes at a terminal (default no) or --rebind-ollama.
  if [[ "$rebind" -eq 2 && "$REBIND_OLLAMA" -ne 1 ]] &&
     ! ask "Your Ollama already runs on 127.0.0.1, which the BOSS containers cannot reach. Move it to the Docker bridge address $gateway:$port? Programs that use 127.0.0.1:$port would then need OLLAMA_HOST=$gateway:$port."; then
    printf '  ✗ Your existing Ollama was left on 127.0.0.1, so the BOSS containers cannot reach it. Nothing was changed. To move it: boss connect --install-ollama --rebind-ollama (or set OLLAMA_HOST=%s:%s for the service yourself, then: boss connect ollama)\n' "$gateway" "$port"
    return 1
  fi
  printf 'Ollama listens on 127.0.0.1, which containers cannot reach on Linux. Binding it to the Docker bridge address %s:%s: this machine and its containers reach it, your network does not.\n' "$gateway" "$port"
  if ! as_root mkdir -p "$dropin" </dev/null ||
     ! printf '[Unit]\nAfter=docker.service\n\n[Service]\nEnvironment="OLLAMA_HOST=%s:%s"\n' "$gateway" "$port" |
         as_root tee "$dropin/boss-docker-bridge.conf" >/dev/null ||
     ! as_root systemctl daemon-reload </dev/null || ! as_root systemctl restart ollama.service </dev/null; then
    printf '  ✗ Could not change the Ollama service (this needs sudo). Set OLLAMA_HOST=%s:%s for it yourself, then: boss connect ollama\n' "$gateway" "$port"
    return 1
  fi
  set_env_value BOSS_OLLAMA_URL "http://$gateway:$port"
  export BOSS_OLLAMA_URL="http://$gateway:$port"
  printf '  Ollama now listens on %s:%s (%s/boss-docker-bridge.conf). For the ollama command use: export OLLAMA_HOST=%s:%s\n' "$gateway" "$port" "$dropin" "$gateway" "$port"
  for i in $(seq 1 30); do
    if ollama_up && container_sees_ollama "$container"; then
      return 0
    fi
    [[ "$i" -eq 30 ]] || sleep 1
  done
  printf '  ✗ The BOSS containers still cannot reach Ollama at %s. Check: systemctl status ollama, then: boss connect ollama\n' "$container"
  return 1
}

pull_local_model() {
  local model="$1" bytes dir free_kb need_kb
  if ollama_models | grep -qxF -- "$model"; then
    printf '  ✓ %s is already pulled\n' "$model"
    return 0
  fi
  bytes="$(python3 "$INSTALL_DIR/scripts/install/local_model.py" size "$model" </dev/null 2>/dev/null || echo unknown)"
  dir="${OLLAMA_MODELS:-}"
  if [[ -z "$dir" ]]; then
    if [[ -d /usr/share/ollama ]]; then dir=/usr/share/ollama; else dir="$HOME"; fi
  fi
  free_kb="$(df -Pk "$dir" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [[ "$bytes" =~ ^[0-9]+$ ]]; then
    need_kb=$(( bytes / 1024 + 2 * 1024 * 1024 ))
    printf 'Model %s: about %s GB to download.\n' "$model" "$(gigabytes "$bytes")"
  else
    need_kb=$(( 10 * 1024 * 1024 ))
    printf 'Model %s: the Ollama registry did not say how large it is, so BOSS asks for 10 GB free.\n' "$model"
  fi
  if [[ "$free_kb" =~ ^[0-9]+$ ]]; then
    printf 'Disk: %s GB free in %s, %s GB needed.\n' "$(gigabytes $(( free_kb * 1024 )))" "$dir" "$(gigabytes $(( need_kb * 1024 )))"
    if (( free_kb < need_kb )); then
      printf '  ✗ Not enough disk space; nothing was downloaded. Free space, or choose a smaller model: boss connect --install-ollama --local-model NAME\n'
      return 1
    fi
  else
    printf 'Disk: could not read free space in %s; pulling anyway.\n' "$dir"
  fi
  printf 'Pulling %s with ollama pull...\n' "$model"
  local pull_ok=0
  if [[ -t 1 ]]; then
    OLLAMA_HOST="$(ollama_hostport)" ollama pull "$model" </dev/null && pull_ok=1
  else
    # Not a terminal (a log, CI, a pipe): the progress spinner would fill it with escape codes.
    local pull_log
    pull_log="$(mktemp)"
    if OLLAMA_HOST="$(ollama_hostport)" ollama pull "$model" </dev/null >"$pull_log" 2>&1; then
      pull_ok=1
    else
      tail -n 5 "$pull_log" | sed 's/\x1b\[[0-9;?]*[A-Za-z]//g'
    fi
    rm -f "$pull_log"
  fi
  if [[ "$pull_ok" -ne 1 ]]; then
    printf '  ✗ ollama pull %s failed; the reason is above. Retry: boss connect --install-ollama --local-model %s\n' "$model" "$model"
    return 1
  fi
  printf '  ✓ %s pulled\n' "$model"
}

# Register through POST /v1/setup/model, make it every role's model when no hosted model is
# connected, price it at zero, and check from the API container that the model is there.
# Container reachability must already be established.
register_ollama() {
  local model="$1" key port container result result_copy rc=0 args
  port="$(ollama_port)" || return 1
  container="http://host.docker.internal:$port"
  key="$(host_key)"
  [[ -n "$key" ]] || { printf '  ✗ no API key for the host tools; run boss identity host-key\n'; return 1; }
  result="$(mktemp)"
  args=(--api-url "$(api_url)" --container-url "$container/v1" --default-if-no-hosted --env-file "$ENV_FILE")
  [[ -z "$model" ]] || args+=(--model "$model")
  BOSS_MODELS_RESULT="$result" BOSS_API_KEY="$key" BOSS_OLLAMA_URL="$(ollama_url)" \
    python3 "$INSTALL_DIR/scripts/install/connect_ollama.py" "${args[@]}" </dev/null || rc=$?
  model="$(grep -m 1 '^ollama_model=' "$result" | cut -d= -f2- || true)"
  result_copy="$(cat "$result")"
  [[ -z "${BOSS_MODELS_RESULT:-}" ]] || cat "$result" >> "$BOSS_MODELS_RESULT"
  rm -f "$result"
  [[ "$rc" -eq 0 && -n "$model" ]] || return 1
  local hosted restart=0
  hosted="$(printf '%s\n' "$result_copy" | grep -m 1 '^hosted=' | cut -d= -f2- || true)"
  rc=0
  python3 "$INSTALL_DIR/scripts/install/local_model.py" price-zero "$model" "$container/v1" "$ENV_FILE" </dev/null || rc=$?
  case "$rc" in
    0) printf '  Priced %s at zero for %s/v1 only (BOSS_MODEL_PRICES_JSON in .env)\n' "$model" "$container"; restart=1 ;;
    3) ;;
    *) printf '  ✗ %s was connected but not priced at zero; BOSS counts it at its default rate.\n' "$model" ;;
  esac
  # Nothing hosted: chat and the other surfaces that read OPENAI_* use the local model too.
  if [[ "$hosted" == no ]]; then
    rc=0
    python3 "$INSTALL_DIR/scripts/install/local_model.py" chat-local "$model" "$container/v1" "$ENV_FILE" </dev/null || rc=$?
    case "$rc" in
      0) printf '  Chat and the other OPENAI_* surfaces now use %s too (no hosted model is connected)\n' "$model"; restart=1 ;;
      3) ;;
      4) printf '  Chat keeps the OpenAI key in .env\n' ;;
      *) printf '  ✗ Chat was not pointed at %s; the reason is above.\n' "$model" ;;
    esac
  fi
  if [[ "$restart" -eq 1 ]]; then
    apply_env "Local model" || return 1
  fi
  if container_sees_ollama "$container" "$model"; then
    printf '  ✓ Checked: the BOSS API container sees %s in Ollama. No completion was run: Community setup makes no model calls; the first governed job verifies it.\n' "$model"
  else
    printf '  ✗ The BOSS API container does not see %s in Ollama. Check: ollama list, then: boss connect ollama\n' "$model"
    return 1
  fi
}

# A hosted model is connected: the roles the Ollama fallback set go back to their defaults.
release_local_default() {
  local key
  key="$(host_key)"
  [[ -n "$key" ]] || return 0
  BOSS_API_KEY="$key" python3 "$INSTALL_DIR/scripts/install/connect_ollama.py" --release-default \
    --api-url "$(api_url)" --env-file "$ENV_FILE" </dev/null || true
}

# A running Ollama, found on this machine. Status: 0 connected, 1 failed, 2 skipped.
connect_ollama() {
  local assume="$1" model="${2:-}"
  if [[ "$assume" -ne 1 ]] &&
     ! ask "Connect the Ollama on this machine as a BOSS model? The BOSS containers reach it as host.docker.internal; no port is opened."; then
    printf '  skipped   Ollama (nothing was changed). Later: boss connect ollama\n'
    return 2
  fi
  reach_ollama 0 || return 1
  register_ollama "$model"
}

# The whole local setup: install, start, reach from the containers, pull, register, check.
setup_local_model() {
  local model="$1" existing=1
  printf 'Setting up a local model: Ollama with %s\n' "$model"
  # An Ollama that was already installed or running belongs to the user: moving it needs consent.
  if ! command -v ollama >/dev/null 2>&1 && ! ollama_up; then
    existing=0
  fi
  install_ollama || return 1
  start_ollama || return 1
  reach_ollama "$([[ "$existing" -eq 1 ]] && echo 2 || echo 1)" || return 1
  pull_local_model "$model" || return 1
  register_ollama "$model"
}

# ---- the Models step: detect, connect what was found, offer to install when nothing is usable ---

ASSUME=0           # --connect-found / --yes: yes for found items (never for installs)
NO_QUESTIONS=0     # --yes / an unattended installer: ask nothing
INSTALL_OLLAMA=0   # --install-ollama: the whole local setup, unattended allowed
NO_LOCAL_MODEL=0   # --no-local-model: never offer the local setup
REBIND_OLLAMA=0    # --rebind-ollama: an existing Ollama may be moved to the Docker bridge
LOCAL_MODEL="$OLLAMA_DEFAULT_MODEL"
LOCAL_MODEL_SET=0
FROM_INSTALLER=0   # the installer already saved keys and handled Codex
FOUND_FILE=""

# Ask unless told not to. ask_default_yes answers yes on Enter.
ask() { [[ "$NO_QUESTIONS" -eq 0 ]] && ask_yes "$1"; }
ask_default_yes() {
  local reply
  [[ "$NO_QUESTIONS" -eq 0 ]] && have_tty || return 1
  printf '%s [Y/n] ' "$1" >/dev/tty
  read -r reply </dev/tty || return 1
  [[ -z "$reply" || "$reply" == y || "$reply" == Y || "$reply" == yes ]]
}

model_state() {
  local key rc=0
  key="$(host_key)"
  BOSS_API_KEY="$key" python3 "$INSTALL_DIR/scripts/install/model_status.py" \
    --api-url "$(api_url)" --env-file "$ENV_FILE" "$@" </dev/null >/dev/null 2>&1 || rc=$?
  return "$rc"
}

# 0 a model BOSS can use is connected, 3 none, 4 BOSS cannot say.
usable_model() {
  local rc=0
  model_state --hosted || rc=$?
  [[ "$rc" -ne 0 ]] || return 0
  [[ "$rc" -ne 4 ]] || return 4
  rc=0
  model_state --local || rc=$?
  return "$rc"
}

print_model_rows() {
  local found="$1" key
  key="$(host_key)"
  BOSS_API_KEY="$key" python3 "$INSTALL_DIR/scripts/install/model_status.py" \
    --api-url "$(api_url)" --env-file "$ENV_FILE" ${found:+--found "$found"} </dev/null
}

models_step() {
  local summary own_summary=0 key rc=0 size failed=0
  if [[ -n "$FOUND_FILE" ]]; then
    summary="$FOUND_FILE"
  else
    printf 'Looking for model keys, subscriptions, coding CLIs, and Ollama on this machine (read only)...\n'
    summary="$(mktemp)"; own_summary=1
    BOSS_OLLAMA_URL="$(ollama_url)" python3 "$INSTALL_DIR/scripts/install/discover_models.py" --summary "$summary" </dev/null
  fi
  state() { grep -m 1 "^$1=" "$summary" | cut -d= -f2-; }

  if [[ "$FROM_INSTALLER" -eq 0 ]]; then
    for key in openai anthropic google xai openrouter; do
      if [[ "$(state "${key}_key")" == found ]]; then
        connect_keys || true
        break
      fi
    done
  fi
  # A Codex, Claude Code or Grok Build login on this machine says which subscription you use.
  # Nothing is copied from it: BOSS offers to sign that subscription in inside BOSS.
  if [[ "$(state codex_login)" == found ]]; then
    offer_sign_in codex "$ASSUME" || true
  fi
  if [[ "$(state claude_login)" == found ]]; then
    offer_sign_in claude "$ASSUME" || true
  fi
  if [[ "$(state grok_login)" == found ]]; then
    offer_sign_in grok "$ASSUME" || true
  fi

  if [[ "$INSTALL_OLLAMA" -eq 1 ]]; then
    setup_local_model "$LOCAL_MODEL" || failed=1
  elif [[ "$(state ollama)" == found ]]; then
    if ollama_models | grep -qxF -- "$LOCAL_MODEL"; then
      connect_ollama "$ASSUME" "$LOCAL_MODEL" || true
    else
      connect_ollama "$ASSUME" || true
    fi
  fi

  release_local_default
  usable_model || rc=$?
  if [[ "$rc" -eq 3 ]]; then
    printf '\nNo model is connected yet, so BOSS cannot run model work.\n'
    for key in codex claude grok; do
      [[ "$(state "${key}_login")" != found ]] || continue  # already offered above
      if have_tty && ask "Sign in to $(provider_label "$key") inside BOSS now? It runs the provider's own login in the BOSS container; you need that subscription."; then
        if sign_in "$key" && usable_model; then
          break
        fi
      fi
    done
    rc=0
    usable_model || rc=$?
  fi
  if [[ "$rc" -eq 3 && "$NO_LOCAL_MODEL" -eq 0 && "$INSTALL_OLLAMA" -eq 0 ]]; then
    size="$(python3 "$INSTALL_DIR/scripts/install/local_model.py" size "$LOCAL_MODEL" </dev/null 2>/dev/null || echo unknown)"
    [[ "$size" =~ ^[0-9]+$ ]] && size="about $(gigabytes "$size") GB" || size="size unknown"
    if ask_default_yes "Set up a free local model? BOSS installs Ollama if needed and downloads $LOCAL_MODEL ($size)."; then
      setup_local_model "$LOCAL_MODEL" || true
    else
      printf '  next      A free local model: boss connect --install-ollama  (or --local-model NAME)\n'
    fi
  fi
  if [[ "$FROM_INSTALLER" -eq 0 ]]; then
    if [[ "$(state codex_login)" != found ]]; then
      printf '  next      Codex: boss connect codex  (signs in inside BOSS through Codex'"'"'s own login)\n'
    fi
    printf '\nModels\n'
    print_model_rows "$summary" || true
  fi
  [[ "$own_summary" -eq 0 ]] || rm -f "$summary"
  # An explicit --install-ollama that did not finish is a failure, not a note.
  return "$failed"
}

cmd_connect() {
  local what="" offer=0 mode=steps summary
  need_install
  while [[ $# -gt 0 ]]; do
    case "$1" in
      codex|claude|grok|ollama|keys) what="$1"; shift ;;
      --connect-found) ASSUME=1; shift ;;
      --yes|-y) ASSUME=1; NO_QUESTIONS=1; shift ;;
      --accept-database-fallback)
        die "--accept-database-fallback is not needed: a subscription signs in inside BOSS through its own login (boss connect codex|claude|grok); nothing is copied or stored from this machine." ;;
      --install-ollama) INSTALL_OLLAMA=1; shift ;;
      --local-model)
        LOCAL_MODEL="${2:-}"; LOCAL_MODEL_SET=1; shift 2 || die "--local-model needs a model name"
        valid_model_name "$LOCAL_MODEL" || die "--local-model takes an Ollama model name such as $OLLAMA_DEFAULT_MODEL" ;;
      --no-local-model) NO_LOCAL_MODEL=1; shift ;;
      --rebind-ollama) REBIND_OLLAMA=1; shift ;;
      --list) mode=list; shift ;;
      --summary) mode=summary; shift ;;
      # Used by the installer.
      --offer) offer=1; shift ;;
      --no-questions) NO_QUESTIONS=1; shift ;;
      --from-installer) FROM_INSTALLER=1; shift ;;
      --found) FOUND_FILE="${2:?--found needs a file}"; shift 2 ;;
      *) die "connect does not take that argument (see boss help). Model keys are read from the environment, never from arguments, so the argument is not repeated here." ;;
    esac
  done
  case "$mode" in
    list)
      printf 'Model keys, subscriptions, coding CLIs, and Ollama on this machine (read only):\n'
      BOSS_OLLAMA_URL="$(ollama_url)" python3 "$INSTALL_DIR/scripts/install/discover_models.py" </dev/null
      return ;;
    summary)
      summary="$(mktemp)"
      BOSS_OLLAMA_URL="$(ollama_url)" python3 "$INSTALL_DIR/scripts/install/discover_models.py" --summary "$summary" </dev/null >/dev/null
      print_model_rows "$summary"
      rm -f "$summary"
      return ;;
  esac
  case "$what" in
    keys) connect_keys; return ;;
    ollama)
      if [[ "$INSTALL_OLLAMA" -eq 1 ]]; then
        setup_local_model "$LOCAL_MODEL"
      elif [[ "$offer" -eq 1 ]]; then
        connect_ollama "$ASSUME"
      else
        connect_ollama 1 "$([[ "${LOCAL_MODEL_SET:-0}" -eq 1 ]] && printf '%s' "$LOCAL_MODEL")"
      fi
      return ;;
    codex|claude|grok)
      if [[ "$offer" -eq 1 ]]; then
        offer_sign_in "$what" "$ASSUME"
      else
        sign_in "$what"
      fi
      return ;;
  esac
  models_step
}

# ---- BOSS fixes itself -------------------------------------------------------------------------

# The self-installer runs on THIS host (never in a container), with python3 and nothing else.
cmd_self_fix() {
  need_install
  [[ $# -gt 0 ]] || die "self-fix needs a command: seed, enroll, watch [--install-service], install SFX, remove SFX, remove-all, status, plan-upgrade --to REF"
  command -v python3 >/dev/null 2>&1 || die "python3 is needed on this machine for the self-installer"
  case "$1" in
    enroll)
      # The installer's own key (sys_self_installer): written 0600 beside .env, never printed.
      local key_file="$INSTALL_DIR/.boss/self-installer.key" key
      mkdir -p "$INSTALL_DIR/.boss" && chmod 700 "$INSTALL_DIR/.boss"
      key="$(compose exec -T -u boss api python -m boss_engine.identity.cli self-installer-key --quiet)" ||
        die "BOSS did not issue a key. Is the api service running (boss status)?"
      [[ "$key" == boss_sak_* ]] || die "BOSS did not issue a key."
      (umask 077 && printf '%s\n' "$key" > "$key_file")
      printf 'This host can now install the self-fixes you approve. The key is in %s.\n' "$key_file" ;;
    *)
      local api
      api="$(api_url)"
      BOSS_SELF_FIX_API="$api" python3 "$INSTALL_DIR/scripts/install/self_fix_installer.py" \
        --dir "$INSTALL_DIR" "$@" ;;
  esac
}

# ---- the terminal -----------------------------------------------------------------------------

# METATRON, NATIVELY (BOSSAPP-761). Colours, keys and the terminal's size work as they do for any
# program on this machine, not through `docker compose exec`. The host tools' key reaches it
# through the environment, never a command line, and is never printed. Rich and prompt_toolkit
# live in a virtualenv of their own outside the install directory, rebuilt when their pins change;
# if they cannot be installed Metatron still runs, as plain text. `make metatron` still runs it
# inside the API container.
metatron_python() {
  local py req venv want stamp
  py="$(command -v python3 || true)"
  [[ -n "$py" ]] || die "Metatron needs python3 on this machine. Or run it in the container: cd $INSTALL_DIR && make metatron"
  "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null ||
    die "Metatron needs Python 3.9 or newer here. Or run it in the container: cd $INSTALL_DIR && make metatron"
  req="$INSTALL_DIR/scripts/metatron-requirements.txt"
  if [[ ! -f "$req" || -n "${BOSS_METATRON_NO_INSTALL:-}" ]]; then
    printf '%s\n' "$py"; return 0
  fi
  venv="${BOSS_METATRON_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/boss/metatron-venv}"
  want="$(cksum <"$req" | cut -d' ' -f1)"
  stamp="$venv/.boss-requirements"
  if [[ ! -x "$venv/bin/python" || "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]]; then
    printf 'Setting up the terminal once (Rich, prompt_toolkit) in %s\n' "$venv" >&2
    if mkdir -p "$(dirname "$venv")" && "$py" -m venv "$venv" >/dev/null 2>&1 &&
       "$venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$req" >/dev/null 2>&1; then
      printf '%s\n' "$want" >"$stamp"
    else
      printf 'boss: could not install Rich and prompt_toolkit; Metatron runs as plain text.\n' >&2
      printf '%s\n' "$py"; return 0
    fi
  fi
  printf '%s\n' "$venv/bin/python"
}

cmd_metatron() {
  need_install
  [[ -f "$INSTALL_DIR/scripts/metatron_tui.py" ]] || die "this release has no Metatron terminal."
  local py key project
  py="$(metatron_python)"
  key="$(host_key)"
  [[ -n "$key" ]] || die "no key for the host tools. Is the api service running (boss status)? Then: boss identity host-key"
  project="${BOSS_PROJECT_ID:-$(env_value BOSS_DEFAULT_PROJECT_ID)}"
  BOSS_API_URL="$(api_url)" BOSS_API_KEY="$key" BOSS_PROJECT_ID="${project:-boss-dogfood}" \
    exec "$py" "$INSTALL_DIR/scripts/metatron_tui.py" "$@"
}

# ---- identity ---------------------------------------------------------------------------------

# The identity CLI inside the api container, as the boss user (never root: files it writes must
# stay the boss user's).
identity_cli() {
  local tty=(-T)
  [[ -t 0 && -t 1 ]] && tty=()
  compose exec ${tty[@]+"${tty[@]}"} -u boss api python -m boss_engine.identity.cli "$@"
}

cmd_identity() {
  need_install
  [[ $# -gt 0 ]] || die "identity needs a command: setup-link, claim-status, recover-owner, host-key, legacy, posture, audit, migrate (see boss help)"
  case "$1" in
    host-key)
      shift
      [[ $# -eq 0 ]] || die "host-key takes no options"
      issue_host_key >/dev/null || die "BOSS did not issue a key. Is the api service running (boss status)?"
      printf 'A new key for the host tools is in %s as BOSS_HOST_API_KEY. Earlier ones stopped working.\n' "$ENV_FILE" ;;
    mcp-key|egress-key|seed-test-accounts)
      die "$1 prints a credential, which this command never does. Use make mcp-setup for MCP, or run it in the container (see docs/community/IDENTITY_ADMIN.md)." ;;
    *) identity_cli "$@" ;;
  esac
}

# ---- the local git remote and local review (BOSSAPP-948) ------------------------------------
# A token is never printed, never an argument of a process, never in a URL or .git/config: it goes
# to git's credential helper on stdin, and to curl as a header on stdin.

git_remote_base() {
  local base="${BOSS_GIT_REMOTE_BASE:-}" port
  if [[ -z "$base" ]]; then
    port="$(env_value BOSS_WEB_PORT)"; base="http://localhost:${port:-3000}"
  fi
  printf '%s' "${base%/}"
}

credential_helper() {
  if [[ -n "${BOSS_GIT_CREDENTIAL_HELPER:-}" ]]; then printf '%s' "$BOSS_GIT_CREDENTIAL_HELPER"; return; fi
  if [[ "$(host_os)" == Darwin ]]; then printf 'osxkeychain'; return; fi
  if [[ -x "$(git --exec-path 2>/dev/null)/git-credential-libsecret" ]] || command -v git-credential-libsecret >/dev/null 2>&1; then
    printf 'libsecret'; return
  fi
  # No keychain on this machine: git keeps it in memory for a day, never on disk.
  printf 'cache --timeout=86400'
}

PERSON_TOKEN_REFUSAL='Run `boss clone <project>` or `boss login` first: comments are signed by a person.'

# A PERSON's token: a personal access token (boss_pat_…), never this machine's host tools key —
# what that key signs reads as no person's (BOSSAPP-948). Only the shape is checked here (no
# network, nothing printed); `boss login` asks BOSS who it belongs to.
is_person_token() {
  local token="$1" host
  [[ "$token" == boss_pat_* ]] || return 1
  host="$(env_value BOSS_HOST_API_KEY)"
  [[ -z "$host" || "$token" != "$host" ]]
}

# The person's BOSS token: what is stored for this BOSS (stored_token), else asked once on the
# terminal (hidden). Never the host tools' key. Printed to stdout for a $(...) capture only —
# never echoed to the terminal; the caller exits when this fails.
person_token() {
  local token
  token="$(stored_token)"
  if [[ -z "$token" ]] && have_tty; then
    printf 'Your personal BOSS access token (boss_pat_…, from Account › Access tokens): ' >/dev/tty
    IFS= read -rs token </dev/tty || token=""
    printf '\n' >/dev/tty
  fi
  [[ -n "$token" ]] || die "no personal BOSS token: set BOSS_TOKEN, run boss login, or run this on a terminal."
  is_person_token "$token" || die "that is not a personal access token (boss_pat_…): the host tools' key and other keys cannot sign for a person. Make one in Account › Access tokens."
  printf '%s' "$token"
}

# Keep TOKEN in git's credential helper (the keychain on macOS), keyed by this BOSS's address.
remember_token() {
  local token="$1" base proto hostport
  base="$(git_remote_base)"; proto="${base%%://*}"; hostport="${base#*://}"; hostport="${hostport%%/*}"
  printf 'protocol=%s\nhost=%s\nusername=boss\npassword=%s\n\n' "$proto" "$hostport" "$token" |
    git -c credential.helper= -c "credential.helper=$(credential_helper)" credential approve
}

# One call to the API, signed with TOKEN (read from the header on stdin, never an argument).
api_call() {
  local token="$1" method="$2" path="$3" body="${4:-}"
  if [[ -n "$body" ]]; then
    printf 'Authorization: Bearer %s\n' "$token" | curl -sS -m 30 -X "$method" "$(api_url)$path" \
      -H @- -H 'Content-Type: application/json' --data-binary "$body" -w '\n%{http_code}'
  else
    printf 'Authorization: Bearer %s\n' "$token" | curl -sS -m 30 -X "$method" "$(api_url)$path" \
      -H @- -w '\n%{http_code}'
  fi
}

json_of() { python3 -c 'import json,sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' "$@"; }

cmd_clone() {
  need_install
  local project="" dir="" branch="" open_it=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --branch) branch="${2:?--branch needs a name}"; shift 2 ;;
      --open) open_it=1; shift ;;
      -*) die "clone: unknown option $1" ;;
      *) if [[ -z "$project" ]]; then project="$1"; elif [[ -z "$dir" ]]; then dir="$1"; else die "clone takes PROJECT [DIR]"; fi; shift ;;
    esac
  done
  [[ -n "$project" ]] || die "clone needs the project: boss clone PROJECT [DIR] [--branch B]"
  [[ "$project" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$ ]] || die "that is not a project id: $project"
  dir="${dir:-$HOME/code/$project}"
  dir="${dir/#\~/$HOME}"
  [[ ! -e "$dir" || -z "$(ls -A "$dir" 2>/dev/null)" ]] || die "$dir already exists and is not empty. Pick another folder."
  local base url helper token
  base="$(git_remote_base)"; url="$base/git/$project.git"; helper="$(credential_helper)"
  token="$(person_token)" || exit 1
  # The token goes to the credential helper (the keychain), keyed by this BOSS's address.
  remember_token "$token"
  mkdir -p "$(dirname "$dir")"
  git -c credential.helper= -c "credential.helper=$helper" clone ${branch:+--branch "$branch"} "$url" "$dir" ||
    die "the clone did not complete (is BOSS running, and is $project a local project?)"
  git -C "$dir" config credential.helper "$helper"
  printf 'Cloned %s into %s%s\n' "$project" "$dir" "${branch:+ on $branch}"
  local said code
  said="$(api_call "$token" POST "/v1/projects/$project/local-repo/working-copies" \
    "$(json_of path "$dir" branch "${branch:-}" machine "$(hostname -s 2>/dev/null || hostname)")" 2>/dev/null || true)"
  code="${said##*$'\n'}"
  [[ "$code" == 200 ]] && printf 'The change page lists it under Working copy.\n' ||
    printf 'BOSS did not record the working copy (HTTP %s); the clone is fine.\n' "${code:-000}"
  if [[ "$open_it" -eq 1 ]]; then
    if command -v code >/dev/null 2>&1; then code "$dir" || true
    elif [[ "$(host_os)" == Darwin ]]; then open "$dir" || true
    fi
  fi
}

# The person's token for review commands: BOSS_TOKEN, else what boss clone / boss login stored
# for this BOSS's address. Empty when there is none — never the host tools' key.
stored_token() {
  local base proto hostport out
  [[ -n "${BOSS_TOKEN:-}" ]] && { printf '%s' "$BOSS_TOKEN"; return; }
  base="$(git_remote_base)"; proto="${base%%://*}"; hostport="${base#*://}"; hostport="${hostport%%/*}"
  out="$(printf 'protocol=%s\nhost=%s\nusername=boss\n\n' "$proto" "$hostport" |
    GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false \
    git -c credential.helper= -c "credential.helper=$(credential_helper)" credential fill 2>/dev/null || true)"
  out="$(printf '%s\n' "$out" | sed -n 's/^password=//p' | head -n 1)"
  printf '%s' "$out"
}

# boss login: keep a person's own access token for this BOSS, so `boss review` signs as them.
# The token is asked for hidden on the terminal (or read from stdin with --with-token, or
# BOSS_TOKEN), checked with BOSS (GET /v1/identity/me: it must be a person's), and handed to git's
# credential helper — never printed, never an argument, never in a file BOSS writes.
cmd_login() {
  need_install
  local token="${BOSS_TOKEN:-}" from_stdin=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --with-token) from_stdin=1; shift ;;
      *) die "login: unknown option $1 (boss login [--with-token < file])" ;;
    esac
  done
  if [[ -z "$token" && "$from_stdin" -eq 1 ]]; then
    IFS= read -r token || true
  elif [[ -z "$token" ]]; then
    have_tty || die "boss login needs a terminal, BOSS_TOKEN, or --with-token on stdin."
    printf 'Make a personal access token in the console (Account › Access tokens), then paste it.\n' >/dev/tty
    printf 'Personal access token (boss_pat_…, hidden): ' >/dev/tty
    IFS= read -rs token </dev/tty || token=""
    printf '\n' >/dev/tty
  fi
  token="${token//[[:space:]]/}"
  [[ -n "$token" ]] || die "no token given."
  is_person_token "$token" || die "that is not a personal access token (boss_pat_…): the host tools' key and other keys cannot sign for a person."
  local said code
  said="$(api_call "$token" GET "/v1/identity/me" 2>/dev/null || true)"
  code="${said##*$'\n'}"; said="${said%$'\n'*}"
  case "$code" in
    200) ;;
    401|403) die "BOSS refused that token (HTTP $code): it is expired, revoked or mistyped." ;;
    *) die "could not reach BOSS at $(api_url) to check the token (HTTP ${code:-000}). Is BOSS running?" ;;
  esac
  local who
  who="$(python3 -c '
import json, sys
try:
    me = json.loads(sys.stdin.read() or "{}")
except ValueError:
    me = {}
if me.get("kind") != "user":
    sys.exit(3)
print(me.get("display_name") or me.get("email") or me.get("principal_id") or "you")
' <<<"$said")" || die "that token is not a person's: comments are signed by a person."
  remember_token "$token"
  printf 'Signed in as %s. The token is kept by git'"'"'s credential helper (%s), for git and boss review.\n' \
    "$who" "$(credential_helper)"
}

project_from_remote() {
  local remote
  remote="$(git remote get-url origin 2>/dev/null || true)"
  [[ "$remote" =~ /git/([A-Za-z0-9._-]+)(/[A-Za-z0-9._-]+)?\.git/?$ ]] && printf '%s' "${BASH_REMATCH[1]}"
}

cmd_review() {
  need_install
  local project="" severity="" args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project) project="${2:?--project needs an id}"; shift 2 ;;
      --severity) severity="${2:?--severity needs must, should or nit}"; shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  set -- "${args[@]+"${args[@]}"}"
  [[ $# -ge 2 ]] || die "review needs: comment N FILE:LINE TEXT | list N | reply N ID TEXT | resolve N ID"
  local action="$1" number="$2"; shift 2
  [[ "$number" =~ ^[0-9]+$ ]] || die "the change number is a number: $number"
  project="${project:-$(project_from_remote)}"
  [[ -n "$project" ]] || die "which project? Run this inside a working copy from boss clone, or pass --project."
  local token said code body base="/v1/projects/$project/local-changes/$number"
  token="$(stored_token)"
  # Signed by a person or not at all: no fallback to the host tools' key (BOSSAPP-948).
  [[ -n "$token" ]] && is_person_token "$token" || die "$PERSON_TOKEN_REFUSAL"
  case "$action" in
    comment)
      [[ $# -eq 2 ]] || die "review comment N FILE:LINE \"TEXT\""
      local anchor="$1" text="$2" file line
      file="${anchor%:*}"; line="${anchor##*:}"
      [[ "$anchor" == *:* && "$line" =~ ^[0-9]+$ ]] || { file="$anchor"; line=""; }
      case "$severity" in ""|must|must_fix) severity="must fix" ;; should|should_fix) severity="should fix" ;; nit) ;; *) die "--severity is must, should or nit" ;; esac
      if [[ -n "$line" ]]; then
        body="$(python3 -c 'import json,sys; print(json.dumps({"path": sys.argv[1], "line": int(sys.argv[2]), "body": sys.argv[3], "severity": sys.argv[4], "origin": "cli"}))' "$file" "$line" "$text" "$severity")"
      else
        body="$(python3 -c 'import json,sys; print(json.dumps({"path": sys.argv[1], "body": sys.argv[2], "severity": sys.argv[3], "origin": "cli"}))' "$file" "$text" "$severity")"
      fi
      said="$(api_call "$token" POST "$base/comments" "$body")" ;;
    list) said="$(api_call "$token" GET "$base/comments")" ;;
    reply)
      [[ $# -eq 2 && "$1" =~ ^[0-9]+$ ]] || die "review reply N ID \"TEXT\""
      said="$(api_call "$token" POST "$base/comments/$1/reply" "$(json_of body "$2" origin cli)")" ;;
    resolve)
      [[ $# -eq 1 && "$1" =~ ^[0-9]+$ ]] || die "review resolve N ID"
      said="$(api_call "$token" POST "$base/comments/$1/resolve" '{}')" ;;
    *) die "review: comment, list, reply or resolve" ;;
  esac
  code="${said##*$'\n'}"; said="${said%$'\n'*}"
  BOSS_REVIEW_ACTION="$action" BOSS_REVIEW_NUMBER="$number" BOSS_REVIEW_CODE="$code" python3 -c '
import json, os, sys
action, number, code = os.environ["BOSS_REVIEW_ACTION"], os.environ["BOSS_REVIEW_NUMBER"], os.environ["BOSS_REVIEW_CODE"]
try:
    data = json.loads(sys.stdin.read() or "{}")
except ValueError:
    data = {}
if code != "200":
    msg = data.get("message") or (data.get("detail") if isinstance(data.get("detail"), str) else "") or data.get("error") or ("HTTP " + code)
    print("boss: " + str(msg), file=sys.stderr); sys.exit(1)
def where(c):
    return (c.get("path") or "(the change)") + (":" + str(c["line"]) if c.get("line") else "")
if action == "list":
    for t in data.get("items", []):
        print("  %-10s %-24s %-40s %s" % (t["state"], where(t), (t["body"][:38] + "…") if len(t["body"]) > 39 else t["body"], t["author"]))
    if not data.get("items"):
        print("  no comments on change #" + number)
else:
    verb = {"comment": "comment", "reply": "reply", "resolve": "resolved"}[action]
    print("✓ %s %s on change #%s · %s · %s · %s" % (verb, data.get("id"), number, where(data), data.get("severity_words", ""), data.get("state", "")))
' <<<"$said"
}

case "$COMMAND" in
  clone) cmd_clone "$@" ;;
  login) cmd_login "$@" ;;
  review) cmd_review "$@" ;;
  status) cmd_status "$@" ;;
  metatron|terminal) cmd_metatron "$@" ;;
  identity) cmd_identity "$@" ;;
  open) cmd_open "$@" ;;
  password) cmd_password "$@" ;;
  doctor|checkup) cmd_doctor "$@" ;;
  logs) cmd_logs "$@" ;;
  connect) cmd_connect "$@" ;;
  upgrade) cmd_upgrade "$@" ;;
  rollback) cmd_rollback "$@" ;;
  update) cmd_update "$@" ;;
  uninstall) cmd_uninstall "$@" ;;
  prune) cmd_prune "$@" ;;
  host-sample) cmd_host_sample "$@" ;;
  toolchains) cmd_toolchains "$@" ;;
  self-fix) cmd_self_fix "$@" ;;
  version|--version) cmd_version ;;
  *) printf 'boss: unknown command %s\n\n' "$COMMAND" >&2; usage >&2; exit 2 ;;
esac
