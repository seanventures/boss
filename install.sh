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
# WHERE THIS INSTALL KEEPS ITS SECRETS. Both options stay inside your own infrastructure; the
# difference is two containers and about 400MB, not who holds the data. See --help.
SECRET_STORE="${BOSS_SECRET_STORE:-database}"

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
  --secret-store WHICH    database (default) or infisical. Both stay on your own machine:
                            database   4 containers. Credentials are held by BOSS's own
                                       Postgres. Lean; storing a SUBSCRIPTION token asks you
                                       to confirm the database is an acceptable home for it.
                            infisical  6 containers, ~400MB more. Bundled Infisical secret
                                       store, bootstrapped and wired up for you. Credentials
                                       are referenced rather than held, and rotate cleanly.
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
    --secret-store)
      SECRET_STORE="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"; shift 2
      case "$SECRET_STORE" in database|infisical) ;; *) die "--secret-store must be database or infisical" ;; esac ;;
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
# THE CHOICE IS THE OPERATOR'S, because the right answer depends on their machine.
#
# install.sh used to generate INFISICAL_ENCRYPTION_KEY and then never start Infisical, so every
# install had those keys written and unused and no secret store at all — which only surfaced
# later, when storing a subscription token returned "no secret store". Now it is asked and
# answered, and whichever way it goes the install matches what was chosen.
if [[ "$ASSUME_YES" -eq 0 && "$NONINTERACTIVE" -eq 0 ]]; then
  echo ""
  ui_info "Where should this install keep credentials? Both stay on this machine."
  ui_info "  1) database   — 4 containers, leaner. BOSS's own Postgres holds them."
  ui_info "  2) infisical  — 6 containers (~400MB more). A real secret store, bootstrapped."
  prompt "Secret store: 1 (database) or 2 (infisical)?" "1"
  case "$(printf '%s' "$REPLY" | tr -d ' ')" in
    2|infisical) SECRET_STORE="infisical" ;;
    *) SECRET_STORE="database" ;;
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
# running the installer twice over the same volume on 2.29.2.202.
#
# The API key is preserved for a smaller reason: anything an operator saved, scripted or put in
# a tunnel command keeps working across a re-run instead of silently 401-ing.
if [ -f "$ENV_FILE" ]; then
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

# The store has to be PROVISIONED, not merely running. `infisical bootstrap` returns the
# machine-identity token exactly once, so bootstrap_infisical.sh writes it to .env before doing
# anything else — re-running against an already-bootstrapped instance returns nothing.
if [[ "$SECRET_STORE" == "infisical" ]]; then
  ui_step "Provisioning the secret store"
  if [[ -f "$INSTALL_DIR/scripts/install/bootstrap_infisical.sh" ]]; then
    if COMPOSE_FILE=scripts/install/community-compose.yml \
       sh "$INSTALL_DIR/scripts/install/bootstrap_infisical.sh" "$ENV_FILE" >/dev/null 2>&1; then
      # THE VALUES ARRIVE AFTER THE CONTAINERS DID.
      #
      # bootstrap writes BOSS_INFISICAL_URL / _PROJECT_ID / _TOKEN into .env, and compose reads
      # .env when it CREATES a container. api and worker were created before any of that
      # existed, so they hold the variables with empty values and BOSS reports "no secret
      # store" on an install where Infisical is running perfectly. Recreate them so they read
      # what was just written.
      # shellcheck disable=SC2086
      ${COMPOSE[@]} -f scripts/install/community-compose.yml --env-file "$ENV_FILE" \
        $COMPOSE_PROFILES up -d --no-deps --force-recreate api worker >/dev/null 2>&1 \
        || ui_warn "could not restart api/worker — they may not see the secret store yet"
      ui_ok "Secret store provisioned — BOSS will reference credentials rather than hold them"
    else
      # NOT FATAL, and not silent. BOSS falls back through its store chain to the database, so
      # the install works; the operator just has the leaner arrangement they did not pick.
      ui_warn "The secret store did not provision. BOSS will use its database instead."
      ui_warn "  Retry: sh scripts/install/bootstrap_infisical.sh $ENV_FILE"
    fi
  else
    ui_warn "bootstrap_infisical.sh is missing — falling back to the database store"
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
  -H "Authorization: Bearer ${BOSS_API_KEY}" \
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
echo -e "${MUTED}Docs: https://boss.sean.network/community/${NC}"
echo -e "${MUTED}Spec: docs/product/BOSS_COMMUNITY_EDITION_SPEC.md${NC}"
echo -e "${MUTED}Cloud is optional — Easy Connect later via Mission Control Editions.${NC}"
echo ""
