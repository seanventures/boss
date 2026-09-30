#!/usr/bin/env bash
# =============================================================================
# Shared-server bare-metal PaaS installer (modular kit)
#
# Standalone (future OSS repo root):
#   curl -fsSL https://raw.githubusercontent.com/<org>/shared-server/main/install.sh | sudo bash
#
# Vendored inside BOSS monorepo today:
#   sudo packages/shared-server/install.sh --site /etc/shared-server/site.yaml
#
# Prerequisites: Ubuntu 22.04/24.04 (primary), root, Tailscale for admin plane.
# =============================================================================
set -euo pipefail

SITE_FILE=""
SKIP_TAILSCALE_CHECK=0
INSTALL_CORE_PACKAGES=0
KIT_SRC=""

usage() {
  cat <<'EOF'
Usage: install.sh [options]

  --site PATH           Site config (default: /etc/shared-server/site.yaml)
  --kit-src PATH        Path to shared-server kit tree (default: script dir)
  --with-core           After bootstrap, install default bootstrap modules
  --skip-tailscale-check
  -h, --help

Installs the modular kit (core + modules manifests + ge CLI). Use
`ge upgrade <new-tree>` later to upgrade without re-running this script.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --site) SITE_FILE="${2:-}"; shift 2 ;;
    --kit-src) KIT_SRC="${2:-}"; shift 2 ;;
    --with-core) INSTALL_CORE_PACKAGES=1; shift ;;
    --skip-tailscale-check) SKIP_TAILSCALE_CHECK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

[ "$(id -u)" = "0" ] || { echo "run as root" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_SRC="${KIT_SRC:-$SCRIPT_DIR}"

COMMON=""
if [ -f "$KIT_SRC/core/lib/common.sh" ]; then
  COMMON="$KIT_SRC/core/lib/common.sh"
  MODULE_SH="$KIT_SRC/core/lib/module.sh"
elif [ -f "$KIT_SRC/lib/common.sh" ]; then
  COMMON="$KIT_SRC/lib/common.sh"
  MODULE_SH="$KIT_SRC/lib/module.sh"
else
  echo "missing core/lib/common.sh in $KIT_SRC" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$COMMON"
# shellcheck source=/dev/null
source "$MODULE_SH"

export SHARED_SERVER_KIT_ROOT="$KIT_SRC"
if [ -n "$SITE_FILE" ]; then
  export SHARED_SERVER_SITE="$SITE_FILE"
fi

KIT_VER="$(ge_kit_version)"
ge_step "Shared-server kit installer v${KIT_VER}"
ge_info "kit source: $KIT_SRC (modular)"

if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  ge_info "os: ${PRETTY_NAME:-unknown}"
  case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:24.*|ubuntu:22.*|debian:12*|debian:13*) ;;
    *) ge_info "warning: untested OS — continuing anyway" ;;
  esac
fi

if [ "$SKIP_TAILSCALE_CHECK" != "1" ]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    ge_info "Tailscale not installed. Admin plane expects a tailnet."
    ge_info "Install: curl -fsSL https://tailscale.com/install.sh | sh && tailscale up"
  elif ! tailscale ip -4 >/dev/null 2>&1; then
    ge_info "Tailscale present but not up — run: tailscale up"
  else
    TNIP="$(tailscale ip -4 | head -1)"
    ge_info "tailnet ip: $TNIP"
  fi
fi

SITE_PATH="$(ge_site_file)"
if [ ! -f "$SITE_PATH" ]; then
  ge_step "install default site config → $SITE_PATH"
  mkdir -p "$(dirname "$SITE_PATH")"
  cp "$KIT_SRC/site.example.yaml" "$SITE_PATH"
  ge_info "edit $SITE_PATH before production use"
else
  ge_info "using site config: $SITE_PATH"
fi

ge_step "create platform directories"
ge_ensure_dirs
INFRA_ROOT="$(ge_load_site_value paths.infra_root)"
INFRA_ROOT="${INFRA_ROOT:-/opt/shared-server}"
STATE="$(ge_state_dir)"

ge_step "install kit → $INFRA_ROOT"
mkdir -p "$INFRA_ROOT"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    "$KIT_SRC/" "$INFRA_ROOT/"
else
  cp -a "$KIT_SRC/." "$INFRA_ROOT/"
fi
# ensure back-compat symlinks on target
ln -sfn core/lib "$INFRA_ROOT/lib"
ln -sfn core/bin "$INFRA_ROOT/bin"
ln -sfn modules "$INFRA_ROOT/packages"

export SHARED_SERVER_KIT_ROOT="$INFRA_ROOT"

ge_step "install ge CLI"
GE_BIN="$INFRA_ROOT/core/bin/ge"
[ -x "$GE_BIN" ] || GE_BIN="$INFRA_ROOT/bin/ge"
install -m 0755 "$GE_BIN" /usr/local/bin/ge
if [ ! -e /usr/local/bin/deploy ] || grep -q 'exec ge deploy' /usr/local/bin/deploy 2>/dev/null; then
  cat >/usr/local/bin/deploy <<'WRAP'
#!/usr/bin/env bash
exec ge deploy "$@"
WRAP
  chmod 0755 /usr/local/bin/deploy
fi

echo "$KIT_VER" >"$STATE/kit-version"
date -u +%Y-%m-%dT%H:%M:%SZ >"$STATE/installed-at"
echo "$INFRA_ROOT" >"$STATE/kit-root"

ge_step "build module catalog"
SHARED_SERVER_KIT_ROOT="$INFRA_ROOT" bash -c 'source "'"$INFRA_ROOT"'/core/lib/common.sh"; source "'"$INFRA_ROOT"'/core/lib/module.sh"; ge_catalog_build' \
  || SHARED_SERVER_KIT_ROOT="$INFRA_ROOT" /usr/local/bin/ge catalog build || true

if ! command -v docker >/dev/null 2>&1; then
  ge_step "Docker not found — installing via get.docker.com"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker || true
else
  ge_info "docker present: $(docker --version 2>/dev/null || true)"
fi

if [ "$INSTALL_CORE_PACKAGES" = "1" ]; then
  ge_step "install default bootstrap modules"
  ge module install layer0-host
  ge module install layer1-runtime
  ge module install layer2-edge
  ge module install deploy-cli
fi

ge_step "done"
cat <<EOF

Shared-server kit ${KIT_VER} installed (modular).

  site:    $SITE_PATH
  kit:     $INFRA_ROOT
  state:   $STATE
  cli:     ge help

Next:
  1. Edit $SITE_PATH
  2. ge module list
  3. ge module install layer0-host && ge module install layer1-runtime
  4. ge module install layer2-edge && ge module install deploy-cli
  5. ge node status

Upgrade later:
  ge upgrade /path/to/new-kit-tree

Optional BOSS control plane: see integrations/boss/README.md
  docs: docs/ARCHITECTURE.md · docs/MODULE_CONTRACT.md · docs/UPGRADE.md
EOF
