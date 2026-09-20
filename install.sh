#!/usr/bin/env bash
# =============================================================================
# BOSS Community — one-line installer (OpenClaw-style)
#
#   curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash
#
# Idiot-proof Community edition: checks the machine, installs deps, prompts for
# keys, starts Postgres + API + worker (+ optional Mission Control), seeds the
# project, and verifies health. No BOSS Cloud required.
# =============================================================================
set -euo pipefail

BOSS_INSTALL_VERSION="1.0.0"
BOSS_REPO_URL="${BOSS_REPO_URL:-https://github.com/seanheiney/boss.git}"
BOSS_REPO_REF="${BOSS_REPO_REF:-main}"
BOSS_RAW_BASE="${BOSS_RAW_BASE:-https://raw.githubusercontent.com/seanheiney/boss/${BOSS_REPO_REF}}"

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
ASSUME_YES=0
WITH_WEB=1
NONINTERACTIVE=0
SKIP_MODEL_KEY=0
OPENAI_KEY_PRESET="${OPENAI_API_KEY:-}"
ANTHROPIC_KEY_PRESET="${ANTHROPIC_API_KEY:-}"
GITHUB_TOKEN_PRESET="${BOSS_GITHUB_TOKEN:-}"
API_PORT="${BOSS_API_PORT:-8000}"
# THE ADDRESS THE BROWSER WILL USE — not the one the server calls itself.
#
# NEXT_PUBLIC_BOSS_API_URL is compiled INTO the web bundle at image build time. Defaulting it
# to 127.0.0.1 is right on a laptop and wrong on every server: the browser then tries to reach
# the API on the machine it is running on, which is the operator's, and every preview link
# points at nothing while the previews themselves are fine. Empty means "this is a laptop".
PUBLIC_URL="${BOSS_PUBLIC_URL:-}"
WEB_PORT="${BOSS_WEB_PORT:-3000}"
PG_PORT="${BOSS_POSTGRES_PORT:-5432}"
COMPOSE_PROFILES=""

usage() {
  cat <<EOF
BOSS Community installer

Usage:
  curl -fsSL ${BOSS_RAW_BASE}/install.sh | bash
  curl -fsSL ${BOSS_RAW_BASE}/install.sh | bash -s -- [options]

Options:
  --dir PATH              Install directory (default: ~/boss)
  --yes, -y               Non-interactive defaults (still needs a model key via env or --openai-key)
  --openai-key KEY        OpenAI (or compatible) API key
  --anthropic-key KEY     Anthropic API key (optional)
  --github-token TOKEN    GitHub token for Software Factory (optional)
  --no-web                Skip Mission Control web console
  --api-port N            Host port for API (default 8000)
  --web-port N            Host port for web (default 3000)
  --postgres-port N       Host port for Postgres (default 5432)
  --ref REF               Git ref to clone (default main)
  --skip-model-key        Allow empty model key (API starts; model calls fail closed)
  -h, --help              Show this help

Environment overrides:
  BOSS_INSTALL_DIR, OPENAI_API_KEY, ANTHROPIC_API_KEY, BOSS_GITHUB_TOKEN,
  BOSS_REPO_URL, BOSS_REPO_REF, BOSS_API_PORT, BOSS_WEB_PORT, BOSS_POSTGRES_PORT

Examples:
  curl -fsSL ${BOSS_RAW_BASE}/install.sh | bash
  curl -fsSL ${BOSS_RAW_BASE}/install.sh | bash -s -- --dir ~/boss --openai-key "\$OPENAI_API_KEY"
  curl -fsSL ${BOSS_RAW_BASE}/install.sh | bash -s -- -y --skip-model-key --no-web
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; NONINTERACTIVE=1; shift ;;
    --openai-key) OPENAI_KEY_PRESET="$2"; shift 2 ;;
    --anthropic-key) ANTHROPIC_KEY_PRESET="$2"; shift 2 ;;
    --github-token) GITHUB_TOKEN_PRESET="$2"; shift 2 ;;
    --no-web) WITH_WEB=0; shift ;;
    --public-url) PUBLIC_URL="$2"; shift 2 ;;
    --api-port) API_PORT="$2"; shift 2 ;;
    --web-port) WEB_PORT="$2"; shift 2 ;;
    --postgres-port) PG_PORT="$2"; shift 2 ;;
    --ref) BOSS_REPO_REF="$2"; shift 2 ;;
    --skip-model-key) SKIP_MODEL_KEY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
done

# Expand ~
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

prompt() {
  # prompt "Question" "default" -> sets REPLY
  local q="$1" def="${2:-}"
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then
    REPLY="$def"
    return
  fi
  if [[ -n "$def" ]]; then
    read -r -p "$(echo -e "${BOLD}$q${NC} ${MUTED}[$def]${NC}: ")" REPLY || true
    REPLY="${REPLY:-$def}"
  else
    read -r -p "$(echo -e "${BOLD}$q${NC}: ")" REPLY || true
  fi
}

prompt_secret() {
  local q="$1"
  if [[ "$NONINTERACTIVE" -eq 1 ]]; then
    REPLY=""
    return
  fi
  read -r -s -p "$(echo -e "${BOLD}$q${NC}: ")" REPLY || true
  echo ""
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

# ---- preflight -------------------------------------------------------------
ui_banner
ui_step "Checking your machine"

OS="$(uname -s 2>/dev/null || echo unknown)"
ARCH="$(uname -m 2>/dev/null || echo unknown)"
ui_info "OS: $OS  arch: $ARCH"

MISSING=()
need_cmd curl || need_cmd wget || MISSING+=("curl or wget")
need_cmd git || MISSING+=("git")
need_cmd docker || MISSING+=("docker")

if [[ ${#MISSING[@]} -gt 0 ]]; then
  ui_err "Missing required tools: ${MISSING[*]}"
  echo ""
  echo "Install tips:"
  case "$OS" in
    Darwin)
      echo "  brew install git curl"
      echo "  Install Docker Desktop: https://docs.docker.com/desktop/"
      ;;
    Linux)
      echo "  sudo apt-get update && sudo apt-get install -y git curl"
      echo "  Docker: https://docs.docker.com/engine/install/"
      ;;
    *)
      echo "  Install git, curl, and Docker for your OS, then re-run this installer."
      ;;
  esac
  exit 1
fi
ui_ok "git, docker, and a downloader are available"

# Docker daemon
if ! docker info >/dev/null 2>&1; then
  die "Docker is installed but not running. Start Docker Desktop (or the docker daemon) and re-run."
fi
ui_ok "Docker daemon is running"

# Compose v2
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif need_cmd docker-compose; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose is required (docker compose plugin or docker-compose)."
fi
ui_ok "Docker Compose: $(${COMPOSE[@]} version 2>/dev/null | head -1)"

# Disk space (~5GB free recommended for images)
if need_cmd df; then
  FREE_KB="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print $4}')"
  if [[ -n "${FREE_KB:-}" && "$FREE_KB" -lt 3000000 ]]; then
    ui_warn "Low disk space under \$HOME (${FREE_KB}KB free). Builds may fail."
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
  prompt "Install Mission Control web console? (Y/n)" "Y"
  case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
    n|no) WITH_WEB=0 ;;
    *) WITH_WEB=1 ;;
  esac
fi
if [[ "$WITH_WEB" -eq 1 ]]; then
  COMPOSE_PROFILES="--profile web"
  ui_ok "Mission Control: yes (port $WEB_PORT)"
else
  ui_ok "Mission Control: skipped"
fi

# Generate control-plane API key
if need_cmd openssl; then
  BOSS_API_KEY="$(openssl rand -hex 24)"
else
  BOSS_API_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
BOSS_DB_PASSWORD="boss"
if need_cmd openssl; then
  BOSS_DB_PASSWORD="$(openssl rand -hex 12)"
fi

# ---- fetch source ----------------------------------------------------------
ui_step "Fetching BOSS Community source"

mkdir -p "$(dirname "$INSTALL_DIR")"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  ui_info "Existing git repo at $INSTALL_DIR — pulling $BOSS_REPO_REF"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$BOSS_REPO_REF" 2>/dev/null || true
  git -C "$INSTALL_DIR" checkout "$BOSS_REPO_REF" 2>/dev/null || true
  git -C "$INSTALL_DIR" pull --ff-only origin "$BOSS_REPO_REF" 2>/dev/null || \
    ui_warn "Could not fast-forward; using existing tree"
elif [[ -f "$INSTALL_DIR/install.sh" && -f "$INSTALL_DIR/Dockerfile" ]]; then
  ui_ok "Using existing checkout at $INSTALL_DIR"
else
  ui_info "Cloning $BOSS_REPO_URL ($BOSS_REPO_REF) → $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  git clone --depth 1 --branch "$BOSS_REPO_REF" "$BOSS_REPO_URL" "$INSTALL_DIR" \
    || git clone --depth 1 "$BOSS_REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
[[ -f scripts/install/community-compose.yml ]] || die "community-compose.yml missing — is this a BOSS checkout?"
ui_ok "Source ready: $INSTALL_DIR"

# ---- write env -------------------------------------------------------------
ui_step "Writing Community configuration"

ENV_FILE="$INSTALL_DIR/.env"

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

cat > "$ENV_FILE" <<EOF
# BOSS Community — generated by install.sh $(date -u +%Y-%m-%dT%H:%MZ)
# Keep this file private. Do not commit.

BOSS_EDITION=community
BOSS_REQUIRE_API_AUTH=true
BOSS_API_KEY=${BOSS_API_KEY}
BOSS_API_KEYS=${BOSS_API_KEY}
BOSS_API_KEY_PRINCIPALS=${BOSS_API_KEY}:ceo
BOSS_DB_PASSWORD=${BOSS_DB_PASSWORD}
BOSS_DEFAULT_ORG_ID=org_boss
BOSS_DEFAULT_WORKSPACE_ID=ws_boss
BOSS_DEFAULT_PROJECT_ID=boss-dogfood
BOSS_API_PORT=${API_PORT}
BOSS_WEB_PORT=${WEB_PORT}
BOSS_POSTGRES_PORT=${PG_PORT}
NEXT_PUBLIC_BOSS_API_URL=${PUBLIC_API_URL}
NEXT_PUBLIC_BOSS_PROJECT_ID=boss-dogfood

# BYOK — your keys (Community never requires BOSS Cloud)
OPENAI_API_KEY=${OPENAI_KEY}
OPENAI_BASE_URL=
OPENAI_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
BOSS_GITHUB_TOKEN=${GITHUB_TOKEN}

# Leave unset for pure Community independence
BOSS_ACCOUNT_CONNECT_URL=
EOF
chmod 600 "$ENV_FILE"
ui_ok "Wrote $ENV_FILE (mode 600)"

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
    sh "$INSTALL_DIR/scripts/install/gen_infisical_keys.sh" "$ENV_FILE" >/dev/null 2>&1 \
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
  up -d --build

ui_ok "Containers started"

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
set +e
${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" exec -T api \
  python -c "from boss_engine.bootstrap import seed; seed()" 2>/tmp/boss-seed.err
SEED_RC=$?
set -e
if [[ "$SEED_RC" -ne 0 ]]; then
  ui_warn "Seed returned $SEED_RC — trying alternate entrypoint"
  ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" exec -T api \
    python -m boss_engine.bootstrap 2>>/tmp/boss-seed.err || \
    ui_warn "Seed failed; API is up — run seed manually (see docs). Log: /tmp/boss-seed.err"
else
  ui_ok "Database seeded (project boss-dogfood)"
fi

# ---- post checks -----------------------------------------------------------
ui_step "Verifying Community independence"

set +e
EDITION_CODE="$(curl -sS -o /tmp/boss-edition.json -w '%{http_code}' \
  -H "X-API-Key: ${BOSS_API_KEY}" \
  "${API_URL}/v1/edition?project_id=boss-dogfood" 2>/dev/null || echo 000)"
set -e
if [[ "$EDITION_CODE" == "200" ]]; then
  ui_ok "GET /v1/edition → 200"
  if grep -q 'community' /tmp/boss-edition.json 2>/dev/null; then
    ui_ok "Edition reports community"
  fi
else
  ui_warn "Edition probe HTTP $EDITION_CODE (auth/seed may still be settling)"
fi

set +e
PROV_CODE="$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "X-API-Key: ${BOSS_API_KEY}" \
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
echo "  curl -sS -H \"X-API-Key: \$BOSS_API_KEY\" \$BOSS_API_URL/health"
echo ""
echo "  # Logs"
echo "  cd $INSTALL_DIR && docker compose -f scripts/install/community-compose.yml --env-file .env logs -f"
echo ""
echo "  # Stop"
echo "  cd $INSTALL_DIR && docker compose -f scripts/install/community-compose.yml --env-file .env down"
echo ""
echo -e "${MUTED}Docs: https://boss.sean.network/community/${NC}"
echo -e "${MUTED}Spec: docs/product/BOSS_COMMUNITY_EDITION_SPEC.md${NC}"
echo -e "${MUTED}Cloud is optional — Easy Connect later via Mission Control Editions.${NC}"
echo ""
