#!/usr/bin/env bash
# =============================================================================
# BOSS Community — one-line installer (OpenClaw-style)
#
#   See docs/community/QUICKSTART.md for the reviewed-commit download command.
#
# Guided Community setup: checks the machine, installs deps, prompts for
# keys, starts Postgres + API + worker (+ optional Mission Control), seeds the
# project, and verifies health. No BOSS Cloud required.
# =============================================================================
set -euo pipefail
umask 077

BOSS_INSTALL_VERSION="1.1.0"
BOSS_REPO_URL="${BOSS_REPO_URL:-https://github.com/seanventures/boss.git}"
BOSS_REPO_REF="${BOSS_REPO_REF:-main}"

# ---- UI --------------------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
  CYAN='\033[38;2;46;197;197m'; BLUE='\033[38;2;47;111;224m'
  GREEN='\033[38;2;125;255;168m'; AMBER='\033[38;2;255;210;122m'
  RED='\033[38;2;255;120;120m'; MUTED='\033[38;2;100;118;154m'
else
  BOLD=''; DIM=''; NC=''; CYAN=''; BLUE=''; GREEN=''; AMBER=''; RED=''; MUTED=''
fi

ui_banner() {
  echo ""
  echo -e "${CYAN}${BOLD}"
  cat <<'BANNER'
  ____   ____   _____ _____
 |  _ \ / __ \ / ____/ ____|
 | |_) | |  | | (___| (___
 |  _ <| |  | |\___ \\___ \
 | |_) | |__| |____) |___) |
 |____/ \____/|_____/_____/

  Community Edition — governed AI work, self-hosted
BANNER
  echo -e "${NC}${MUTED}  one-line installer v${BOSS_INSTALL_VERSION}${NC}"
  echo ""
}
ui_step()  { echo -e "${CYAN}${BOLD}==>${NC}${BOLD} $*${NC}"; }
ui_ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
ui_warn()  { echo -e "  ${AMBER}!${NC} $*"; }
ui_err()   { echo -e "  ${RED}✗${NC} $*" >&2; }
ui_info()  { echo -e "  ${MUTED}•${NC} $*"; }
die()      { ui_err "$*"; exit 1; }

# ---- defaults / flags ------------------------------------------------------
INSTALL_DIR="${BOSS_INSTALL_DIR:-$HOME/boss}"
BACKUP_DIR=""
ASSUME_YES=0
WITH_WEB=1
WEB_EXPLICIT=0
NONINTERACTIVE=0
SKIP_MODEL_KEY=0
OPENAI_KEY_PRESET="${OPENAI_API_KEY:-}"
ANTHROPIC_KEY_PRESET="${ANTHROPIC_API_KEY:-}"
GITHUB_TOKEN_PRESET="${BOSS_GITHUB_TOKEN:-}"
API_PORT="${BOSS_API_PORT:-8000}"
API_PORT_EXPLICIT=0
[[ -n "${BOSS_API_PORT:-}" ]] && API_PORT_EXPLICIT=1
# THE ADDRESS THE BROWSER WILL USE — not the one the server calls itself.
#
# NEXT_PUBLIC_BOSS_API_URL is compiled INTO the web bundle at image build time. Defaulting it
# to 127.0.0.1 is right on a laptop and wrong on every server: the browser then tries to reach
# the API on the machine it is running on, which is the operator's, and every preview link
# points at nothing while the previews themselves are fine. Empty means "this is a laptop".
PUBLIC_URL="${BOSS_PUBLIC_URL:-}"
# Set when this run was told an address. Unset means "keep the URL a previous .env already
# has" — writing 127.0.0.1 over it rebuilds the console against the operator's laptop.
PUBLIC_URL_EXPLICIT=0
if [ -n "${BOSS_PUBLIC_URL:-}" ]; then
  PUBLIC_URL_EXPLICIT=1
fi
WEB_PORT="${BOSS_WEB_PORT:-3000}"
WEB_PORT_EXPLICIT=0
[[ -n "${BOSS_WEB_PORT:-}" ]] && WEB_PORT_EXPLICIT=1
PG_PORT="${BOSS_POSTGRES_PORT:-5432}"
PG_PORT_EXPLICIT=0
[[ -n "${BOSS_POSTGRES_PORT:-}" ]] && PG_PORT_EXPLICIT=1
BIND_ADDRESS="127.0.0.1"
COMPOSE_PROFILES=""
# WHERE THIS INSTALL KEEPS ITS SECRETS. Both options stay inside your own infrastructure; the
# difference is two containers and about 400MB, not who holds the data. See --help.
SECRET_STORE="${BOSS_SECRET_STORE:-database}"
DOCKER=(docker)
INSTALL_DEPS=1
DISCOVER_SUBSCRIPTIONS=0
CONNECT_CODEX=0
CONNECT_CLAUDE=0
CONNECT_CLAUDE_LOCAL=0
INSTALL_CODING_CLIS=0
ACCEPT_DATABASE_FALLBACK=0
SECRET_STORE_EXPLICIT=0
if [ -n "${BOSS_SECRET_STORE:-}" ]; then
  SECRET_STORE_EXPLICIT=1
fi

usage() {
  cat <<EOF
BOSS Community installer

Usage:
  ./install.sh [options]
  See docs/community/QUICKSTART.md for the reviewed-commit download command.

Options:
  --dir PATH              Install directory (default: ~/boss)
  --yes, -y               Non-interactive defaults (use --skip-model-key to start without one)
  --openai-key KEY        OpenAI (or compatible) API key
  --anthropic-key KEY     Anthropic API key (optional)
  --github-token TOKEN    GitHub token for Software Factory (optional)
  --no-web                Skip Mission Control web console
  --web                   Enable Mission Control on an install previously made without it
  --public-url URL        Browser-reachable API URL when using a reverse proxy
  --api-port N            Host port for API (default 8000)
  --web-port N            Host port for web (default 3000)
  --postgres-port N       Host port for Postgres (default 5432)
  --ref REF               Git ref or full commit SHA to install (default main)
  --backup-dir PATH       Private recovery backup location for an upgrade
  --skip-model-key        Allow empty model key (API starts; model calls fail closed)
  --no-install-deps       Do not install missing VM packages
  --discover-subscriptions  Check Claude, Codex and Grok CLIs on this VM (no credential transfer)
  --install-coding-clis  Install missing official Claude, Codex and Grok coding CLIs on this VM
  --connect-codex        Explicitly import this VM user's Codex ChatGPT login into BOSS
  --connect-claude       Rejected. Claude setup-token import has no recorded clearance
  --connect-claude-local Record the CLI-owned Claude login made inside the API container
  --accept-database-fallback  Allow a subscription credential in Postgres if no secret store
  --secret-store WHICH    database (default) or infisical. Both stay on your own machine:
                            database   4 containers. Credentials are held by BOSS's own
                                       Postgres. Lean; storing a SUBSCRIPTION token asks you
                                       to confirm the database is an acceptable home for it.
                            infisical  6 containers, ~400MB more. Bundled Infisical secret
                                       store, bootstrapped and wired up for you. Credentials
                                       are referenced rather than held, and rotate cleanly.
  -h, --help              Show this help

API and web bind to loopback only; use an SSH tunnel or HTTPS reverse proxy remotely.

Environment overrides:
  BOSS_INSTALL_DIR, OPENAI_API_KEY, ANTHROPIC_API_KEY, BOSS_GITHUB_TOKEN,
  BOSS_REPO_URL, BOSS_REPO_REF, BOSS_API_PORT, BOSS_WEB_PORT, BOSS_POSTGRES_PORT,
  BOSS_PUBLIC_URL, BOSS_SECRET_STORE

Examples:
  ./install.sh --dir "\$PWD" --yes --skip-model-key
  ./install.sh --dir "\$PWD" --ref REVIEWED_COMMIT --backup-dir "\$HOME/boss-backups"
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; NONINTERACTIVE=1; shift ;;
    --no-install-deps) INSTALL_DEPS=0; shift ;;
    --discover-subscriptions) DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --install-coding-clis) INSTALL_CODING_CLIS=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --connect-codex) CONNECT_CODEX=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --connect-claude) CONNECT_CLAUDE=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --connect-claude-local) CONNECT_CLAUDE_LOCAL=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --accept-database-fallback) ACCEPT_DATABASE_FALLBACK=1; shift ;;
    --openai-key) OPENAI_KEY_PRESET="$2"; shift 2 ;;
    --anthropic-key) ANTHROPIC_KEY_PRESET="$2"; shift 2 ;;
    --github-token) GITHUB_TOKEN_PRESET="$2"; shift 2 ;;
    --no-web) WITH_WEB=0; WEB_EXPLICIT=1; shift ;;
    --web) WITH_WEB=1; WEB_EXPLICIT=1; shift ;;
    --public-url) PUBLIC_URL="$2"; PUBLIC_URL_EXPLICIT=1; shift 2 ;;
    --bind-address) die "--bind-address is no longer supported; Community binds API/web to loopback. Use an SSH tunnel or HTTPS reverse proxy." ;;
    --api-port) API_PORT="$2"; API_PORT_EXPLICIT=1; shift 2 ;;
    --web-port) WEB_PORT="$2"; WEB_PORT_EXPLICIT=1; shift 2 ;;
    --postgres-port) PG_PORT="$2"; PG_PORT_EXPLICIT=1; shift 2 ;;
    --ref) BOSS_REPO_REF="$2"; shift 2 ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --skip-model-key) SKIP_MODEL_KEY=1; shift ;;
    --secret-store)
      SECRET_STORE="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"; shift 2
      SECRET_STORE_EXPLICIT=1
      case "$SECRET_STORE" in database|infisical) ;; *) die "--secret-store must be database or infisical" ;; esac ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
done
if [[ "$CONNECT_CLAUDE" -eq 1 ]]; then
  die "Claude setup-token import is disabled. No provider or counsel clearance is recorded for copying a Claude session token into BOSS. Use --connect-claude-local after claude auth login as the boss user inside the API container."
fi
if [[ "$CONNECT_CLAUDE" -eq 1 && "$CONNECT_CLAUDE_LOCAL" -eq 1 ]]; then
  die "Choose --connect-claude or --connect-claude-local, not both."
fi

if [[ -n "${BOSS_PREUPGRADE_BACKUP_DIR:-}" ]]; then
  ui_warn 'Ignoring a legacy pre-upgrade backup path; this installer will make its own fresh snapshot before updating source.'
fi

# Expand ~
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
if [[ -n "${BOSS_BIND_ADDRESS:-}" && "$BOSS_BIND_ADDRESS" != 127.0.0.1 ]]; then
  die "BOSS_BIND_ADDRESS is no longer supported; Community binds API/web to loopback. Use an SSH tunnel or HTTPS reverse proxy."
fi

# A pipe occupies stdin. Read questions from the terminal so the one-liner is interactive.
# In unattended shells, choose safe defaults rather than reading the rest of install.sh.
if [[ "$NONINTERACTIVE" -eq 0 ]] && ! (: </dev/tty) 2>/dev/null; then
  NONINTERACTIVE=1
  ASSUME_YES=1
fi

prompt() {
  # prompt "Question" "default" -> sets REPLY
  local q="$1" def="${2:-}"
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then
    REPLY="$def"
    return
  fi
  if [[ -n "$def" ]]; then
    read -r -p "$(echo -e "${BOLD}$q${NC} ${MUTED}[$def]${NC}: ")" REPLY </dev/tty || true
    REPLY="${REPLY:-$def}"
  else
    read -r -p "$(echo -e "${BOLD}$q${NC}: ")" REPLY </dev/tty || true
  fi
}

prompt_secret() {
  local q="$1"
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then
    REPLY=""
    return
  fi
  read -r -s -p "$(echo -e "${BOLD}$q${NC}: ")" REPLY </dev/tty || true
  echo ""
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_coding_clis() {
  [[ "$INSTALL_CODING_CLIS" -eq 1 ]] || return 0
  # The official native installers install per user, so never run them through sudo.
  # Download first: a failed HTTP fetch must never execute a partial shell script.
  export PATH="$HOME/.local/bin:$HOME/.grok/bin:$HOME/.codex/bin:$PATH"
  local spec name binary url shell script
  for spec in \
      'Claude|claude|https://claude.ai/install.sh|bash' \
      'Codex|codex|https://chatgpt.com/codex/install.sh|sh' \
      'Grok|grok|https://x.ai/cli/install.sh|bash'; do
    IFS='|' read -r name binary url shell <<< "$spec"
    if need_cmd "$binary" && "$binary" --version >/dev/null 2>&1; then
      ui_ok "$name CLI already installed"
      continue
    fi
    ui_step "Installing $name CLI from its official installer"
    script="$(mktemp)"
    if ! curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 "$url" -o "$script"; then
      rm -f "$script"
      die "$name installer could not be downloaded from $url"
    fi
    if ! "$shell" "$script" </dev/null; then
      rm -f "$script"
      die "$name installer failed. BOSS core install has not started."
    fi
    rm -f "$script"
    hash -r
    need_cmd "$binary" && "$binary" --version >/dev/null 2>&1 ||
      die "$name installer completed but $binary --version failed; check the installer's PATH instructions."
    ui_ok "$name CLI ready"
  done
}

# ---- preflight -------------------------------------------------------------
ui_banner
ui_step "Checking your machine"

OS="$(uname -s 2>/dev/null || echo unknown)"
ARCH="$(uname -m 2>/dev/null || echo unknown)"
ui_info "OS: $OS  arch: $ARCH"

install_vm_deps() {
  [[ "$INSTALL_DEPS" -eq 1 ]] || die "Missing dependencies. Remove --no-install-deps to install them."
  [[ "$OS" == Linux && -f /etc/os-release ]] || die "Automatic dependencies require Ubuntu or Debian Linux. Install git, curl, Docker Engine and Compose manually."
  # shellcheck disable=SC1091
  . /etc/os-release
  case "$ID:$VERSION_ID" in
    ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
    *) die "Automatic dependencies support Ubuntu 22.04/24.04 and Debian 12/13; found ${PRETTY_NAME:-$ID}." ;;
  esac
  need_cmd apt-get && need_cmd dpkg || die "apt-get and dpkg are required for VM bootstrap."
  local admin=()
  if [[ "$(id -u)" -ne 0 ]]; then
    need_cmd sudo || die "Install sudo or run as root to install missing VM packages."
    # Some cloud images allow passwordless package commands but reject sudo -v.
    # Test access through the real operation, without prompting in a piped install.
    admin=(sudo -n)
  fi
  ui_step "Installing missing VM dependencies"
  "${admin[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update -qq ||
    die "Could not update VM packages. Check package sources, network, and noninteractive administrator access."
  "${admin[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git make openssl python3
  if ! need_cmd docker || ! docker compose version >/dev/null 2>&1; then
    # Replacing an existing container runtime can interrupt unrelated workloads.
    for pkg in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
      if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
        die "Existing $pkg needs a manual Docker/Compose upgrade; leaving it untouched."
      fi
    done
    "${admin[@]}" install -m 0755 -d /etc/apt/keyrings
    local docker_key
    docker_key="$(mktemp)"
    if ! curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$docker_key"; then
      rm -f "$docker_key"
      die "Could not fetch Docker's package signing key."
    fi
    "${admin[@]}" install -m 0644 "$docker_key" /etc/apt/keyrings/docker.asc
    rm -f "$docker_key"
    local codename="${VERSION_CODENAME}"
    if [[ "$ID" == ubuntu ]]; then codename="${UBUNTU_CODENAME:-$VERSION_CODENAME}"; fi
    printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$ID" "$codename" "$(dpkg --print-architecture)" |
      "${admin[@]}" tee /etc/apt/sources.list.d/docker.sources >/dev/null
    "${admin[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    "${admin[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  if need_cmd systemctl; then
    "${admin[@]}" systemctl enable --now docker || die "Docker installed, but its service did not start."
  fi
}

if ! need_cmd git || ! need_cmd curl || ! need_cmd openssl || ! need_cmd python3 || ! need_cmd docker || ! docker compose version >/dev/null 2>&1 || { [[ "$OS" == Linux ]] && ! need_cmd make; }; then
  install_vm_deps
fi
need_cmd git && need_cmd curl && need_cmd openssl && need_cmd python3 && need_cmd docker ||
  die "git, curl, OpenSSL, Python 3 and Docker are required."
if [[ "$OS" == Linux ]]; then
  need_cmd make || die "make is required for the Community command targets. Remove --no-install-deps to install it on supported VMs."
fi

# Adding the current user to the docker group would grant root-equivalent access and does
# not take effect in this shell. Use sudo for this install when the socket requires it.
if ! docker info >/dev/null 2>&1; then
  if [[ "$OS" == Linux && "$(id -u)" -ne 0 ]] && need_cmd sudo && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    die "Docker daemon is unavailable. Start Docker and rerun; on Linux ensure noninteractive sudo access to Docker."
  fi
fi
ui_ok "Docker daemon is running"
if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  COMPOSE=("${DOCKER[@]}" compose)
elif need_cmd docker-compose; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose v2 is required."
fi
ui_ok "Docker Compose: $("${COMPOSE[@]}" version 2>/dev/null | head -1)"
install_coding_clis

# Image builds need much more room than the old 3 GB check implied.
if need_cmd df; then
  FREE_KB="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print $4}')"
  if [[ -n "${FREE_KB:-}" && "$FREE_KB" -lt 80000000 ]]; then
    ui_warn "Less than 80 GB free under $HOME. Allow about 100 GB for images, build cache and agent work."
  else
    ui_ok "Disk space looks OK"
  fi
fi

# Port checks (best-effort)
port_in_use() {
  local p="$1"
  if need_cmd lsof; then
    lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
  elif need_cmd ss; then
    ss -lnt 2>/dev/null | grep -q ":$p "
  else
    return 1
  fi
}
for p in "$API_PORT" "$WEB_PORT" "$PG_PORT"; do
  if port_in_use "$p"; then
    ui_warn "Port $p is already in use — installer will still try; override with --api-port / --web-port / --postgres-port if needed."
  fi
done

# ---- prompts ---------------------------------------------------------------
ui_step "A few quick questions"

if [[ "$ASSUME_YES" -eq 0 ]]; then
  prompt "Install directory" "$INSTALL_DIR"
  INSTALL_DIR="${REPLY/#\~/$HOME}"
fi
ui_ok "Install dir: $INSTALL_DIR"

# A noninteractive reinstall or upgrade must retain the service profile chosen on the
# first run. Without this marker, `upgrade.sh --yes` silently added the web build to a
# headless install; on a small VM that can exhaust memory before the API is refreshed.
# Existing installs made before this marker can still use --no-web once to record it.
if [[ "$WEB_EXPLICIT" -eq 0 && -f "$INSTALL_DIR/.env" ]]; then
  case "$(grep -m 1 '^BOSS_COMMUNITY_WEB_ENABLED=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)" in
    0) WITH_WEB=0 ;;
    1) WITH_WEB=1 ;;
  esac
fi

OPENAI_KEY="$OPENAI_KEY_PRESET"
if [[ -z "$OPENAI_KEY" && "$SKIP_MODEL_KEY" -eq 0 ]]; then
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then
    die "Non-interactive mode needs OPENAI_API_KEY env, --openai-key, or --skip-model-key"
  fi
  echo ""
  ui_info "BOSS Community uses YOUR model keys (BYOK). Paste an OpenAI key, or leave blank to skip."
  ui_info "Compatible base URLs (Ollama, etc.) can be set later in .env as OPENAI_BASE_URL."
  prompt_secret "OpenAI API key (sk-…, optional)"
  OPENAI_KEY="${REPLY:-}"
fi
if [[ -z "$OPENAI_KEY" ]]; then
  ui_warn "No model key — API will start; model calls fail closed until you add OPENAI_API_KEY to .env"
else
  ui_ok "Model key received (not printed)"
fi

ANTHROPIC_KEY="$ANTHROPIC_KEY_PRESET"
GITHUB_TOKEN="$GITHUB_TOKEN_PRESET"

if [[ "$ASSUME_YES" -eq 0 && "$NONINTERACTIVE" -eq 0 ]]; then
  _web_default=Y
  [[ "$WITH_WEB" -eq 0 ]] && _web_default=N
  prompt "Install Mission Control web console? (Y/n)" "$_web_default"
  case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
    n|no) WITH_WEB=0 ;;
    *) WITH_WEB=1 ;;
  esac
fi
# THE CHOICE IS THE OPERATOR'S, because the right answer depends on their machine.
#
# install.sh used to generate INFISICAL_ENCRYPTION_KEY and then never start Infisical, so every
# install had those keys written and unused and no secret store at all — which only surfaced
# later, when storing a subscription token returned "no secret store". Now it is asked and
# answered, and whichever way it goes the install matches what was chosen.
#
# A SECOND RUN MUST NOT FORGET THE ANSWER. `remote.sh install --yes` does not repeat
# --secret-store, and the interactive prompt used to default to "1" even when this run passed
# --secret-store infisical and even when .env already held a machine-identity token. Enter, or
# -y, then started the stack without the secrets profile. The token detection is outside the
# interactive block so a non-interactive update keeps the store it already provisioned. An
# explicit --secret-store still wins, including an explicit database.
if [[ "$SECRET_STORE_EXPLICIT" -eq 0 && -f "$INSTALL_DIR/.env" ]]; then
  _prior_store="$(grep -m 1 '^BOSS_COMMUNITY_SECRET_STORE=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
  case "$_prior_store" in
    database|infisical) SECRET_STORE="$_prior_store" ;;
    *)
      if grep -qs '^BOSS_INFISICAL_TOKEN=.' "$INSTALL_DIR/.env"; then
        SECRET_STORE="infisical"
      fi ;;
  esac
fi
if [[ "$ASSUME_YES" -eq 0 && "$NONINTERACTIVE" -eq 0 ]]; then
  echo ""
  ui_info "Where should this install keep credentials? Both stay on this machine."
  ui_info "  1) database   — 4 containers, leaner. BOSS's own Postgres holds them."
  ui_info "  2) infisical  — 6 containers (~400MB more). A real secret store, bootstrapped."
  _store_default=1
  [[ "$SECRET_STORE" == "infisical" ]] && _store_default=2
  prompt "Secret store: 1 (database) or 2 (infisical)?" "$_store_default"
  case "$(printf '%s' "$REPLY" | tr -d ' ' | tr '[:upper:]' '[:lower:]')" in
    2|infisical) SECRET_STORE="infisical" ;;
    1|database) SECRET_STORE="database" ;;
    *) die "Secret store must be 1 (database) or 2 (infisical)" ;;
  esac
fi

if [[ "$WITH_WEB" -eq 1 ]]; then
  COMPOSE_PROFILES="--profile web"
  ui_ok "Mission Control: yes (port $WEB_PORT)"
else
  ui_ok "Mission Control: skipped"
fi

if [[ "$SECRET_STORE" == "infisical" ]]; then
  COMPOSE_PROFILES="$COMPOSE_PROFILES --profile secrets"
  ui_ok "Secret store: bundled Infisical"
else
  ui_ok "Secret store: BOSS's database (no extra containers)"
fi

# Generate separate owner and worker control-plane API keys. On a rerun the
# previous values win below, before the temporary .env replaces the live file.
if need_cmd openssl; then
  BOSS_API_KEY="$(openssl rand -hex 24)"
  BOSS_WORKER_API_KEY="$(openssl rand -hex 24)"
else
  BOSS_API_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  BOSS_WORKER_API_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
BOSS_DB_PASSWORD="boss"
if need_cmd openssl; then
  BOSS_DB_PASSWORD="$(openssl rand -hex 12)"
fi
# The console gate has its own credentials. The API bearer key must never be used
# as a browser login secret; each value is random on first install and preserved
# by restore_preserved_env.sh on reruns.
BOSS_CONSOLE_PASSWORD="$(openssl rand -hex 24)"
BOSS_CONSOLE_SESSION_SECRET="$(openssl rand -hex 32)"

# ---- fetch source ----------------------------------------------------------
ui_step "Fetching BOSS Community source"

mkdir -p "$(dirname "$INSTALL_DIR")"
if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR" ]]; then
  die "$INSTALL_DIR exists and is not a directory."
fi
if [[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" ]]; then
  [[ -f "$INSTALL_DIR/scripts/install/community-compose.yml" ]] ||
    die "$INSTALL_DIR is a Git repository but not a BOSS checkout."
  if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]]; then
    die "$INSTALL_DIR has tracked local changes. Commit or move them before updating BOSS."
  fi
  ui_info "Updating existing checkout at $INSTALL_DIR to $BOSS_REPO_REF"
  git -C "$INSTALL_DIR" fetch origin "$BOSS_REPO_REF" </dev/null ||
    die "Could not fetch $BOSS_REPO_REF; existing checkout left unchanged."
  _installed_commit="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  _fetched_commit="$(git -C "$INSTALL_DIR" rev-parse FETCH_HEAD)"
  git -C "$INSTALL_DIR" merge-base --is-ancestor "$_installed_commit" "$_fetched_commit" ||
    die "The requested ref cannot fast-forward this install; existing checkout left unchanged."
  if [[ "$_installed_commit" != "$_fetched_commit" && -f "$INSTALL_DIR/.env" ]]; then
    # A pasted install command is also an upgrade command for an existing checkout.
    # Snapshot the old database, artifacts and credentials before switching source:
    # the new API may migrate the database as soon as it starts. Always create
    # this run's own snapshot; a prior path could refer to stale data even if
    # its source commit happens to match.
    _backup_helper="$(mktemp)"
    if ! git -C "$INSTALL_DIR" show FETCH_HEAD:scripts/install/backup.sh > "$_backup_helper"; then
      rm -f "$_backup_helper"
      die "The fetched source has no backup helper; existing checkout left unchanged."
    fi
    _backup_args=(--dir "$INSTALL_DIR")
    [[ -z "$BACKUP_DIR" ]] || _backup_args+=(--backup-dir "$BACKUP_DIR")
    if ! _recovery_backup="$(bash "$_backup_helper" "${_backup_args[@]}" </dev/null)"; then
      rm -f "$_backup_helper"
      die "Recovery backup failed; existing source, credentials and volumes were left unchanged. Check the backup error above, start the installed database if needed, then retry."
    fi
    rm -f "$_backup_helper"
    [[ -s "$_recovery_backup/.env" && -s "$_recovery_backup/boss.dump" &&
       -s "$_recovery_backup/artifacts.tar" &&
       "$(cat "$_recovery_backup/source-commit" 2>/dev/null)" == "$_installed_commit" ]] ||
      die "Recovery backup is incomplete or for another source commit; existing checkout left unchanged."
    ui_ok "Private recovery backup: $_recovery_backup"
  fi
  git -C "$INSTALL_DIR" merge --ff-only FETCH_HEAD </dev/null ||
    die "Could not fast-forward; recovery backup: ${_recovery_backup:-not needed}."
elif [[ -d "$INSTALL_DIR" ]]; then
  [[ -z "$(ls -A "$INSTALL_DIR")" ]] ||
    die "$INSTALL_DIR is not empty and is not a Git checkout. Choose --dir; nothing was removed."
  ui_info "Cloning $BOSS_REPO_URL ($BOSS_REPO_REF) → $INSTALL_DIR"
  if [[ "$BOSS_REPO_REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
    git clone --no-checkout "$BOSS_REPO_URL" "$INSTALL_DIR" </dev/null &&
      git -C "$INSTALL_DIR" checkout --detach "$BOSS_REPO_REF" </dev/null ||
      die "Exact-commit clone failed; existing directory was not removed."
  else
    git clone --depth 1 --branch "$BOSS_REPO_REF" "$BOSS_REPO_URL" "$INSTALL_DIR" </dev/null ||
      die "Clone failed; existing directory was not removed."
  fi
else
  ui_info "Cloning $BOSS_REPO_URL ($BOSS_REPO_REF) → $INSTALL_DIR"
  if [[ "$BOSS_REPO_REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
    git clone --no-checkout "$BOSS_REPO_URL" "$INSTALL_DIR" </dev/null &&
      git -C "$INSTALL_DIR" checkout --detach "$BOSS_REPO_REF" </dev/null ||
      die "Exact-commit clone failed; check repo visibility, URL, and commit."
  else
    git clone --depth 1 --branch "$BOSS_REPO_REF" "$BOSS_REPO_URL" "$INSTALL_DIR" </dev/null ||
      die "Clone failed; check repo visibility, URL, and ref."
  fi
fi

if [[ "$BOSS_REPO_REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
  _expected_commit="$(printf '%s' "$BOSS_REPO_REF" | tr '[:upper:]' '[:lower:]')"
  [[ "$(git -C "$INSTALL_DIR" rev-parse HEAD)" == "$_expected_commit" ]] ||
    die 'Installed checkout does not match the requested release commit.'
fi

cd "$INSTALL_DIR"
[[ -f scripts/install/community-compose.yml ]] || die "community-compose.yml missing — is this a BOSS checkout?"
ui_ok "Source ready: $INSTALL_DIR"

# ---- write env -------------------------------------------------------------
ui_step "Writing Community configuration"

ENV_FILE="$INSTALL_DIR/.env"

# RE-RUNNING THE INSTALLER MUST NOT BRICK THE INSTALL.
#
# Everything below writes .env with `cat >`, wholesale, and the credentials above it are
# generated fresh every run. That is correct for a first install and destructive for a second:
# the Postgres ROLE PASSWORD lives in the data volume, which outlives .env. Regenerating it
# left the database expecting the old password and the API presenting a new one, so every
# request that touched the database answered
#
#     FATAL: password authentication failed for user "boss"
#
# while /health still answered 200 — an install that looks up and cannot read a row. Found by
# running the installer twice over the same volume on one VM.
#
# The API key is preserved for a smaller reason: anything an operator saved, scripted or put in
# a tunnel command keeps working across a re-run instead of silently 401-ing.
#
# THE REWRITE STILL DELETES EVERYTHING ELSE. The database password was the first thing a
# second run destroyed, because Postgres keeps it in the volume. The secret store is the same
# shape and was not covered: INFISICAL_ENCRYPTION_KEY and BOSS_INFISICAL_TOKEN live only in
# this file. Deleting them and generating a new encryption key (the block below this write
# does that whenever the line is absent) makes the Infisical database in that same volume
# unreadable, and the token cannot be issued again. Model keys go the same way: a remote
# update runs `-y --skip-model-key`, so the heredoc writes OPENAI_API_KEY= blank.
# restore_preserved_env.sh puts those lines back. It runs on a temp file, and the live .env
# is replaced only after that succeeds, so a failure leaves the previous credentials in place.
_ENV_SNAPSHOT=""
if [ -f "$ENV_FILE" ]; then
  if grep -qs '^BOSS_BIND_ADDRESS=0.0.0.0$' "$ENV_FILE"; then
    ui_warn "Moving this install's API and web bind back to loopback; use an SSH tunnel or HTTPS reverse proxy."
  fi
  _existing() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }
  _keep_db="$(_existing BOSS_DB_PASSWORD)"
  _keep_api="$(_existing BOSS_API_KEY)"
  if [ -n "$_keep_db" ]; then
    BOSS_DB_PASSWORD="$_keep_db"
    ui_info "Reusing the existing database password — the data volume already expects it"
  fi
  if [ -n "$_keep_api" ]; then
    BOSS_API_KEY="$_keep_api"
    ui_info "Reusing the existing API key — saved keys keep working"
  fi
  _ENV_SNAPSHOT="$INSTALL_DIR/.env.previous.$$"
  cp -p "$ENV_FILE" "$_ENV_SNAPSHOT"
  chmod 600 "$_ENV_SNAPSHOT"
fi

# A bare host or host:port is accepted and completed, because "boss.example.internal" is what
# someone types and "http://boss.example.internal:8000" is what the browser needs.
if [ -n "$PUBLIC_URL" ]; then
  case "$PUBLIC_URL" in
    http://*|https://*) PUBLIC_API_URL="$PUBLIC_URL" ;;
    *:*)                PUBLIC_API_URL="http://$PUBLIC_URL" ;;
    *)                  PUBLIC_API_URL="http://$PUBLIC_URL:${API_PORT}" ;;
  esac
else
  PUBLIC_API_URL="http://127.0.0.1:${API_PORT}"
fi

_new_env="$INSTALL_DIR/.env.writing.$$"
cat > "$_new_env" <<EOF
# BOSS Community — generated by install.sh $(date -u +%Y-%m-%dT%H:%MZ)
# Keep this file private. Do not commit.

BOSS_EDITION=community
BOSS_REQUIRE_API_AUTH=true
BOSS_API_KEY=${BOSS_API_KEY}
BOSS_WORKER_API_KEY=${BOSS_WORKER_API_KEY}
BOSS_API_KEYS=${BOSS_API_KEY},${BOSS_WORKER_API_KEY}
BOSS_API_KEY_PRINCIPALS=${BOSS_API_KEY}:ceo,${BOSS_WORKER_API_KEY}:system
BOSS_WORKER_PRINCIPAL=system
BOSS_DB_PASSWORD=${BOSS_DB_PASSWORD}
BOSS_CONSOLE_PASSWORD=${BOSS_CONSOLE_PASSWORD}
BOSS_CONSOLE_SESSION_SECRET=${BOSS_CONSOLE_SESSION_SECRET}
BOSS_DEFAULT_ORG_ID=org_boss
BOSS_DEFAULT_WORKSPACE_ID=ws_boss
BOSS_DEFAULT_PROJECT_ID=boss-dogfood
BOSS_API_PORT=${API_PORT}
BOSS_WEB_PORT=${WEB_PORT}
BOSS_POSTGRES_PORT=${PG_PORT}
BOSS_COMMUNITY_WEB_ENABLED=${WITH_WEB}
BOSS_COMMUNITY_SECRET_STORE=${SECRET_STORE}
BOSS_BIND_ADDRESS=${BIND_ADDRESS}
NEXT_PUBLIC_BOSS_API_URL=${PUBLIC_API_URL}
NEXT_PUBLIC_BOSS_PROJECT_ID=boss-dogfood

# BYOK — your keys (Community never requires BOSS Cloud)
OPENAI_API_KEY=${OPENAI_KEY}
OPENAI_BASE_URL=
OPENAI_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
BOSS_GITHUB_TOKEN=${GITHUB_TOKEN}
# This installer source is not the operator's webhook repository. Leave the
# legacy default-project GitHub route unbound until explicitly configured.
BOSS_GITHUB_OWNER=
BOSS_GITHUB_REPO=

# Leave unset for pure Community independence
BOSS_ACCOUNT_CONNECT_URL=
EOF
chmod 600 "$_new_env"
if [ -n "${_ENV_SNAPSHOT:-}" ]; then
  if [ ! -f "$INSTALL_DIR/scripts/install/restore_preserved_env.sh" ]; then
    rm -f "$_new_env"
    die "scripts/install/restore_preserved_env.sh is missing — left the existing $ENV_FILE unchanged"
  fi
  if ! BOSS_PUBLIC_URL_EXPLICIT="$PUBLIC_URL_EXPLICIT" \
      BOSS_API_PORT_EXPLICIT="$API_PORT_EXPLICIT" \
      BOSS_WEB_PORT_EXPLICIT="$WEB_PORT_EXPLICIT" \
      BOSS_POSTGRES_PORT_EXPLICIT="$PG_PORT_EXPLICIT" \
      sh "$INSTALL_DIR/scripts/install/restore_preserved_env.sh" "$_ENV_SNAPSHOT" "$_new_env" </dev/null; then
    rm -f "$_new_env"
    die "could not carry credentials forward — left the existing $ENV_FILE unchanged (copy at $_ENV_SNAPSHOT)"
  fi
  rm -f "$_ENV_SNAPSHOT"
  _ENV_SNAPSHOT=""
  ui_info "Carried existing credentials forward — a re-run must not strand them"
fi
if [ ! -f "$INSTALL_DIR/scripts/install/reconcile_api_credentials.py" ]; then
  rm -f "$_new_env"
  die "scripts/install/reconcile_api_credentials.py is missing — left the existing $ENV_FILE unchanged"
fi
if ! python3 "$INSTALL_DIR/scripts/install/reconcile_api_credentials.py" "$_new_env" </dev/null; then
  rm -f "$_new_env"
  die "could not bind separate owner and worker keys — left the existing $ENV_FILE unchanged"
fi
mv "$_new_env" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ui_ok "Wrote $ENV_FILE (mode 600)"

# The preserved .env may hold ports from a previous run. Keep probes and printed URLs on
# the same ports Compose will use; never source a credential file as shell code.
env_value() { grep -m 1 "^$1=" "$ENV_FILE" | cut -d= -f2-; }
API_PORT="$(env_value BOSS_API_PORT)"
WEB_PORT="$(env_value BOSS_WEB_PORT)"
PG_PORT="$(env_value BOSS_POSTGRES_PORT)"
PUBLIC_API_URL="$(env_value NEXT_PUBLIC_BOSS_API_URL)"

# Local helper for humans
cat > "$INSTALL_DIR/boss-community.env.sh" <<EOF
# Source me:  source $INSTALL_DIR/boss-community.env.sh
export BOSS_API_URL="http://127.0.0.1:${API_PORT}"
export BOSS_API_KEY="${BOSS_API_KEY}"
export BOSS_PROJECT_ID="boss-dogfood"
export BOSS_EDITION="community"
EOF
chmod 600 "$INSTALL_DIR/boss-community.env.sh"

# ---- build & start ---------------------------------------------------------
# THE TWO KEYS INFISICAL REFUSES TO START WITHOUT.
#
# scripts/install/gen_infisical_keys.sh has existed since the secret store was bundled and
# nothing called it, so every fresh install died here:
#
#   error while interpolating services.infisical.environment.AUTH_SECRET:
#   required variable INFISICAL_AUTH_SECRET is missing a value
#
# They have no defaults on purpose — a shipped default encryption key is the same key on every
# installation that forgot to change it — so they are generated per install, once. Regenerating
# ENCRYPTION_KEY would strand every secret already stored under the old one, which is why the
# generator refuses when the value is already there.
if ! grep -qs '^INFISICAL_ENCRYPTION_KEY=' "$ENV_FILE"; then
  if [ -x "$INSTALL_DIR/scripts/install/gen_infisical_keys.sh" ]; then
    sh "$INSTALL_DIR/scripts/install/gen_infisical_keys.sh" "$ENV_FILE" </dev/null >/dev/null 2>&1 \
      && ui_ok "Generated the secret-store keys (back up $ENV_FILE — they are unrecoverable)"
  else
    { echo "INFISICAL_ENCRYPTION_KEY=$(openssl rand -hex 16)"
      echo "INFISICAL_AUTH_SECRET=$(openssl rand -base64 32)"; } >> "$ENV_FILE"
    ui_ok "Generated the secret-store keys (back up $ENV_FILE — they are unrecoverable)"
  fi
fi

ui_step "Building and starting Community stack (this can take several minutes the first time)"
ui_info "Services: Postgres · API · worker$( [[ $WITH_WEB -eq 1 ]] && echo ' · Mission Control' )"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-boss-community}"
# shellcheck disable=SC2086
${COMPOSE[@]} \
  -f scripts/install/community-compose.yml \
  --env-file "$ENV_FILE" \
  $COMPOSE_PROFILES \
  build </dev/null

# A rerun/upgrade may already have a worker polling the old API. Keep it up
# during the image build, then pause it before changing the API or schema.
# On a first install there is no worker to stop.
# shellcheck disable=SC2086
RUNNING_SERVICES="$(${COMPOSE[@]} \
  -f scripts/install/community-compose.yml \
  --env-file "$ENV_FILE" \
  $COMPOSE_PROFILES \
  ps --status running --services </dev/null)"
if printf '%s\n' "$RUNNING_SERVICES" | grep -qx worker; then
  ui_info "Pausing the existing worker until the project is seeded"
  # shellcheck disable=SC2086
  ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
    $COMPOSE_PROFILES stop worker </dev/null
fi

# Start only the database and API for bootstrap. A fresh worker cannot poll
# before the default project and its actor memberships exist.
# shellcheck disable=SC2086
${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
  $COMPOSE_PROFILES up -d --no-build db api </dev/null
ui_ok "Database and API started"

# The store has to be PROVISIONED, not merely running. `infisical bootstrap` returns the
# machine-identity token exactly once, so bootstrap_infisical.sh writes it to .env before doing
# anything else — re-running against an already-bootstrapped instance returns nothing.
if [[ "$SECRET_STORE" == "infisical" ]]; then
  ui_step "Provisioning the secret store"
  # Infisical needs its own services before bootstrap, but the worker must
  # remain stopped until the project seed below has succeeded.
  # shellcheck disable=SC2086
  ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
    $COMPOSE_PROFILES up -d --no-build infisical-redis infisical </dev/null
  if [[ -f "$INSTALL_DIR/scripts/install/bootstrap_infisical.sh" ]]; then
    if COMPOSE_FILE=scripts/install/community-compose.yml \
       BOSS_DOCKER_USE_SUDO="$(if [[ "${DOCKER[0]}" == sudo ]]; then echo 1; else echo 0; fi)" \
       sh "$INSTALL_DIR/scripts/install/bootstrap_infisical.sh" "$ENV_FILE" </dev/null >/dev/null 2>&1; then
      # THE VALUES ARRIVE AFTER THE CONTAINERS DID.
      #
      # bootstrap writes BOSS_INFISICAL_URL / _PROJECT_ID / _TOKEN into .env, and compose reads
      # .env when it CREATES a container. API was created before those values existed, so
      # recreate it. The worker has not started yet and will read them after seeding.
      # shellcheck disable=SC2086
      ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
        $COMPOSE_PROFILES up -d --no-deps --no-build --force-recreate api </dev/null >/dev/null 2>&1 \
        || die "could not restart API with the provisioned secret store; installation is incomplete"
      ui_ok "Secret store provisioned — BOSS will reference credentials rather than hold them"
    else
      die "The selected secret store did not provision. BOSS may be running with database fallback; installation is incomplete. Retry: sh scripts/install/bootstrap_infisical.sh $ENV_FILE"
    fi
  else
    die "bootstrap_infisical.sh is missing; selected secret store cannot be provisioned"
  fi
fi

# ---- wait health -----------------------------------------------------------
ui_step "Waiting for API health"

API_URL="http://127.0.0.1:${API_PORT}"
HEALTH_OK=0
for i in $(seq 1 60); do
  if curl -fsS "${API_URL}/health" >/dev/null 2>&1; then
    HEALTH_OK=1
    break
  fi
  sleep 3
  if [[ $((i % 5)) -eq 0 ]]; then
    ui_info "still waiting… (${i}/60)"
  fi
done
if [[ "$HEALTH_OK" -ne 1 ]]; then
  ui_err "API did not become healthy in time."
  echo ""
  echo "Debug:"
  echo "  ${COMPOSE[*]} -f scripts/install/community-compose.yml --env-file $ENV_FILE logs api --tail 80"
  exit 1
fi
ui_ok "API healthy at ${API_URL}/health"

# ---- seed ------------------------------------------------------------------
ui_step "Seeding Community project (roles, packs, demo project)"
SEED_ERR="$(mktemp)"
set +e
${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" exec -T api \
  python -c "from boss_engine.bootstrap import seed; seed()" </dev/null 2>"$SEED_ERR"
SEED_RC=$?
set -e
if [[ "$SEED_RC" -ne 0 ]]; then
  ui_warn "Seed returned $SEED_RC — trying alternate entrypoint"
  ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" exec -T api \
    python -m boss_engine.bootstrap </dev/null 2>>"$SEED_ERR" ||
    die "Seed failed. API may be up but the project is not ready; see $SEED_ERR."
else
  ui_ok "Database seeded (project boss-dogfood)"
fi
rm -f "$SEED_ERR"

# Start the worker and optional console only after the project and actors are
# available. The same order applies to a first install, rerun, and upgrade.
# shellcheck disable=SC2086
${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
  $COMPOSE_PROFILES up -d --no-build </dev/null
if [[ "$WITH_WEB" -eq 0 ]]; then
  # Switching an existing install to headless must stop a previously running
  # console. Compose does not stop services omitted from the active profile.
  ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
    --profile web stop web </dev/null
fi
ui_ok "Containers started"

# ---- post checks -----------------------------------------------------------
ui_step "Verifying Community independence"

set +e
EDITION_BODY="$(mktemp)"
EDITION_CODE="$(curl -sS -o "$EDITION_BODY" -w '%{http_code}' \
  -H "Authorization: Bearer ${BOSS_API_KEY}" \
  "${API_URL}/v1/edition?project_id=boss-dogfood" 2>/dev/null || echo 000)"
set -e
if [[ "$EDITION_CODE" == "200" ]]; then
  ui_ok "GET /v1/edition → 200"
  grep -q '"community"' "$EDITION_BODY" ||
    die "Edition response did not report Community; see $EDITION_BODY."
  ui_ok "Edition reports community"
else
  die "Edition probe returned HTTP $EDITION_CODE. BOSS was started but is not ready; see $EDITION_BODY."
fi
rm -f "$EDITION_BODY"

if [[ "$DISCOVER_SUBSCRIPTIONS" -eq 1 ]]; then
  ui_step "Checking coding subscription connections"
  if [[ "$CONNECT_CLAUDE" -eq 1 || "$CONNECT_CLAUDE_LOCAL" -eq 1 ]] &&
     [[ -n "$(env_value ANTHROPIC_API_KEY)" ]]; then
    die "Claude subscription connection would use ANTHROPIC_API_KEY for headless work. Remove that key from .env, then rerun the selected Claude connection option."
  fi
  if [[ "$CONNECT_CLAUDE_LOCAL" -eq 1 ]] && [[ -n "$(env_value CLAUDE_CODE_OAUTH_TOKEN)" ]]; then
    die "The CLI-owned Claude login requires an empty CLAUDE_CODE_OAUTH_TOKEN. Remove the legacy token from .env first."
  fi
  _subscription_args=(--api-url "$API_URL")
  [[ "$CONNECT_CODEX" -eq 1 ]] && _subscription_args+=(--connect-codex)
  [[ "$CONNECT_CLAUDE" -eq 1 ]] && _subscription_args+=(--connect-claude)
  [[ "$CONNECT_CLAUDE_LOCAL" -eq 1 ]] && _subscription_args+=(--connect-claude-local)
  [[ "$ACCEPT_DATABASE_FALLBACK" -eq 1 ]] && _subscription_args+=(--accept-database-fallback)
  BOSS_API_KEY="$BOSS_API_KEY" python3 scripts/install/coding_subscriptions.py "${_subscription_args[@]}" </dev/null ||
    die "Coding subscription discovery/connection failed; BOSS core remains available."
fi

set +e
PROV_CODE="$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${BOSS_API_KEY}" \
  "${API_URL}/v1/providers?project_id=boss-dogfood" 2>/dev/null || echo 000)"
set -e
if [[ "$PROV_CODE" == "200" ]]; then
  ui_ok "ProviderOps surface reachable (Community kernel)"
else
  ui_warn "Providers probe HTTP $PROV_CODE"
fi

# ---- success card ----------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}────────────────────────────────────────────────────────${NC}"
echo -e "${GREEN}${BOLD}  BOSS Community is running.${NC}"
echo -e "${GREEN}${BOLD}────────────────────────────────────────────────────────${NC}"
echo ""
echo -e "  ${BOLD}API${NC}              ${API_URL}"
echo -e "  ${BOLD}Health${NC}           ${API_URL}/health"
if [[ "$WITH_WEB" -eq 1 ]]; then
  echo -e "  ${BOLD}Mission Control${NC}  http://127.0.0.1:${WEB_PORT}"
  echo -e "  ${BOLD}Console password${NC} python3 \"$INSTALL_DIR/scripts/install/console_password.py\" show --env-file \"$ENV_FILE\""
fi
echo -e "  ${BOLD}Project${NC}          boss-dogfood"
echo -e "  ${BOLD}API key${NC}          (in $ENV_FILE — not printed)"
echo -e "  ${BOLD}Install dir${NC}      $INSTALL_DIR"
echo ""
echo -e "${BOLD}Quick commands${NC}"
echo ""
echo "  # Load env for this shell"
echo "  source $INSTALL_DIR/boss-community.env.sh"
echo ""
echo "  # Health check"
echo "  curl -sS -H \"Authorization: Bearer \$BOSS_API_KEY\" \$BOSS_API_URL/health"
echo ""
echo "  # Logs"
echo "  cd $INSTALL_DIR && docker compose -f scripts/install/community-compose.yml --env-file .env logs -f"
echo ""
echo "  # Stop"
echo "  cd $INSTALL_DIR && docker compose -f scripts/install/community-compose.yml --env-file .env down"
echo ""
echo -e "${MUTED}Docs: https://github.com/seanventures/boss#readme${NC}"
echo -e "${MUTED}Features: docs/community/FEATURES.md${NC}"
echo -e "${MUTED}Model paths: docs/community/MODEL_CONNECTIONS.md${NC}"
echo -e "${MUTED}Cloud is optional — Easy Connect later via Mission Control Editions.${NC}"
echo ""
