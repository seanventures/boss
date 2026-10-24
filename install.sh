#!/usr/bin/env bash
# =============================================================================
# BOSS Community — one-line installer
#
#   The release bootstrap (scripts/install/bootstrap.sh) finds the newest release and runs
#   this file from it. See docs/community/QUICKSTART.md.
#
# Checks the machine, installs what it can, picks defaults (~/boss, the database
# secret store, Mission Control on), starts Postgres + API + worker, seeds the
# project, verifies health, finds model keys and subscriptions already on this
# machine, and installs the `boss` command. No BOSS Cloud required.
# =============================================================================
set -euo pipefail
umask 077

BOSS_INSTALL_VERSION="1.2.0"
# An explicit URL still clones that remote (tests and the published one-liner).
# Unset means: if this script is already inside a BOSS checkout, install that
# tree. Do not require the unpublished GitHub repository.
REPO_URL_EXPLICIT=0
if [[ -n "${BOSS_REPO_URL:-}" ]]; then
  REPO_URL_EXPLICIT=1
fi
REF_EXPLICIT=0
if [[ -n "${BOSS_REPO_REF:-}" ]]; then
  REF_EXPLICIT=1
fi
BOSS_REPO_URL="${BOSS_REPO_URL:-https://github.com/seanventures/boss.git}"
# Unset is resolved after the flags: the ref this install recorded last time, else the
# newest published release. main is the last resort, never a silent default on a rerun.
BOSS_REPO_REF="${BOSS_REPO_REF:-}"
# Where the chosen ref came from: explicit, remembered, release, local, or default.
REF_SOURCE=""
[[ "$REF_EXPLICIT" -eq 0 ]] || REF_SOURCE="explicit"
RELEASE_STATE_NAME=".boss-release"

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
CURRENT_STEP=""
ui_step()  { CURRENT_STEP="$*"; echo -e "${CYAN}${BOLD}==>${NC}${BOLD} $*${NC}"; }
ui_ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
ui_warn()  { echo -e "  ${AMBER}!${NC} $*"; }
ui_err()   { echo -e "  ${RED}✗${NC} $*" >&2; }
ui_info()  { echo -e "  ${MUTED}•${NC} $*"; }
die()      { ui_err "$*"; exit 1; }

# stdout is the normalized origin. stderr is one reason, and the status is the script's.
normalize_console_origin() {
  if [[ ! -f "$CONSOLE_ORIGIN_PY" ]]; then
    echo "scripts/install/console_origin.py is missing" >&2
    return 1
  fi
  python3 "$CONSOLE_ORIGIN_PY" "$1"
}

# stdout is the address Compose can publish. Hostnames and blanks are rejected.
# IPv6 is wrapped in brackets so the host:port:container mapping stays unambiguous.
normalize_api_bind() {
  python3 -c '
import ipaddress, sys
raw = sys.argv[1].strip()
if not raw:
    sys.stderr.write("API bind must be 127.0.0.1, 0.0.0.0, or a single IPv4 or IPv6 address\n")
    raise SystemExit(2)
text = raw
if text.startswith("[") and text.endswith("]") and ":" in text[1:-1]:
    text = text[1:-1]
try:
    parsed = ipaddress.ip_address(text)
except ValueError:
    sys.stderr.write("API bind must be 127.0.0.1, 0.0.0.0, or a single IPv4 or IPv6 address\n")
    raise SystemExit(2)
if isinstance(parsed, ipaddress.IPv6Address):
    sys.stdout.write("[%s]\n" % parsed)
else:
    sys.stdout.write("%s\n" % parsed)
' "$1"
}

# Where a client on this machine opens the API. Publishing 0.0.0.0 still answers
# on loopback. A specific address does not.
local_api_url() {
  local bind="$1" port="$2"
  case "$bind" in
    ""|127.0.0.1|0.0.0.0)
      printf 'http://127.0.0.1:%s\n' "$port" ;;
    \[*)
      printf 'http://%s:%s\n' "$bind" "$port" ;;
    *:*)
      printf 'http://[%s]:%s\n' "$bind" "$port" ;;
    *)
      printf 'http://%s:%s\n' "$bind" "$port" ;;
  esac
}

# ---- defaults / flags ------------------------------------------------------
INSTALL_DIR="${BOSS_INSTALL_DIR:-$HOME/boss}"
BACKUP_DIR=""
WITH_WEB=1
WEB_EXPLICIT=0
# THE LOCAL TEST INBOX (Mailpit) is OFF unless the operator asks: --with-mailpit, or
# BOSS_EMAIL_DEV_INBOX=1 in the environment. Every install brings its own mail service (EMAIL.md).
WITH_MAILPIT="${BOSS_EMAIL_DEV_INBOX:-0}"
case "$WITH_MAILPIT" in 1|true|yes|on) WITH_MAILPIT=1 ;; *) WITH_MAILPIT=0 ;; esac
MAILPIT_EXPLICIT=0
[[ -n "${BOSS_EMAIL_DEV_INBOX:-}" ]] && MAILPIT_EXPLICIT=1
NONINTERACTIVE=0
SKIP_MODEL_KEY=0
OPENAI_KEY_PRESET="${OPENAI_API_KEY:-}"
ANTHROPIC_KEY_PRESET="${ANTHROPIC_API_KEY:-}"
GOOGLE_KEY_PRESET="${GOOGLE_API_KEY:-}"
XAI_KEY_PRESET="${XAI_API_KEY:-}"
OPENROUTER_KEY_PRESET="${OPENROUTER_API_KEY:-}"
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
# Hostname the browser will use for Mission Control. Empty means loopback only.
# The web listener stays on 127.0.0.1 either way. A previous .env keeps its origin
# unless this run passed --console-origin or BOSS_CONSOLE_PUBLIC_ORIGIN.
CONSOLE_ORIGIN="${BOSS_CONSOLE_PUBLIC_ORIGIN:-}"
CONSOLE_ORIGIN_EXPLICIT=0
CONSOLE_ORIGIN_FROM_FLAG=0
if [ -n "${BOSS_CONSOLE_PUBLIC_ORIGIN:-}" ]; then
  CONSOLE_ORIGIN_EXPLICIT=1
fi
# Host address published for the API. Web, Postgres, and Infisical stay on loopback.
# Unset means 127.0.0.1, and a rerun keeps a previous bind unless this run set one.
API_BIND="127.0.0.1"
API_BIND_EXPLICIT=0
API_BIND_FROM_FLAG=0
if [ "${BOSS_API_BIND+x}" = x ]; then
  API_BIND="${BOSS_API_BIND}"
  API_BIND_EXPLICIT=1
fi
_INSTALLER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONSOLE_ORIGIN_PY="${_INSTALLER_ROOT}/scripts/install/console_origin.py"
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
CONNECT_GROK_LOCAL=0
INSTALL_CODING_CLIS=0
ACCEPT_DATABASE_FALLBACK=0
# Look for model keys and subscriptions already on this machine after the stack is healthy.
# Finding is read only. Copying a login into BOSS needs a yes at the prompt, or --yes
# together with --connect-found.
AUTO_DISCOVER=1
CONNECT_FOUND=0
# The local model (Ollama). Offered at a terminal when no model is usable; --install-ollama
# sets it up unattended; --no-local-model never offers it.
INSTALL_OLLAMA=0
LOCAL_MODEL=""
NO_LOCAL_MODEL=0
REBIND_OLLAMA=0
INSTALL_BOSS_COMMAND=1
SECRET_STORE_EXPLICIT=0
if [ -n "${BOSS_SECRET_STORE:-}" ]; then
  SECRET_STORE_EXPLICIT=1
fi

usage() {
  cat <<EOF
BOSS Community installer

Usage:
  curl -fsSL <website>/install.sh | bash        (see docs/community/QUICKSTART.md)
  ./install.sh [options]

Every option is optional. Defaults: ~/boss, the newest release (a rerun keeps the release
it installed), the database secret store, Mission Control on, and loopback-only listeners.

After install, the \`boss\` command runs the lifecycle from any directory:
  boss status | open | password | doctor | logs | connect | upgrade | update | uninstall

Options:
  --dir PATH              Install directory (default: ~/boss)
  --yes, -y               Never ask a question. Starts without a model key if none is found
  --no-web                Skip Mission Control web console
  --web                   Enable Mission Control on an install previously made without it
  --with-mailpit          Add a local test inbox (Mailpit): mail stays on this machine. Off by
                          default; also BOSS_EMAIL_DEV_INBOX=1. --no-mailpit turns it off again
  --public-url URL        Browser-reachable API URL when using a reverse proxy
  --console-origin URL    HTTPS origin for Mission Control (DNS hostname, no path).
                          The web listener stays on 127.0.0.1. See docs/community/hostname.md
  --api-bind ADDRESS      Address that accepts API connections (default 127.0.0.1).
                          127.0.0.1, 0.0.0.0, or one IPv4/IPv6 address. See docs/community/hostname.md
  --api-port N            Host port for API (default 8000)
  --web-port N            Host port for web (default 3000)
  --postgres-port N       Host port for Postgres (default 5432)
  --ref REF               Release tag, branch, or full commit SHA to install. Default: the
                          ref this install recorded in .boss-release, else the newest
                          release tag. Recorded for reruns and \`boss upgrade\`
  --backup-dir PATH       Private recovery backup location for an upgrade
  --skip-model-key        Do not ask for a model key (API starts; model calls fail closed)
  --no-install-deps       Do not install missing VM packages
  --connect-found         Connect what was found on this machine without asking: a Codex
                          login, a Claude Code or Grok Build sign-in inside the BOSS
                          container, and Ollama. Needed for --yes to connect anything.
                          A sign-in needs a person at a terminal and is skipped without
                          one; a Codex login on the database store also needs
                          --accept-database-fallback
  --install-ollama        Set up a free local model without asking: install Ollama if
                          missing, start it, make it reachable from the BOSS containers,
                          pull the model, connect it. When no hosted model is connected it
                          becomes every role's model. At a terminal BOSS offers this when
                          no model is usable; --yes alone never installs it
  --local-model NAME      The Ollama model to pull (default qwen2.5-coder:7b)
  --no-local-model        Never offer the local model
  --rebind-ollama         Allow --install-ollama to move an Ollama you already had from
                          127.0.0.1 to the Docker bridge address (Linux). Without it an
                          unattended run leaves an existing Ollama alone and says how
  --no-discover           Do not look for model keys and subscriptions on this machine
  --no-boss-command       Do not install the \`boss\` command on PATH
  --discover-subscriptions  Check Claude, Codex and Grok CLIs on this VM (no credential transfer)
  --install-coding-clis  Install missing official Claude, Codex and Grok coding CLIs on this VM
  --connect-codex        Explicitly import this VM user's Codex ChatGPT login into BOSS
  --connect-claude       Import an operator-supplied Claude setup token into the local secret store
  --connect-claude-local Record the CLI-owned Claude login made inside the API container
  --connect-grok-local   Record the CLI-owned Grok login made inside the API container
  --accept-database-fallback  Allow a subscription credential in Postgres if no secret store
  --secret-store WHICH    database (default) or infisical. Both stay on your own machine:
                            database   4 containers. Credentials are held by BOSS's own
                                       Postgres. Lean; storing a SUBSCRIPTION token asks you
                                       to confirm the database is an acceptable home for it.
                            infisical  6 containers, ~400MB more. Bundled Infisical secret
                                       store, bootstrapped and wired up for you. Credentials
                                       are referenced rather than held, and rotate cleanly.
  -h, --help              Show this help

Web, Postgres, and the optional secret store bind to 127.0.0.1.
The API binds to 127.0.0.1 unless you pass --api-bind. Authentication stays required.
Console hostname and API bind are documented in docs/community/hostname.md.
--bind-address is not a way to publish ports.

Environment overrides:
  BOSS_INSTALL_DIR, OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY, XAI_API_KEY,
  OPENROUTER_API_KEY, BOSS_GITHUB_TOKEN, BOSS_REPO_URL, BOSS_REPO_REF, BOSS_API_PORT,
  BOSS_WEB_PORT, BOSS_POSTGRES_PORT, BOSS_PUBLIC_URL, BOSS_CONSOLE_PUBLIC_ORIGIN,
  BOSS_API_BIND, BOSS_SECRET_STORE, BOSS_BIN_DIR (where \`boss\` goes)

  Do not pass API keys or tokens as arguments. The shell saves them in history
  and process listings. Export them, or paste the OpenAI key at the hidden prompt.
  An exported provider key is saved to the private .env (never printed).

Examples:
  ./install.sh --dir "\$PWD" --yes --skip-model-key
  ./install.sh --dir "\$PWD" --ref REVIEWED_COMMIT --backup-dir "\$HOME/boss-backups"
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --yes|-y) NONINTERACTIVE=1; shift ;;
    --no-install-deps) INSTALL_DEPS=0; shift ;;
    --discover-subscriptions) DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --install-coding-clis) INSTALL_CODING_CLIS=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --connect-codex) CONNECT_CODEX=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --connect-claude) CONNECT_CLAUDE=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --connect-claude-local) CONNECT_CLAUDE_LOCAL=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --connect-grok-local) CONNECT_GROK_LOCAL=1; DISCOVER_SUBSCRIPTIONS=1; shift ;;
    --accept-database-fallback) ACCEPT_DATABASE_FALLBACK=1; shift ;;
    --connect-found) CONNECT_FOUND=1; shift ;;
    --install-ollama) INSTALL_OLLAMA=1; shift ;;
    --local-model)
      LOCAL_MODEL="${2:-}"
      [[ "$LOCAL_MODEL" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$ ]] ||
        die "--local-model takes an Ollama model name, for example qwen2.5-coder:7b"
      shift 2 ;;
    --no-local-model) NO_LOCAL_MODEL=1; shift ;;
    --rebind-ollama) REBIND_OLLAMA=1; shift ;;
    --no-discover) AUTO_DISCOVER=0; shift ;;
    --no-boss-command) INSTALL_BOSS_COMMAND=0; shift ;;
    --openai-key|--anthropic-key|--github-token|--openai-key=*|--anthropic-key=*|--github-token=*)
      die "Refusing a secret on the command line. It would be saved in shell history and process listings. Export OPENAI_API_KEY, ANTHROPIC_API_KEY, or BOSS_GITHUB_TOKEN, or paste the OpenAI key at the hidden prompt." ;;
    --no-web) WITH_WEB=0; WEB_EXPLICIT=1; shift ;;
    --web) WITH_WEB=1; WEB_EXPLICIT=1; shift ;;
    --with-mailpit) WITH_MAILPIT=1; MAILPIT_EXPLICIT=1; shift ;;
    --no-mailpit) WITH_MAILPIT=0; MAILPIT_EXPLICIT=1; shift ;;
    --public-url) PUBLIC_URL="$2"; PUBLIC_URL_EXPLICIT=1; shift 2 ;;
    --console-origin)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "pass one https origin" >&2
        exit 2
      fi
      CONSOLE_ORIGIN="$(normalize_console_origin "$2")" || exit $?
      CONSOLE_ORIGIN_EXPLICIT=1
      CONSOLE_ORIGIN_FROM_FLAG=1
      shift 2 ;;
    --api-bind)
      if [[ $# -lt 2 ]]; then
        echo "API bind must be 127.0.0.1, 0.0.0.0, or a single IPv4 or IPv6 address" >&2
        exit 2
      fi
      API_BIND="$(normalize_api_bind "$2")" || exit $?
      API_BIND_EXPLICIT=1
      API_BIND_FROM_FLAG=1
      shift 2 ;;
    --bind-address) die "--bind-address is no longer supported; Community binds API/web to loopback. Use an SSH tunnel or HTTPS reverse proxy." ;;
    --api-port) API_PORT="$2"; API_PORT_EXPLICIT=1; shift 2 ;;
    --web-port) WEB_PORT="$2"; WEB_PORT_EXPLICIT=1; shift 2 ;;
    --postgres-port) PG_PORT="$2"; PG_PORT_EXPLICIT=1; shift 2 ;;
    --ref) BOSS_REPO_REF="$2"; REF_EXPLICIT=1; REF_SOURCE="explicit"; shift 2 ;;
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
if [[ "$CONNECT_CLAUDE" -eq 1 && "$CONNECT_CLAUDE_LOCAL" -eq 1 ]]; then
  die "Choose --connect-claude or --connect-claude-local, not both."
fi
if [[ "$CONNECT_CLAUDE" -eq 1 && "$NONINTERACTIVE" -eq 1 && -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
  die "No Claude setup token was provided. Set CLAUDE_CODE_OAUTH_TOKEN for this command, or run it in a terminal and paste the token from \`claude setup-token\`. Nothing was stored."
fi

if [[ -n "${BOSS_PREUPGRADE_BACKUP_DIR:-}" ]]; then
  ui_warn 'Ignoring a legacy pre-upgrade backup path; this installer will make its own fresh snapshot before updating source.'
fi

# A value inherited from the environment is explicit, same as the flag.
# Checked before the banner so a bad origin or bind never starts an install.
# A flag already normalized above wins over the environment.
if [[ "$CONSOLE_ORIGIN_FROM_FLAG" -eq 0 && -n "$CONSOLE_ORIGIN" ]]; then
  CONSOLE_ORIGIN="$(normalize_console_origin "$CONSOLE_ORIGIN")" || exit $?
fi
if [[ "$API_BIND_FROM_FLAG" -eq 0 && "$API_BIND_EXPLICIT" -eq 1 ]]; then
  API_BIND="$(normalize_api_bind "$API_BIND")" || exit $?
fi

# A ref reaches git as an argument. Refuse option-looking or odd names before any action.
ref_is_valid() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$1" != *..* && "$1" != *.lock ]]
}
if [[ "$REF_EXPLICIT" -eq 1 ]] && ! ref_is_valid "$BOSS_REPO_REF"; then
  die "--ref must be a release tag, branch name, or full commit SHA."
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
fi
if [[ "$CONNECT_CLAUDE" -eq 1 && "$NONINTERACTIVE" -eq 1 && -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
  die "No Claude setup token was provided. Set CLAUDE_CODE_OAUTH_TOKEN for this command, or run it in a terminal and paste the token from \`claude setup-token\`. Nothing was stored."
fi

# ask_yes "Question" -> status 0 only for an explicit yes typed at the terminal.
# Non-interactive runs answer no; --connect-found is the only unattended yes.
ask_yes() {
  local reply=""
  [[ "$NONINTERACTIVE" -eq 0 ]] || return 1
  read -r -p "$(echo -e "  ${BOLD}$1${NC} ${MUTED}[y/N]${NC} ")" reply </dev/tty || return 1
  case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
    y|yes) return 0 ;;
  esac
  return 1
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

# Every failure after this point names the step it stopped in. The status is unchanged.
on_exit() {
  local rc=$?
  if [[ "$rc" -ne 0 && -n "$CURRENT_STEP" ]]; then
    echo -e "  ${RED}✗${NC} Stopped during: ${CURRENT_STEP}" >&2
    echo -e "    Nothing was deleted. Fix the cause above, then run the same command again." >&2
  fi
}
trap on_exit EXIT

ui_step "Checking your machine"

OS="$(uname -s 2>/dev/null || echo unknown)"
ARCH="$(uname -m 2>/dev/null || echo unknown)"
ui_info "OS: $OS  arch: $ARCH"

install_vm_deps() {
  [[ "$INSTALL_DEPS" -eq 1 ]] || die "Missing dependencies. Remove --no-install-deps to install them."
  if [[ "$OS" == Darwin ]]; then
    need_cmd git || ui_err "git is missing. Install Apple's command line tools: xcode-select --install"
    need_cmd docker || ui_err "Docker is missing. Install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/"
    need_cmd python3 || ui_err "Python 3 is missing. xcode-select --install provides it."
    die "Install the items above, start Docker Desktop, then run the same install command again."
  fi
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
    # A person at a terminal can type the sudo password once; --yes never prompts.
    if [[ "$NONINTERACTIVE" -eq 0 ]] && ! sudo -n true 2>/dev/null; then
      ui_info "Installing git, Python 3 and Docker needs administrator access. sudo asks for your password once."
      sudo -v </dev/tty || die "Administrator access is needed to install missing packages. Nothing was changed."
    fi
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

if ! docker info >/dev/null 2>&1; then
  boss_sudo_scope_safe "$INSTALL_DIR/.env" || { echo "Cannot safely use sudo for this Docker selection. Use direct Docker access for custom projects, contexts or connection settings." >&2; exit 1; }
  if [[ "$OS" == Linux && "$(id -u)" -ne 0 ]] && need_cmd sudo && sudo -n docker --host unix:///var/run/docker.sock info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker --host unix:///var/run/docker.sock)
  elif [[ "$OS" == Linux && "$(id -u)" -ne 0 && "$NONINTERACTIVE" -eq 0 ]] && need_cmd sudo &&
       ui_info "Docker needs administrator access for this user. sudo may ask for your password." &&
       sudo -v </dev/tty && sudo -n docker --host unix:///var/run/docker.sock info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker --host unix:///var/run/docker.sock)
  else
    die "Docker daemon is unavailable. Start Docker and rerun; on Linux ensure noninteractive sudo access to Docker."
  fi
fi
ui_ok "Docker daemon is running"
if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  COMPOSE=("${DOCKER[@]}" compose)
elif [[ "${DOCKER[0]}" != sudo ]] && need_cmd docker-compose; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose v2 is required."
fi
ui_ok "Docker Compose: $("${COMPOSE[@]}" version 2>/dev/null | head -1)"
install_coding_clis

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
ui_step "Choosing settings"

# No questions for what has a safe default. --dir, --no-web and --secret-store change them.
ui_ok "Install dir: $INSTALL_DIR"

# ---- storage advice -------------------------------------------------------
# The checkout filesystem and the daemon's image store may be different disks.
# These are planning measurements, not an installation admission threshold.
report_disk_space() {
  local label="$1" path="$2" free_kb
  if ! need_cmd df || ! free_kb="$(df -Pk "$path" 2>/dev/null | awk 'NR==2{print $4}')" ||
     [[ ! "$free_kb" =~ ^[0-9]+$ ]]; then
    ui_warn "$label: free space could not be measured."
    return
  fi
  ui_info "$label ($path): $(awk -v kb="$free_kb" 'BEGIN {printf "%.1f", kb/1048576}') GiB free."
  if [[ "$free_kb" -lt 80000000 ]]; then
    ui_warn "$label has less than about 80 GB free; builds and agent work may need more space. This is advisory."
  fi
}
_disk_path="$INSTALL_DIR"
while [[ ! -e "$_disk_path" && "$_disk_path" != / && "$_disk_path" != . ]]; do
  _disk_path="$(dirname "$_disk_path")"
done
report_disk_space "Install filesystem" "$_disk_path"

# Never df a remote daemon's path on this host, or treat Desktop's host free
# space as free space inside its VM. An inaccessible local path is unknown too.
_docker_endpoint=""
if [[ -n "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" ]]; then
  _docker_endpoint="$DOCKER_HOST"
else
  _docker_endpoint="$("${DOCKER[@]}" context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
fi
_docker_root=""; _docker_os=""
if [[ "$OS" == Linux && "$_docker_endpoint" == unix://* ]]; then
  IFS='|' read -r _docker_root _docker_os <<< "$("${DOCKER[@]}" info --format '{{.DockerRootDir}}|{{.OperatingSystem}}' 2>/dev/null || true)"
fi
if [[ "$_docker_root" == /* && -d "$_docker_root" && -n "$_docker_os" && "$_docker_os" != *"Docker Desktop"* ]]; then
  report_disk_space "Docker image-store filesystem" "$_docker_root"
else
  ui_warn "Docker image-store free space is unknown here. Check the selected Docker host or Docker Desktop disk settings; install filesystem space is not Docker-store headroom."
fi
ui_info "About 100 GB of working capacity is recommended for images, cache and agent work; actual needs vary."
# ---- end storage advice ---------------------------------------------------

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
# A rerun keeps the test inbox only when the operator opted in before (the marker is written
# by the opt-in alone; an install that never asked keeps it off).
if [[ "$MAILPIT_EXPLICIT" -eq 0 && -f "$INSTALL_DIR/.env" ]] &&
   grep -q '^BOSS_EMAIL_DEV_INBOX_OPTED_IN=1$' "$INSTALL_DIR/.env" 2>/dev/null; then
  WITH_MAILPIT=1
fi

OPENAI_KEY="$OPENAI_KEY_PRESET"
ANTHROPIC_KEY="$ANTHROPIC_KEY_PRESET"
GOOGLE_KEY="$GOOGLE_KEY_PRESET"
XAI_KEY="$XAI_KEY_PRESET"
OPENROUTER_KEY="$OPENROUTER_KEY_PRESET"
# A key in the environment is used without asking: exporting it for this installer is the
# consent. Names only are printed; values go to the private .env.
_exported_keys=""
[[ -z "$OPENAI_KEY" ]] || _exported_keys="$_exported_keys OPENAI_API_KEY"
[[ -z "$ANTHROPIC_KEY" ]] || _exported_keys="$_exported_keys ANTHROPIC_API_KEY"
[[ -z "$GOOGLE_KEY" ]] || _exported_keys="$_exported_keys GOOGLE_API_KEY"
[[ -z "$XAI_KEY" ]] || _exported_keys="$_exported_keys XAI_API_KEY"
[[ -z "$OPENROUTER_KEY" ]] || _exported_keys="$_exported_keys OPENROUTER_API_KEY"
# The only question: a model key, and only when nothing was exported and a person is here.
# An existing install keeps its key (restore_preserved_env.sh), so a rerun does not ask.
if [[ -z "$_exported_keys" && "$SKIP_MODEL_KEY" -eq 0 && "$NONINTERACTIVE" -eq 0 ]] &&
   ! grep -qs '^OPENAI_API_KEY=.' "$INSTALL_DIR/.env"; then
  echo ""
  ui_info "BOSS uses your own model keys and subscriptions. It also looks for existing logins after setup."
  prompt_secret "OpenAI API key (optional, press Enter to skip)"
  OPENAI_KEY="${REPLY:-}"
  [[ -z "$OPENAI_KEY" ]] || _exported_keys=" OPENAI_API_KEY"
fi
if [[ -n "$_exported_keys" ]]; then
  ui_ok "Model keys received (values not printed):${_exported_keys}"
else
  ui_info "No model key yet. BOSS starts anyway; model calls fail closed until one is connected."
fi
GITHUB_TOKEN="$GITHUB_TOKEN_PRESET"

# THE CHOICE IS THE OPERATOR'S, because the right answer depends on their machine.
#
# install.sh used to generate INFISICAL_ENCRYPTION_KEY and then never start Infisical, so every
# install had those keys written and unused and no secret store at all — which only surfaced
# later, when storing a subscription token returned "no secret store". The installer no longer
# asks: the default is the database store, --secret-store infisical chooses the bundled one,
# and whichever way it goes the install matches what was chosen.
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

# THE BUNDLED PROMETHEUS AND GRAFANA come up with Mission Control (compose `web` profile):
# BOSS forwards every app's scraped series to Prometheus, and the console's Grafana and
# Dashboard links open the bundled Grafana on the loopback. Headless (--no-web) they are not
# started and BOSS's Postgres floor evaluates everything, so no address is written for them.
GRAFANA_PORT="${BOSS_GRAFANA_PORT:-$(grep -m 1 '^BOSS_GRAFANA_PORT=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)}"
GRAFANA_PORT="${GRAFANA_PORT:-3300}"
MON_PROMETHEUS_URL=""
MON_GRAFANA_URL=""
MON_GRAFANA_PUBLIC_URL=""
# THE LOCAL TEST INBOX (Mailpit, compose `mail` profile), only with --with-mailpit: the Email
# (SMTP) connector can then choose it, and mail lands there with nothing leaving the machine.
MAILPIT_PORT="${BOSS_MAILPIT_PORT:-$(grep -m 1 '^BOSS_MAILPIT_PORT=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- || true)}"
MAILPIT_PORT="${MAILPIT_PORT:-8025}"
MAIL_DEV_INBOX=""
MAIL_DEV_INBOX_URL=""
if [[ "$WITH_WEB" -eq 1 ]]; then
  COMPOSE_PROFILES="--profile web"
  ui_ok "Mission Control: yes (port $WEB_PORT)"
  MON_PROMETHEUS_URL="http://prometheus:9090"
  MON_GRAFANA_URL="http://grafana:3000"
  MON_GRAFANA_PUBLIC_URL="http://localhost:${GRAFANA_PORT}"
  ui_ok "Monitoring: bundled Prometheus and Grafana (Grafana on port $GRAFANA_PORT)"
  if port_in_use "$GRAFANA_PORT" && ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q 'grafana'; then
    ui_warn "Port $GRAFANA_PORT is already in use — set BOSS_GRAFANA_PORT to another port and re-run if Grafana does not start."
  fi
else
  ui_ok "Mission Control: skipped"
fi

if [[ "$WITH_MAILPIT" -eq 1 ]]; then
  COMPOSE_PROFILES="$COMPOSE_PROFILES --profile mail"
  MAIL_DEV_INBOX="mailpit:1025"
  MAIL_DEV_INBOX_URL="http://localhost:${MAILPIT_PORT}"
  ui_ok "Email: local test inbox (Mailpit, port $MAILPIT_PORT) — local only"
else
  ui_ok "Email: connect your mail server in Skills & MCP › Email (SMTP)"
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

# The directory that contains this installer, when it is already a BOSS checkout.
# A downloaded one-liner lives in a temp file and returns nothing here.
local_installer_tree() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$script_dir/scripts/install/community-compose.yml" ]] &&
     { [[ -d "$script_dir/.git" ]] || [[ -f "$script_dir/.git" ]]; }; then
    printf '%s\n' "$script_dir"
    return 0
  fi
  return 1
}

# Copy the checkout this script lives in. --no-local avoids hardlinks, which
# fail when the empty directory is on another volume than the source tree.
copy_local_checkout() {
  local src="$1" dest="$2" head
  head="$(git -C "$src" rev-parse HEAD)"
  ui_info "Copying this checkout ($head) into $dest"
  if [[ -e "$dest" ]]; then
    [[ -d "$dest" && -z "$(ls -A "$dest")" ]] ||
      die "$dest is not empty and is not a Git checkout. Choose --dir; nothing was removed."
    rmdir "$dest" || die "Could not use empty directory $dest."
  fi
  mkdir -p "$(dirname "$dest")"
  if ! git clone --no-local --no-checkout "$src" "$dest" </dev/null; then
    die "Could not copy this checkout into $dest."
  fi
  if ! git -C "$dest" checkout --detach "$head" </dev/null; then
    die "Could not check out $head in $dest."
  fi
}

clone_remote_source() {
  local dest="$1" empty_existing="$2"
  ui_info "Cloning $BOSS_REPO_URL ($BOSS_REPO_REF) → $dest"
  if [[ "$BOSS_REPO_REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
    if git clone --no-checkout "$BOSS_REPO_URL" "$dest" </dev/null &&
       git -C "$dest" checkout --detach "$BOSS_REPO_REF" </dev/null; then
      return 0
    fi
    if [[ "$empty_existing" -eq 1 ]]; then
      die "Exact-commit clone failed; existing directory was not removed."
    fi
    die "Exact-commit clone failed; check repo visibility, URL, and commit."
  fi
  if git -c advice.detachedHead=false clone --depth 1 --branch "$BOSS_REPO_REF" "$BOSS_REPO_URL" "$dest" </dev/null; then
    return 0
  fi
  if [[ "$empty_existing" -eq 1 ]]; then
    die "Clone failed; existing directory was not removed."
  fi
  die "Clone failed; check repo visibility, URL, and ref."
}

place_fresh_source() {
  local dest="$1" empty_existing="$2" src=""
  if [[ "$REPO_URL_EXPLICIT" -eq 0 ]] && src="$(local_installer_tree)"; then
    copy_local_checkout "$src" "$dest"
    return 0
  fi
  clone_remote_source "$dest" "$empty_existing"
}

# ---- which release ----------------------------------------------------------
# The same program as pick_release in scripts/install/upgrade.sh. This file runs before a
# checkout exists, so it cannot source that helper; a test keeps the copies identical.
# stdin: tag names. $1: 1 to consider prereleases. stdout: the newest tag or nothing.
pick_release() {
  # >>> boss_pick_release
  LC_ALL=C awk -v pre="${1:-0}" '
    /^v[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.]+)?$/ {
      core = substr($0, 2); label = ""
      cut = index(core, "-")
      if (cut > 0) { label = substr(core, cut + 1); core = substr(core, 1, cut - 1) }
      if (label != "" && pre != 1) next
      n = split(core, part, ".")
      if (n > 3) next
      # Numbers inside the label compare as numbers (rc10.4 after rc9, rc10.10 after rc10.9).
      key = ""
      while (match(label, /[0-9]+/)) {
        key = key substr(label, 1, RSTART - 1) sprintf("%09d", substr(label, RSTART, RLENGTH))
        label = substr(label, RSTART + RLENGTH)
      }
      label = key label
      printf "%09d.%09d.%09d.%s\t%s\n", part[1], (n >= 2 ? part[2] : 0), (n >= 3 ? part[3] : 0), (label == "" ? "~" : label), $0
    }' | LC_ALL=C sort | tail -n 1 | cut -f 2
  # <<< boss_pick_release
}
# The newest stable release; the newest prerelease only when no stable release exists.
newest_release() {
  local tags stable
  tags="$(cat)"
  stable="$(printf '%s\n' "$tags" | pick_release 0)"
  if [[ -n "$stable" ]]; then
    printf '%s\n' "$stable"
  else
    printf '%s\n' "$tags" | pick_release 1
  fi
}
state_value() {
  grep -m 1 "^$1=" "$INSTALL_DIR/$RELEASE_STATE_NAME" 2>/dev/null | cut -d= -f2- || true
}

# A RERUN MUST NOT MOVE A RELEASE INSTALL TO MAIN. BOSS_REPO_REF used to default to main and
# nothing recorded what was installed, so pasting the install line again (or running it with a
# new flag) fast-forwarded a v1.0 install to whatever main held that day. Order of precedence:
#   1. --ref / BOSS_REPO_REF
#   2. the ref .boss-release recorded on the last run
#   3. an older install: the release tag at HEAD, else a detached commit stays put, else main
#   4. a fresh install: the newest published release tag, else main with a warning
_src_tree=""
if [[ -z "$REF_SOURCE" ]]; then
  _remembered="$(state_value BOSS_RELEASE_REF)"
  if [[ -n "$_remembered" ]] && ref_is_valid "$_remembered"; then
    BOSS_REPO_REF="$_remembered"
    REF_SOURCE="remembered"
  elif [[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" ]]; then
    _head_tag="$(git -C "$INSTALL_DIR" tag --points-at HEAD 2>/dev/null | pick_release 1 || true)"
    if [[ -n "$_head_tag" ]]; then
      BOSS_REPO_REF="$_head_tag"
      REF_SOURCE="remembered"
    elif ! git -C "$INSTALL_DIR" symbolic-ref -q HEAD >/dev/null 2>&1 &&
         _head="$(git -C "$INSTALL_DIR" rev-parse --verify -q HEAD 2>/dev/null)"; then
      BOSS_REPO_REF="$_head"
      REF_SOURCE="remembered"
    else
      BOSS_REPO_REF="main"
      REF_SOURCE="default"
    fi
  elif [[ -f "$INSTALL_DIR/.boss-shipped-commit" ]] ||
       { [[ -d "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR")" ]]; }; then
    # A shipped tree has its source already; any other nonempty directory is refused below.
    BOSS_REPO_REF="main"
    REF_SOURCE="default"
  elif [[ "$REPO_URL_EXPLICIT" -eq 0 ]] && _src_tree="$(local_installer_tree)"; then
    BOSS_REPO_REF="main"
    REF_SOURCE="local"
  else
    ui_info "Looking up the newest published BOSS release"
    _tags="$(git ls-remote --tags --refs "$BOSS_REPO_URL" 'v*' </dev/null 2>/dev/null | sed 's#.*refs/tags/##' || true)"
    BOSS_REPO_REF="$(printf '%s\n' "$_tags" | newest_release)"
    if [[ -n "$BOSS_REPO_REF" ]]; then
      REF_SOURCE="release"
    else
      BOSS_REPO_REF="main"
      REF_SOURCE="default"
      ui_warn "No published release tag found at $BOSS_REPO_URL; installing main."
    fi
  fi
fi
case "$REF_SOURCE" in
  explicit) ui_ok "Release: $BOSS_REPO_REF (requested)" ;;
  remembered) ui_ok "Release: $BOSS_REPO_REF (kept from the last install; pass --ref to change)" ;;
  release) ui_ok "Release: $BOSS_REPO_REF (newest published)" ;;
  local) ui_ok "Release: this checkout" ;;
esac

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
  ui_info "Checking checkout at $INSTALL_DIR against $BOSS_REPO_REF"
  _installed_commit="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  _fetch_ok=0
  if git -C "$INSTALL_DIR" fetch origin "$BOSS_REPO_REF" </dev/null; then
    _fetch_ok=1
  fi
  if [[ "$_fetch_ok" -eq 1 ]]; then
    # ^{commit}: a release tag is an annotated tag object, not the commit HEAD names.
    _fetched_commit="$(git -C "$INSTALL_DIR" rev-parse 'FETCH_HEAD^{commit}')"
    if [[ "$_installed_commit" == "$_fetched_commit" ]]; then
      ui_info "Checkout already matches $BOSS_REPO_REF"
    elif git -C "$INSTALL_DIR" merge-base --is-ancestor "$_installed_commit" "$_fetched_commit"; then
      if [[ -f "$INSTALL_DIR/.env" ]]; then
        # A pasted install command is also an upgrade command for an existing checkout.
        # Snapshot the old database, artifacts and credentials before switching source:
        # the new API may migrate the database as soon as it starts. Always create
        # this run's own snapshot; a prior path could refer to stale data even if
        # its source commit happens to match.
        _backup_bundle="$(mktemp -d)"
        _backup_helper="$_backup_bundle/backup.sh"
        if ! git -C "$INSTALL_DIR" show "$_fetched_commit:scripts/install/backup.sh" > "$_backup_helper"; then
          rm -rf "$_backup_bundle"
          die "The fetched source has no backup helper; existing checkout left unchanged."
        fi
        # The fetched backup helper and archive validator are one versioned recovery boundary.
        # Older targets without the validator keep their self-contained backup helper.
        if git -C "$INSTALL_DIR" cat-file -e "$_fetched_commit:scripts/install/backup_archive.py" 2>/dev/null; then
          if ! git -C "$INSTALL_DIR" show "$_fetched_commit:scripts/install/backup_archive.py" > "$_backup_bundle/backup_archive.py"; then
            rm -rf "$_backup_bundle"
            die "Could not prepare the fetched recovery validator; existing checkout left unchanged."
          fi
        fi
        _backup_args=(--dir "$INSTALL_DIR")
        [[ -z "$BACKUP_DIR" ]] || _backup_args+=(--backup-dir "$BACKUP_DIR")
        if ! _recovery_backup="$(bash "$_backup_helper" "${_backup_args[@]}" </dev/null)"; then
          rm -rf "$_backup_bundle"
          die "Recovery backup failed; existing source, credentials and volumes were left unchanged. Check the backup error above, start the installed database if needed, then retry."
        fi
        rm -rf "$_backup_bundle"
        [[ -s "$_recovery_backup/.env" && -s "$_recovery_backup/boss.dump" &&
           -s "$_recovery_backup/artifacts.tar" &&
           "$(cat "$_recovery_backup/source-commit" 2>/dev/null)" == "$_installed_commit" ]] ||
          die "Recovery backup is incomplete or for another source commit; existing checkout left unchanged."
        ui_ok "Private recovery backup: $_recovery_backup"
      fi
      git -C "$INSTALL_DIR" merge --ff-only FETCH_HEAD </dev/null ||
        die "Could not fast-forward; recovery backup: ${_recovery_backup:-not needed}."
    elif git -C "$INSTALL_DIR" merge-base --is-ancestor "$_fetched_commit" "$_installed_commit"; then
      ui_info "This checkout is already ahead of $BOSS_REPO_REF; source left unchanged."
    elif [[ "$REF_EXPLICIT" -eq 1 ]]; then
      die "The requested ref cannot fast-forward this install; existing checkout left unchanged."
    else
      # Default main is not a reason to refuse the tree the operator already has.
      # An explicit --ref still refuses a diverged history so an upgrade cannot
      # silently run the wrong source.
      ui_warn "The requested ref $BOSS_REPO_REF has diverged from this checkout; source left unchanged."
    fi
  else
    _local_ref=""
    if git -C "$INSTALL_DIR" rev-parse --verify --quiet "${BOSS_REPO_REF}^{commit}" >/dev/null; then
      _local_ref="$(git -C "$INSTALL_DIR" rev-parse "${BOSS_REPO_REF}^{commit}")"
    fi
    if [[ -n "$_local_ref" ]] && {
         [[ "$_installed_commit" == "$_local_ref" ]] ||
         git -C "$INSTALL_DIR" merge-base --is-ancestor "$_local_ref" "$_installed_commit";
       }; then
      ui_warn "Could not fetch $BOSS_REPO_REF; using the local checkout at $_installed_commit."
    else
      die "Could not fetch $BOSS_REPO_REF; existing checkout left unchanged."
    fi
  fi
elif [[ -f "$INSTALL_DIR/.boss-shipped-commit" &&
        -f "$INSTALL_DIR/scripts/install/community-compose.yml" ]]; then
  # A tree remote.sh shipped. It copies the commit here, stamps
  # .boss-shipped-commit, and runs this script from inside that directory.
  # The shipped tree is the source. There is nothing to fetch.
  ui_ok "Using the source remote.sh shipped ($(head -c 12 "$INSTALL_DIR/.boss-shipped-commit"))"
elif [[ -d "$INSTALL_DIR" ]]; then
  [[ -z "$(ls -A "$INSTALL_DIR")" ]] ||
    die "$INSTALL_DIR is not empty and is not a Git checkout. Choose --dir; nothing was removed."
  place_fresh_source "$INSTALL_DIR" 1
else
  place_fresh_source "$INSTALL_DIR" 0
fi

if [[ "$BOSS_REPO_REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
  _expected_commit="$(printf '%s' "$BOSS_REPO_REF" | tr '[:upper:]' '[:lower:]')"
  [[ "$(git -C "$INSTALL_DIR" rev-parse HEAD)" == "$_expected_commit" ]] ||
    die 'Installed checkout does not match the requested release commit.'
fi

cd "$INSTALL_DIR"
[[ -f scripts/install/community-compose.yml ]] || die "community-compose.yml missing — is this a BOSS checkout?"
ui_ok "Source ready: $INSTALL_DIR"

# Record the ref this install follows, so a rerun and `boss upgrade` stay on it. Not secret.
# upgrade.sh pins the installer to a commit and names the ref it came from in
# BOSS_RELEASE_REF_NAME, so the record says v1.2, not a bare SHA.
INSTALLED_REF=""
INSTALLED_CHANNEL=""
write_release_state() {
  local ref="$BOSS_REPO_REF" head tmp
  head="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null)" || return 0
  if [[ -n "${BOSS_RELEASE_REF_NAME:-}" && "$REF_SOURCE" == explicit &&
        "$ref" =~ ^[0-9a-fA-F]{40}$ ]] && ref_is_valid "$BOSS_RELEASE_REF_NAME"; then
    ref="$BOSS_RELEASE_REF_NAME"
  elif [[ "$REF_SOURCE" == local ]]; then
    ref="$(git -C "$INSTALL_DIR" tag --points-at HEAD 2>/dev/null | pick_release 1 || true)"
    [[ -n "$ref" ]] || ref="$(git -C "$_src_tree" symbolic-ref --short -q HEAD 2>/dev/null || true)"
    ref_is_valid "$ref" || ref="$head"
  fi
  if [[ "$ref" =~ ^v[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.]+)?$ ]]; then
    INSTALLED_CHANNEL="release"
  elif [[ "$ref" =~ ^[0-9a-fA-F]{40}$ ]]; then
    INSTALLED_CHANNEL="pinned"
  else
    INSTALLED_CHANNEL="branch"
  fi
  INSTALLED_REF="$ref"
  tmp="$INSTALL_DIR/$RELEASE_STATE_NAME.writing.$$"
  # The local BOSS self-fix stack (scripts/install/self_fix_installer.py) is kept: boss upgrade
  # and boss update re-apply it after this run (restack), so it is never dropped silently.
  local stack_ref stack_head
  stack_ref="$(state_value BOSS_STACK_REF)"
  stack_head="$(state_value BOSS_STACK_HEAD)"
  printf '%s\n' \
    "# BOSS install record, written by install.sh. Not secret. Reruns and boss upgrade read it." \
    "BOSS_RELEASE_REF=$ref" \
    "BOSS_RELEASE_CHANNEL=$INSTALLED_CHANNEL" \
    "BOSS_INSTALLED_COMMIT=$head" \
    "BOSS_INSTALLED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    ${stack_ref:+"BOSS_STACK_REF=$stack_ref"} ${stack_head:+"BOSS_STACK_HEAD=$stack_head"} > "$tmp"
  mv -f "$tmp" "$INSTALL_DIR/$RELEASE_STATE_NAME"
  # Keep the record out of `git status` in this checkout. The exported repository has no
  # .gitignore entry for it, so use the checkout's own exclude file; nothing tracked changes.
  # The dirty-checkout checks already ignore untracked files.
  local exclude
  exclude="$(git -C "$INSTALL_DIR" rev-parse --git-path info/exclude 2>/dev/null)" || return 0
  [[ "$exclude" == /* ]] || exclude="$INSTALL_DIR/$exclude"
  if ! grep -qsxF "/$RELEASE_STATE_NAME" "$exclude"; then
    mkdir -p "$(dirname "$exclude")" && printf '/%s\n' "$RELEASE_STATE_NAME" >> "$exclude" || true
  fi
}
write_release_state
[[ -z "$INSTALLED_REF" ]] || ui_ok "Recorded release $INSTALLED_REF ($INSTALLED_CHANNEL) in $RELEASE_STATE_NAME"

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
BOSS_API_BIND=${API_BIND}
BOSS_WEB_PORT=${WEB_PORT}
BOSS_POSTGRES_PORT=${PG_PORT}
BOSS_COMMUNITY_WEB_ENABLED=${WITH_WEB}
# The bundled Prometheus and Grafana (with Mission Control); empty when it is skipped.
BOSS_GRAFANA_PORT=${GRAFANA_PORT}
BOSS_MONITORING_PROMETHEUS_URL=${MON_PROMETHEUS_URL}
BOSS_MONITORING_GRAFANA_URL=${MON_GRAFANA_URL}
BOSS_MONITORING_GRAFANA_PUBLIC_URL=${MON_GRAFANA_PUBLIC_URL}
# The local test inbox (Mailpit): empty unless the operator opted in with --with-mailpit.
BOSS_MAILPIT_PORT=${MAILPIT_PORT}
BOSS_EMAIL_DEV_INBOX_OPTED_IN=${WITH_MAILPIT}
BOSS_EMAIL_DEV_INBOX=${MAIL_DEV_INBOX}
BOSS_EMAIL_DEV_INBOX_URL=${MAIL_DEV_INBOX_URL}
BOSS_COMMUNITY_SECRET_STORE=${SECRET_STORE}
BOSS_BIND_ADDRESS=${BIND_ADDRESS}
BOSS_CONSOLE_PUBLIC_ORIGIN=${CONSOLE_ORIGIN}
NEXT_PUBLIC_BOSS_API_URL=${PUBLIC_API_URL}
NEXT_PUBLIC_BOSS_PROJECT_ID=boss-dogfood

# BYOK — your keys (Community never requires BOSS Cloud)
OPENAI_API_KEY=${OPENAI_KEY}
OPENAI_BASE_URL=
OPENAI_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
GOOGLE_API_KEY=${GOOGLE_KEY}
XAI_API_KEY=${XAI_KEY}
OPENROUTER_API_KEY=${OPENROUTER_KEY}
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
      BOSS_CONSOLE_ORIGIN_EXPLICIT="$CONSOLE_ORIGIN_EXPLICIT" \
      BOSS_API_BIND_EXPLICIT="$API_BIND_EXPLICIT" \
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
API_BIND="$(env_value BOSS_API_BIND)"
CONSOLE_ORIGIN="$(env_value BOSS_CONSOLE_PUBLIC_ORIGIN)"
API_URL="$(local_api_url "$API_BIND" "$API_PORT")"

# Local helper for humans. BOSS_API_URL is the address that answers on this machine.
cat > "$INSTALL_DIR/boss-community.env.sh" <<EOF
# Source me:  source $INSTALL_DIR/boss-community.env.sh
export BOSS_API_URL="${API_URL}"
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
# The image tag follows this compose project. A second install on the same
# machine must not replace the images this one is running.
export BOSS_COMMUNITY_IMAGE_TAG="${BOSS_COMMUNITY_IMAGE_TAG:-$COMPOSE_PROJECT_NAME}"
# ---- build metadata -------------------------------------------------------
# Describe this installed source, not a caller's stale build environment. This
# identifies a clean Git checkout; it is not a content attestation for an image.
set_build_metadata() {
  local source_root="" source_head="" source_status="" built_at=""
  # The revision is the TRACKED source: an install-local untracked file (a local CA root.crt, a
  # note) never makes it unknown — that silently disabled self-fix ("not built from a git checkout").
  BOSS_SOURCE_SHA=unknown
  BOSS_BUILT_AT=unknown
  if [[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" ]] &&
     source_root="$(git -C "$INSTALL_DIR" rev-parse --show-toplevel 2>/dev/null)" &&
     [[ "$source_root" == "$(cd "$INSTALL_DIR" && pwd -P)" ]] &&
     source_head="$(git -C "$INSTALL_DIR" rev-parse --verify HEAD 2>/dev/null)" &&
     [[ "$source_head" =~ ^[0-9a-f]{40}$ ]] &&
     source_status="$(GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor=false -C "$INSTALL_DIR" status --porcelain --untracked-files=no 2>/dev/null)" &&
     [[ -z "$source_status" ]]; then
    BOSS_SOURCE_SHA="$source_head"
  fi
  # One UTC build-invocation timestamp for all roles, not a completion timestamp.
  if built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" &&
     [[ "$built_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
    BOSS_BUILT_AT="$built_at"
  fi
  export BOSS_SOURCE_SHA BOSS_BUILT_AT
  if [[ "$BOSS_SOURCE_SHA" == unknown ]]; then
    ui_warn "Build source revision is unknown: a clean Git checkout is required."
  fi
  if [[ "$BOSS_BUILT_AT" == unknown ]]; then
    ui_warn "Build timestamp is unknown: the local UTC clock could not be read."
  fi
}
set_build_metadata
# ---- end build metadata ---------------------------------------------------
# One blocking build per service also serializes image export/load. Compose's
# parallel limit alone does not prevent a single multi-service build from
# exporting several large images at once. Keep normal cache/platform behavior.
# Parse config in memory only: it contains resolved credentials, never log it.
# ---- serial image builds --------------------------------------------------
# shellcheck disable=SC2086
if ! BUILD_SERVICES="$("${COMPOSE[@]}" \
  -f scripts/install/community-compose.yml \
  --env-file "$ENV_FILE" \
  $COMPOSE_PROFILES \
  config --format json </dev/null 2>/dev/null | python3 -c '
import argparse, json, re, sys
parser = argparse.ArgumentParser()
parser.add_argument("--profile", action="append", default=[])
active = set(parser.parse_args().profile)
services = json.load(sys.stdin)["services"]
for name, service in services.items():
    profiles = set(service.get("profiles", []))
    if "build" not in service or (profiles and not active.intersection(profiles) and "*" not in active):
        continue
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
        raise SystemExit("Invalid build service name in Compose configuration")
    print(name)
' $COMPOSE_PROFILES 2>/dev/null)"; then
  die "Could not resolve the selected Compose build services; stopping before image builds. Check the Compose configuration and .env."
fi
while IFS= read -r _build_service; do
  [[ -n "$_build_service" ]] || continue
  ui_info "Building $_build_service (one image at a time)"
  # Pass observed metadata as build arguments: sudo may reset exported variables.
  # shellcheck disable=SC2086
  "${COMPOSE[@]}" -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
    $COMPOSE_PROFILES build \
    --build-arg "BOSS_SOURCE_SHA=$BOSS_SOURCE_SHA" \
    --build-arg "BOSS_BUILT_AT=$BOSS_BUILT_AT" "$_build_service" </dev/null
done <<< "$BUILD_SERVICES"
# ---- end serial image builds ----------------------------------------------

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

# This installer runs under umask 077, so a checkout it cloned is readable only by its owner.
# Postgres reads initdb/ as its own user inside the container, and an unreadable directory
# stops the database on first start. The SQL holds no secrets; .env stays mode 600.
if [[ -d scripts/install/initdb ]]; then
  chmod 755 scripts/install/initdb
  find scripts/install/initdb -type f -name '*.sql' -exec chmod 644 {} +
fi

# BOSS's own source mirror for self-fixes (docs/product/BOSS_FIXES_ITSELF.md §6.1): the
# directory exists before Compose mounts it, so Docker never creates it as root. Empty until
# `boss self-fix seed`; nothing in it is secret.
mkdir -p .boss/selfsrc && chmod 755 .boss .boss/selfsrc

# Start only the database and API for bootstrap. A fresh worker cannot poll
# before the default project and its actor memberships exist.
# shellcheck disable=SC2086
if ! ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
  $COMPOSE_PROFILES up -d --no-build db api </dev/null; then
  ui_err "Database or API did not start."
  ui_info "Recent database and API logs:"
  # shellcheck disable=SC2086
  ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
    $COMPOSE_PROFILES logs --tail 40 db api </dev/null || true
  die "The stack did not start. On Docker Desktop the virtual disk can be full while the host still has free space; the log above names the cause."
fi
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

# Keep the existing 60-attempt / three-minute wait, including time spent in curl.
# A listener that accepts TCP but never answers must not hold the installer forever.
wait_for_api_health() {
  local budget="${1:-180}" started=$SECONDS attempts=0
  local deadline=$((started + budget)) remaining request_seconds sleep_seconds i
  for i in $(seq 1 60); do
    remaining=$((deadline - SECONDS))
    [[ "$remaining" -gt 0 ]] || break
    attempts=$i
    request_seconds=5
    [[ "$remaining" -ge "$request_seconds" ]] || request_seconds=$remaining
    if curl -fsS --connect-timeout 2 --max-time "$request_seconds" \
      "${API_URL}/health" >/dev/null 2>&1; then
      return 0
    fi
    remaining=$((deadline - SECONDS))
    [[ "$remaining" -gt 0 ]] || break
    sleep_seconds=3
    [[ "$remaining" -ge "$sleep_seconds" ]] || sleep_seconds=$remaining
    sleep "$sleep_seconds"
    if [[ $((i % 5)) -eq 0 ]]; then
      ui_info "still waiting… (${i}/60; $((SECONDS - started))s/${budget}s)"
    fi
  done
  ui_err "API did not become healthy after ${attempts} attempts ($((SECONDS - started))s; limit ${budget}s). Health requests time out after at most 5s. Check ${API_URL}/health and whether another service is using its port."
  return 1
}

if ! wait_for_api_health; then
  ui_info "Recent API and database logs:"
  # shellcheck disable=SC2086
  ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
    $COMPOSE_PROFILES logs --tail 40 api db </dev/null || true
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

# Install the one hardware agent on this computer, then record that machine as
# the default local place. The agent program is scripts/install/node_agent.sh.
# A missing script is not installed, and the local place is not marked ready.
# A present script must exit 0 before that row is written. No socket, SSH, or
# provider call is made by the place record itself.
_place_project="$(env_value BOSS_DEFAULT_PROJECT_ID)"
_place_agent_state="absent"
if [[ -z "${_place_project}" ]]; then
  ui_info "No project id yet; the local place will be recorded on first project use."
elif [[ ! -f "$INSTALL_DIR/scripts/install/node_agent.sh" ]]; then
  ui_warn "scripts/install/node_agent.sh is missing. The local hardware agent is absent, so the local place was not marked ready."
else
  # The agent refuses to install with an empty project id. The id is the
  # project this installer just seeded, not a value from the shell environment.
  if BOSS_PROJECT_ID="${_place_project}" \
      bash "$INSTALL_DIR/scripts/install/node_agent.sh" --local </dev/null; then
    _place_agent_state="installed"
  else
    ui_warn "Hardware agent did not install. The local machine was not recorded."
  fi
fi
if [[ -n "${_place_project}" && "${_place_agent_state}" == "installed" ]]; then
  _place_host="$(hostname 2>/dev/null || printf '%s' localhost)"
  _place_arch="$(uname -m 2>/dev/null || printf '%s' unknown)"
  _place_os="$(uname -s 2>/dev/null || printf '%s' unknown)"
  ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" exec -T \
    -e "BOSS_PLACE_PROJECT_ID=${_place_project}" \
    -e "BOSS_PLACE_HOSTNAME=${_place_host}" \
    -e "BOSS_PLACE_ARCH=${_place_arch}" \
    -e "BOSS_PLACE_OS=${_place_os}" \
    -e "BOSS_PLACE_ACTOR=community-installer" \
    -e "BOSS_PLACE_AGENT_STATE=installed" \
    api python scripts/install/ensure_local_place.py </dev/null \
    || ui_warn "Local place was not recorded. It will be recorded on first project use."
fi

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

# ---- models and subscriptions already on this machine ----------------------
# Finding is read only and prints found / not found, never a value. Exported API keys were
# already saved above. A Codex login file is copied only after a yes at the prompt, or with
# --connect-found. Everything else (Claude Code and Grok Build sign-in inside the BOSS
# container, Ollama, and the offers when no model is usable) is the Models step below, which
# is this install's own `boss connect`.
MODEL_NOTES=()
CODEX_FROM_DISCOVERY=0
_found=""
if [[ "$AUTO_DISCOVER" -eq 1 && -f scripts/install/discover_models.py ]]; then
  ui_step "Looking for models and subscriptions on this machine (read only)"
  _found="$(mktemp)"
  # A rerun after the installer moved Ollama to the Docker bridge looks where .env says it is.
  _ollama_url="$(grep -m 1 '^BOSS_OLLAMA_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
  # A reinstall after a purge: an earlier install bound Ollama with BOSS's own systemd drop-in.
  if [[ -z "$_ollama_url" ]]; then
    _ollama_hostport="$(sed -n 's/^Environment="OLLAMA_HOST=\([0-9.]*:[0-9]*\)"$/\1/p' \
      "${BOSS_OLLAMA_DROPIN_DIR:-/etc/systemd/system/ollama.service.d}/boss-docker-bridge.conf" 2>/dev/null | head -n 1 || true)"
    [[ -z "$_ollama_hostport" ]] || _ollama_url="http://$_ollama_hostport"
  fi
  if BOSS_OLLAMA_URL="${_ollama_url:-${BOSS_OLLAMA_URL:-}}" python3 scripts/install/discover_models.py --summary "$_found" </dev/null; then
    found() { [[ "$(grep -m 1 "^$1=" "$_found" | cut -d= -f2-)" == found ]]; }
    if found codex_login && [[ "$CONNECT_CODEX" -eq 0 ]]; then
      _codex_home="BOSS's own database on this machine"
      [[ "$SECRET_STORE" != infisical ]] || _codex_home="this install's secret store"
      if [[ "$CONNECT_FOUND" -eq 1 ]]; then
        if [[ "$SECRET_STORE" == infisical || "$ACCEPT_DATABASE_FALLBACK" -eq 1 ]]; then
          CODEX_FROM_DISCOVERY=1
        else
          ui_warn "Codex login found and not copied: the database store also needs --accept-database-fallback."
        fi
      elif ask_yes "Connect your Codex ChatGPT login? This copies ~/.codex/auth.json into ${_codex_home}."; then
        CODEX_FROM_DISCOVERY=1
      else
        if [[ "$NONINTERACTIVE" -eq 1 ]]; then
          ui_info "Codex login left alone (--yes copies nothing without --connect-found). Connect it later: boss connect codex"
        else
          ui_info "Codex login left alone. Connect it later: boss connect codex"
        fi
      fi
    fi
  else
    ui_warn "Model discovery did not finish; nothing was connected. Retry later: boss connect"
    rm -f "$_found"
    _found=""
  fi
fi

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
  [[ "$CONNECT_GROK_LOCAL" -eq 1 ]] && _subscription_args+=(--connect-grok-local)
  [[ "$ACCEPT_DATABASE_FALLBACK" -eq 1 ]] && _subscription_args+=(--accept-database-fallback)
  BOSS_API_KEY="$BOSS_API_KEY" python3 scripts/install/coding_subscriptions.py "${_subscription_args[@]}" </dev/null ||
    die "Coding subscription discovery/connection failed; BOSS core remains available."
  [[ "$CONNECT_CODEX" -eq 0 ]] || MODEL_NOTES+=("✓ Codex ChatGPT login connected")
  [[ "$CONNECT_CLAUDE" -eq 0 && "$CONNECT_CLAUDE_LOCAL" -eq 0 ]] || MODEL_NOTES+=("✓ Claude Code connected")
  [[ "$CONNECT_GROK_LOCAL" -eq 0 ]] || MODEL_NOTES+=("✓ Grok Build connected")
fi

# A login found and consented to above goes through the same reviewed import path as
# --connect-codex. Its failure is reported, not fatal: BOSS itself is already running.
if [[ "$CODEX_FROM_DISCOVERY" -eq 1 ]]; then
  ui_step "Connecting the Codex ChatGPT login"
  _codex_args=(--api-url "$API_URL" --connect-codex)
  [[ "$SECRET_STORE" == infisical ]] || _codex_args+=(--accept-database-fallback)
  if BOSS_API_KEY="$BOSS_API_KEY" python3 scripts/install/coding_subscriptions.py "${_codex_args[@]}" </dev/null; then
    ui_ok "Codex ChatGPT login connected (value never displayed)"
    MODEL_NOTES+=("✓ Codex ChatGPT login connected")
  else
    ui_err "Codex login was not connected; the reason is above. BOSS is running. Retry: boss connect codex"
    MODEL_NOTES+=("✗ Codex login not connected: boss connect codex")
  fi
fi

# ---- the Models step -------------------------------------------------------
# Detect, connect what was found (with the consent rules above), and when no model is usable
# offer to sign in to a subscription or set up a free local model. This is `boss connect`, so
# a later `boss connect` repeats exactly this step. --yes asks nothing; it installs Ollama
# only with --install-ollama.
model_usable() {
  local check rc
  for check in --hosted --local; do
    rc=0
    BOSS_API_KEY="$BOSS_API_KEY" python3 scripts/install/model_status.py --api-url "$API_URL" \
      --env-file "$ENV_FILE" "$check" </dev/null >/dev/null 2>&1 || rc=$?
    [[ "$rc" -ne 0 && "$rc" -ne 4 ]] || return 0   # usable, or BOSS cannot say
  done
  return 1
}
if [[ -f "$INSTALL_DIR/scripts/install/boss.sh" ]] &&
   [[ ( "$AUTO_DISCOVER" -eq 1 && -n "$_found" ) || "$INSTALL_OLLAMA" -eq 1 ]]; then
  ui_step "Models"
  _models_args=(--dir "$INSTALL_DIR" connect --from-installer)
  [[ -z "$_found" ]] || _models_args+=(--found "$_found")
  [[ "$CONNECT_FOUND" -eq 0 ]] || _models_args+=(--connect-found)
  [[ "$NONINTERACTIVE" -eq 0 ]] || _models_args+=(--no-questions)
  [[ "$INSTALL_OLLAMA" -eq 0 ]] || _models_args+=(--install-ollama)
  [[ -z "$LOCAL_MODEL" ]] || _models_args+=(--local-model "$LOCAL_MODEL")
  [[ "$NO_LOCAL_MODEL" -eq 0 ]] || _models_args+=(--no-local-model)
  [[ "$REBIND_OLLAMA" -eq 0 ]] || _models_args+=(--rebind-ollama)
  bash "$INSTALL_DIR/scripts/install/boss.sh" "${_models_args[@]}" ||
    ui_warn "The Models step did not finish; BOSS is running. Retry: boss connect"
  # Codex signs in on this machine, so it needs the CLI here. Offered only at a terminal and
  # only while no model is usable; --install-coding-clis does it unattended.
  if [[ "$INSTALL_CODING_CLIS" -eq 0 && "$NONINTERACTIVE" -eq 0 ]] && ! model_usable &&
     ask_yes "Install the official coding CLIs on this machine (Claude Code, Codex CLI, Grok Build)? Codex needs its CLI here to sign in; Claude Code and Grok Build already run inside BOSS."; then
    if ( INSTALL_CODING_CLIS=1; install_coding_clis ); then
      ui_info "Sign in to Codex with: codex login --device-auth   then: boss connect codex"
    else
      ui_warn "The coding CLIs were not all installed; the reason is above. BOSS is running."
    fi
  fi
fi
[[ -z "$_found" ]] || rm -f "$_found"

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

# ---- the boss command ------------------------------------------------------
# A small launcher on PATH that remembers this install directory and runs
# scripts/install/boss.sh from it, so `boss upgrade` always uses the installed release's
# helper. ~/.local/bin needs no sudo. /usr/local/bin is used only when it is already
# writable and ~/.local/bin is not on PATH. Another program's `boss` is never replaced.
BOSS_COMMAND=""
BOSS_COMMAND_MARKER="# BOSS Community command (generated by install.sh)"
install_boss_command() {
  local bin_dir="${BOSS_BIN_DIR:-}" target tmp previous resolved
  if [[ ! -f "$INSTALL_DIR/scripts/install/boss.sh" ]]; then
    ui_info "This release has no boss command; use the scripts in $INSTALL_DIR/scripts/install."
    return 0
  fi
  if [[ -z "$bin_dir" ]]; then
    bin_dir="$HOME/.local/bin"
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* && -d /usr/local/bin && -w /usr/local/bin &&
          ":$PATH:" == *":/usr/local/bin:"* ]]; then
      bin_dir="/usr/local/bin"
    fi
  fi
  target="$bin_dir/boss"
  if [[ -e "$target" || -L "$target" ]] && ! grep -qsF "$BOSS_COMMAND_MARKER" "$target"; then
    ui_warn "$target belongs to another program; left it alone. Use: bash $INSTALL_DIR/scripts/install/boss.sh"
    BOSS_COMMAND="bash $INSTALL_DIR/scripts/install/boss.sh"
    return 0
  fi
  previous="$(grep -m 1 '^BOSS_INSTALL_DIR_DEFAULT=' "$target" 2>/dev/null | cut -d= -f2- || true)"
  # The resolved path, so `boss uninstall --purge` can tell this launcher belongs to its install.
  resolved="$(cd "$INSTALL_DIR" && pwd -P)"
  mkdir -p "$bin_dir" || { ui_warn "Could not create $bin_dir; the boss command was not installed."; return 0; }
  tmp="$bin_dir/.boss.writing.$$"
  {
    printf '%s\n' '#!/usr/bin/env bash' "$BOSS_COMMAND_MARKER"
    printf '%s\n' '# Runs the lifecycle helper of one install. Rerunning the installer rewrites this file.'
    printf 'BOSS_INSTALL_DIR_DEFAULT=%q\n' "$resolved"
    printf '%s\n' 'export BOSS_INSTALL_DIR="${BOSS_INSTALL_DIR:-$BOSS_INSTALL_DIR_DEFAULT}"' \
      'export BOSS_LAUNCHER="${BASH_SOURCE[0]}"' \
      'helper="$BOSS_INSTALL_DIR/scripts/install/boss.sh"' \
      '[[ -f "$helper" ]] || { echo "boss: no BOSS install at $BOSS_INSTALL_DIR" >&2; exit 1; }' \
      'exec bash "$helper" "$@"'
  } > "$tmp" || { rm -f "$tmp"; ui_warn "Could not write $target; the boss command was not installed."; return 0; }
  chmod 755 "$tmp"
  mv -f "$tmp" "$target"
  if [[ -n "$previous" && "$previous" != "$(printf '%q' "$resolved")" &&
        "$previous" != "$(printf '%q' "$INSTALL_DIR")" ]]; then
    ui_info "boss now manages $INSTALL_DIR (it pointed at another install before)."
  fi
  if [[ ":$PATH:" == *":$bin_dir:"* ]]; then
    BOSS_COMMAND="boss"
    ui_ok "Installed the boss command: $target"
  else
    BOSS_COMMAND="$target"
    ui_ok "Installed the boss command: $target"
    ui_info "Add it to PATH for new shells: echo 'export PATH=\"$bin_dir:\$PATH\"' >> ~/.profile"
  fi
}
if [[ "$INSTALL_BOSS_COMMAND" -eq 1 ]]; then
  ui_step "Installing the boss command"
  install_boss_command
fi
B="${BOSS_COMMAND:-bash $INSTALL_DIR/scripts/install/boss.sh}"
[[ -f "$INSTALL_DIR/scripts/install/boss.sh" || -n "$BOSS_COMMAND" ]] || B=""
CURRENT_STEP=""

# ---- success card ----------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}────────────────────────────────────────────────────────${NC}"
echo -e "${GREEN}${BOLD}  ✓ BOSS Community is running.${NC}"
echo -e "${GREEN}${BOLD}────────────────────────────────────────────────────────${NC}"
echo ""
if [[ "$WITH_WEB" -eq 1 ]]; then
  # THE ADDRESS IS WHERE SETUP FINISHES: Repo and models — paste a GitHub token, connect a
  # model, open a first pull request. The public origin when there is one.
  if [[ -n "$CONSOLE_ORIGIN" ]]; then
    echo -e "  ${BOLD}Finish setup${NC}     ${CONSOLE_ORIGIN%/}/?area=setup"
  else
    echo -e "  ${BOLD}Finish setup${NC}     http://127.0.0.1:${WEB_PORT}/?area=setup"
  fi
  if [[ -n "$B" ]]; then
    echo -e "  ${BOLD}Password${NC}         $B password   (shown in a terminal only)"
  else
    echo -e "  ${BOLD}Password${NC}         python3 \"$INSTALL_DIR/scripts/install/console_password.py\" show --env-file \"$ENV_FILE\""
  fi
fi
[[ -z "$INSTALLED_REF" ]] || echo -e "  ${BOLD}Release${NC}          ${INSTALLED_REF} (${INSTALLED_CHANNEL}; reruns and upgrades stay on it)"
# One row per model source: connected, found but not connected, or not found, with the command
# that fixes it. Read from BOSS and this machine now, not from what this run happened to do.
_model_rows=""
if [[ -f "$INSTALL_DIR/scripts/install/boss.sh" ]]; then
  _model_rows="$(bash "$INSTALL_DIR/scripts/install/boss.sh" --dir "$INSTALL_DIR" connect --summary 2>/dev/null </dev/null || true)"
fi
if [[ -n "$_model_rows" || "${#MODEL_NOTES[@]}" -gt 0 ]]; then
  echo -e "  ${BOLD}Models${NC}"
  [[ -z "$_model_rows" ]] || printf '%s\n' "$_model_rows"
  for _note in ${MODEL_NOTES[@]+"${MODEL_NOTES[@]}"}; do
    echo "    $_note"
  done
else
  echo -e "  ${BOLD}Models${NC}           none yet. Model calls fail closed until one is connected${B:+: $B connect}"
fi
# The repository is connected in the console, not here; say which, or that it is still to do.
_repo_row=""
if [[ -f "$INSTALL_DIR/scripts/install/setup_state.py" ]]; then
  _repo_row="$(BOSS_API_URL="$API_URL" BOSS_API_KEY="$(grep -m 1 '^BOSS_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)" \
    python3 "$INSTALL_DIR/scripts/install/setup_state.py" repository 2>/dev/null </dev/null || true)"
fi
if [[ -z "$_repo_row" || "$_repo_row" == "not connected" ]]; then
  echo -e "  ${BOLD}Repository${NC}       not connected — connect it in the console"
else
  echo -e "  ${BOLD}Repository${NC}       ${_repo_row}"
fi
echo ""
echo -e "  ${BOLD}API${NC}              ${API_URL}"
if [[ "$API_BIND" != "127.0.0.1" ]]; then
  echo -e "  ${BOLD}API bind${NC}         ${API_BIND}"
fi
echo -e "  ${BOLD}Health${NC}           ${API_URL}/health"
if [[ "$WITH_WEB" -eq 1 && -n "$CONSOLE_ORIGIN" ]]; then
  echo -e "  ${BOLD}Console origin${NC}   ${CONSOLE_ORIGIN}"
  echo -e "  ${MUTED}It answers only after a TLS proxy on this machine forwards to 127.0.0.1:${WEB_PORT} and overwrites Host, X-Forwarded-Host, and X-Forwarded-Proto.${NC}"
fi
echo -e "  ${BOLD}Project${NC}          boss-dogfood"
echo -e "  ${BOLD}API key${NC}          (in $ENV_FILE — not printed)"
echo -e "  ${BOLD}Install dir${NC}      $INSTALL_DIR"
echo ""
if [[ -n "$B" ]]; then
  echo -e "${BOLD}Next${NC}"
  echo ""
  echo "  $B open        # finish setup in the console"
  echo "  $B doctor      # check the install can do its job"
  echo "  $B status      # release, services, health"
  echo "  $B connect     # connect models and subscriptions found later"
  echo "  $B upgrade     # back up, then move to the newest release"
  echo "  $B uninstall   # stop BOSS and keep data"
  echo ""
fi
echo -e "${BOLD}On this machine${NC}"
echo ""
echo "  # Terminal on this machine"
if [[ -n "$B" ]]; then
  echo "  $B metatron"
else
  echo "  cd $INSTALL_DIR && make metatron"
fi
echo ""
echo "  # CLI on this machine"
echo "  source $INSTALL_DIR/boss-community.env.sh"
echo ""
echo -e "${MUTED}Docs: https://github.com/seanventures/boss#readme${NC}"
echo -e "${MUTED}Features: docs/community/FEATURES.md${NC}"
echo -e "${MUTED}Model paths: docs/community/MODEL_CONNECTIONS.md${NC}"
echo -e "${MUTED}Cloud is optional — Easy Connect later via Mission Control Editions.${NC}"
echo ""
