#!/usr/bin/env bash
# Shared helpers for Goldeneye kit installers and the ge CLI.
# shellcheck disable=SC2034
set -euo pipefail

# GE_VERSION is the CLI/tooling revision; kit tree version lives in VERSION.
GE_VERSION="0.2.0"

ge_die() { echo "ge: error: $*" >&2; exit 1; }
ge_info() { echo "ge: $*"; }
ge_step() { echo ""; echo "=== $* ==="; }

ge_require_root() {
  [ "$(id -u)" = "0" ] || ge_die "run as root"
}

ge_kit_root() {
  if [ -n "${GOLDENEYE_KIT_ROOT:-}" ]; then
    echo "$GOLDENEYE_KIT_ROOT"
    return
  fi
  # Resolve physical path so lib/ → core/lib symlink does not break root detection.
  local libdir cand
  libdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P 2>/dev/null || cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # common.sh lives in <kit>/core/lib
  for cand in \
    "$(cd "$libdir/../.." && pwd)" \
    "$(cd "$libdir/.." && pwd)" \
    "$(cd "$libdir/../../.." && pwd)" \
    ; do
    if [ -f "$cand/VERSION" ] || [ -d "$cand/modules" ] || [ -d "$cand/core/lib" ]; then
      echo "$cand"
      return
    fi
  done
  echo "$(cd "$libdir/../.." && pwd)"
}

ge_state_dir() {
  echo "${GOLDENEYE_STATE_ROOT:-/var/lib/goldeneye}"
}

ge_site_file() {
  echo "${GOLDENEYE_SITE:-/etc/goldeneye/site.yaml}"
}

ge_load_site_value() {
  local key="$1"
  local file
  file="$(ge_site_file)"
  [ -f "$file" ] || { echo ""; return 0; }
  python3 - "$file" "$key" <<'PY' 2>/dev/null || true
import sys
path = sys.argv[2].split(".")
try:
    import yaml  # type: ignore
except Exception:
    want = path[-1]
    with open(sys.argv[1], encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s.startswith("#") or ":" not in s:
                continue
            k, _, v = s.partition(":")
            if k.strip() == want:
                print(v.strip().strip('"').strip("'"))
                sys.exit(0)
    sys.exit(0)

with open(sys.argv[1], encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}
cur = data
for p in path:
    if not isinstance(cur, dict) or p not in cur:
        print("")
        sys.exit(0)
    cur = cur[p]
if cur is None:
    print("")
elif isinstance(cur, (list, dict)):
    print("")
else:
    print(cur)
PY
}

ge_ensure_dirs() {
  local data_root apps_root infra_root state
  data_root="$(ge_load_site_value paths.data_root)"
  data_root="${data_root:-/data}"
  apps_root="$(ge_load_site_value paths.apps_root)"
  apps_root="${apps_root:-/srv/apps}"
  infra_root="$(ge_load_site_value paths.infra_root)"
  infra_root="${infra_root:-/opt/goldeneye}"
  state="$(ge_state_dir)"
  mkdir -p "$state/packages" "$state/modules" "$state/apps" "$apps_root" "$infra_root" \
    /etc/goldeneye /var/log/goldeneye \
    "${GOLDENEYE_MODULES_D:-/var/lib/goldeneye/modules.d}"
  mkdir -p "$data_root" 2>/dev/null || true
  for bind in docker srv backups; do
    mkdir -p "$data_root/$bind" 2>/dev/null || true
  done
}

# Legacy wrappers — prefer ge_module_* from module.sh
ge_mark_package_installed() {
  if declare -F ge_module_mark >/dev/null 2>&1; then
    ge_module_mark "$1" "$(ge_module_field "$1" version 2>/dev/null || echo 0.0.0)" install
  else
    local state
    state="$(ge_state_dir)/packages"
    mkdir -p "$state"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$state/${1}.installed"
  fi
}

ge_package_installed() {
  if declare -F ge_module_installed >/dev/null 2>&1; then
    ge_module_installed "$1"
  else
    [ -f "$(ge_state_dir)/packages/${1}.installed" ]
  fi
}

ge_list_installed_packages() {
  local state
  state="$(ge_state_dir)/modules"
  if [ -d "$state" ]; then
    for f in "$state"/*.json; do
      [ -e "$f" ] || continue
      basename "$f" .json
    done
  fi
  state="$(ge_state_dir)/packages"
  if [ -d "$state" ]; then
    for f in "$state"/*.installed; do
      [ -e "$f" ] || continue
      basename "$f" .installed
    done
  fi | sort -u
}

ge_catalog_path() {
  echo "$(ge_kit_root)/catalog.yaml"
}
