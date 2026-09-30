#!/usr/bin/env bash
# Modular package manager — install / upgrade / uninstall / check.
# shellcheck source=common.sh
# Sourced by ge after common.sh.

ge_modules_dir() {
  local root
  root="$(ge_kit_root)"
  if [ -d "$root/modules" ]; then
    echo "$root/modules"
  elif [ -d "$root/packages" ]; then
    echo "$root/packages"
  else
    echo "$root/modules"
  fi
}

ge_module_dir() {
  local id="$1"
  local base
  base="$(ge_modules_dir)"
  if [ -d "$base/$id" ]; then
    echo "$base/$id"
    return
  fi
  # external modules (survive kit upgrade)
  local ext="${GOLDENEYE_MODULES_D:-/var/lib/goldeneye/modules.d}"
  if [ -d "$ext/$id" ]; then
    echo "$ext/$id"
    return
  fi
  return 1
}

ge_module_manifest() {
  local id="$1" dir
  dir="$(ge_module_dir "$id")" || return 1
  if [ -f "$dir/package.yaml" ]; then
    echo "$dir/package.yaml"
    return
  fi
  return 1
}

ge_module_field() {
  # ge_module_field <id> <field>
  local id="$1" field="$2" mf
  mf="$(ge_module_manifest "$id")" || { echo ""; return 0; }
  python3 - "$mf" "$field" <<'PY' 2>/dev/null || true
import sys
path, field = sys.argv[1], sys.argv[2]
try:
    import yaml
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
except Exception:
    data = {}
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if s.startswith("#") or ":" not in s:
            continue
        k, _, v = s.partition(":")
        data[k.strip()] = v.strip().strip('"').strip("'")
val = data.get(field, "")
if isinstance(val, list):
    print(" ".join(str(x) for x in val))
elif isinstance(val, bool):
    print("true" if val else "false")
elif val is None:
    print("")
else:
    print(val)
PY
}

ge_module_state_path() {
  echo "$(ge_state_dir)/modules/${1}.json"
}

ge_module_installed() {
  local id="$1"
  [ -f "$(ge_module_state_path "$id")" ] && return 0
  # legacy marker
  [ -f "$(ge_state_dir)/packages/${id}.installed" ]
}

ge_module_installed_version() {
  local id="$1" sp
  sp="$(ge_module_state_path "$id")"
  if [ -f "$sp" ]; then
    python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "$sp" 2>/dev/null || echo ""
    return
  fi
  if [ -f "$(ge_state_dir)/packages/${id}.installed" ]; then
    echo "0.1.0"
    return
  fi
  echo ""
}

ge_module_mark() {
  local id="$1" version="$2" action="${3:-install}"
  local dir state
  dir="$(ge_state_dir)/modules"
  mkdir -p "$dir" "$(ge_state_dir)/packages"
  version="${version:-$(ge_module_field "$id" version)}"
  version="${version:-0.0.0}"
  python3 - "$dir/${id}.json" "$id" "$version" "$action" <<'PY'
import json, sys, datetime
path, mid, ver, action = sys.argv[1:5]
now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
prev = {}
try:
    prev = json.load(open(path))
except Exception:
    pass
doc = {
    "id": mid,
    "version": ver,
    "api_version": 1,
    "installed_at": prev.get("installed_at") or now,
    "updated_at": now,
    "last_action": action,
}
json.dump(doc, open(path, "w"), indent=2)
print(path)
PY
  # legacy stamp for older tools
  date -u +%Y-%m-%dT%H:%M:%SZ >"$(ge_state_dir)/packages/${id}.installed"
}

ge_list_module_ids() {
  local base
  base="$(ge_modules_dir)"
  [ -d "$base" ] || return 0
  for d in "$base"/*/; do
    [ -d "$d" ] || continue
    [ -f "${d}package.yaml" ] || [ -f "${d}install.sh" ] || continue
    basename "$d"
  done | sort
  local ext="${GOLDENEYE_MODULES_D:-/var/lib/goldeneye/modules.d}"
  if [ -d "$ext" ]; then
    for d in "$ext"/*/; do
      [ -d "$d" ] || continue
      [ -f "${d}package.yaml" ] || continue
      basename "$d"
    done | sort
  fi
}

ge_module_depends() {
  ge_module_field "$1" depends_on
}

ge_semver_lt() {
  # return 0 if $1 < $2 (simple major.minor.patch)
  python3 - "$1" "$2" <<'PY'
import sys
def parse(v):
    parts = []
    for p in str(v).strip().lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])
a, b = parse(sys.argv[1]), parse(sys.argv[2])
sys.exit(0 if a < b else 1)
PY
}

ge_module_run_hook() {
  local id="$1" action="$2" force="${3:-0}"
  local dir hook version
  dir="$(ge_module_dir "$id")" || ge_die "unknown module: $id"
  version="$(ge_module_field "$id" version)"
  version="${version:-0.0.0}"

  case "$action" in
    install|upgrade)
      hook="$(ge_module_field "$id" hooks)" # unused aggregate
      # prefer explicit files
      if [ "$action" = "upgrade" ] && [ -f "$dir/upgrade.sh" ]; then
        hook="$dir/upgrade.sh"
      elif [ -f "$dir/install.sh" ]; then
        hook="$dir/install.sh"
      else
        ge_die "module $id has no install.sh"
      fi
      ;;
    uninstall)
      hook="$dir/uninstall.sh"
      [ -f "$hook" ] || { ge_info "no uninstall.sh for $id — removing state only"; ge_module_unmark "$id"; return 0; }
      ;;
    check)
      hook="$dir/check.sh"
      [ -f "$hook" ] || { ge_info "no check.sh for $id"; return 0; }
      ;;
    *) ge_die "unknown module action: $action" ;;
  esac

  if [ "$action" = "install" ] && ge_module_installed "$id" && [ "$force" != "1" ]; then
    local cur want
    cur="$(ge_module_installed_version "$id")"
    want="$version"
    if [ -n "$cur" ] && [ "$cur" = "$want" ]; then
      ge_info "module already installed: $id@$cur (use --force or ge module upgrade)"
      return 0
    fi
    if [ -n "$cur" ] && ge_semver_lt "$cur" "$want"; then
      ge_info "installed $id@$cur < $want — upgrading"
      action="upgrade"
    fi
  fi

  if [ "$action" = "install" ] || [ "$action" = "upgrade" ]; then
    if [ "$force" != "1" ]; then
      local dep
      for dep in $(ge_module_depends "$id"); do
        [ -n "$dep" ] || continue
        if ! ge_module_installed "$dep"; then
          ge_die "missing dependency $dep (install it first, or pass --force)"
        fi
      done
    fi
  fi

  ge_step "$action module $id@$version"
  export GOLDENEYE_KIT_ROOT="$(ge_kit_root)"
  export GOLDENEYE_SITE="$(ge_site_file)"
  export GOLDENEYE_STATE_ROOT="$(ge_state_dir)"
  export GOLDENEYE_MODULE_ID="$id"
  export GOLDENEYE_MODULE_VERSION="$version"
  export GOLDENEYE_MODULE_ACTION="$action"
  if [ -x "$hook" ]; then
    "$hook"
  else
    bash "$hook"
  fi
  if [ "$action" = "install" ] || [ "$action" = "upgrade" ]; then
    ge_module_mark "$id" "$version" "$action"
  elif [ "$action" = "uninstall" ]; then
    ge_module_unmark "$id"
  fi
  ge_info "$action complete: $id@$version"
}

ge_module_unmark() {
  local id="$1"
  rm -f "$(ge_module_state_path "$id")"
  rm -f "$(ge_state_dir)/packages/${id}.installed"
}

ge_catalog_build() {
  local out="${1:-$(ge_kit_root)/catalog.yaml}"
  python3 - "$(ge_modules_dir)" "$out" "$(ge_kit_version)" <<'PY'
import sys
from pathlib import Path

mod_root = Path(sys.argv[1])
out = Path(sys.argv[2])
kit_ver = sys.argv[3]

try:
    import yaml
except ImportError:
    yaml = None

packages = []
for d in sorted(mod_root.iterdir() if mod_root.is_dir() else []):
    if not d.is_dir():
        continue
    mf = d / "package.yaml"
    if not mf.is_file():
        # legacy: only install.sh
        if (d / "install.sh").is_file():
            packages.append({
                "id": d.name,
                "name": d.name,
                "version": "0.0.0",
                "layer": 9,
                "kind": "optional",
                "description": "",
                "depends_on": [],
                "optional": True,
                "recipe": f"modules/{d.name}",
                "path": f"modules/{d.name}",
            })
        continue
    if yaml:
        data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    else:
        data = {"id": d.name, "name": d.name, "version": "0.0.0", "layer": 9, "kind": "optional", "depends_on": []}
        for line in mf.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if ":" not in s or s.startswith("#"):
                continue
            k, _, v = s.partition(":")
            data[k.strip()] = v.strip().strip('"').strip("'")
    packages.append({
        "id": data.get("id") or d.name,
        "name": data.get("name") or d.name,
        "version": str(data.get("version") or "0.0.0"),
        "layer": int(data.get("layer") or 9),
        "kind": data.get("kind") or "optional",
        "description": str(data.get("description") or "").strip(),
        "depends_on": list(data.get("depends_on") or []),
        "optional": bool(data.get("optional", False)),
        "provides": list(data.get("provides") or []),
        "min_kit_version": data.get("min_kit_version"),
        "recipe": f"modules/{d.name}",
        "path": f"modules/{d.name}",
        "api_version": int(data.get("api_version") or 1),
    })

bootstrap = [
    "layer0-host", "layer1-runtime", "layer2-edge", "layer3-control", "deploy-cli"
]
catalog = {
    "version": 2,
    "api_version": 1,
    "kit_version": kit_ver,
    "source": "modules/*/package.yaml",
    "default_bootstrap": bootstrap,
    "packages": packages,
}

if yaml:
    out.write_text(
        "# GENERATED by `ge catalog build` — edit modules/*/package.yaml, not this file.\n"
        + yaml.safe_dump(catalog, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
else:
    # minimal fallback
    lines = [
        "# GENERATED by ge catalog build",
        "version: 2",
        f"kit_version: \"{kit_ver}\"",
        "default_bootstrap:",
    ]
    for b in bootstrap:
        lines.append(f"  - {b}")
    lines.append("packages:")
    for p in packages:
        lines.append(f"  - id: {p['id']}")
        lines.append(f"    name: {p['name']}")
        lines.append(f"    version: \"{p['version']}\"")
        lines.append(f"    layer: {p['layer']}")
        lines.append(f"    kind: {p['kind']}")
        lines.append(f"    recipe: {p['recipe']}")
        lines.append(f"    optional: {str(p['optional']).lower()}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(out)
PY
}

ge_kit_version() {
  local root vfile
  root="$(ge_kit_root)"
  vfile="$root/VERSION"
  if [ -f "$vfile" ]; then
    tr -d '[:space:]' <"$vfile"
  else
    echo "${GE_VERSION:-0.0.0}"
  fi
}

ge_upgrade_kit() {
  # ge_upgrade_kit [source-dir]
  # Replace kit tree contents from source (git checkout / release tarball), preserve state.
  local src="${1:-}"
  ge_require_root
  local root state
  root="$(ge_kit_root)"
  state="$(ge_state_dir)"
  if [ -z "$src" ]; then
    ge_die "usage: ge upgrade <path-to-new-kit-tree>  (or set GOLDENEYE_UPGRADE_URL later)"
  fi
  [ -d "$src" ] || ge_die "upgrade source not a directory: $src"
  [ -f "$src/VERSION" ] || ge_die "upgrade source missing VERSION: $src"
  local new_ver old_ver
  new_ver="$(tr -d '[:space:]' <"$src/VERSION")"
  old_ver="$(ge_kit_version)"
  ge_step "upgrade kit $old_ver → $new_ver"
  # rsync kit (preserve site and state outside tree)
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude '.git' \
      --exclude '__pycache__' \
      "$src/" "$root/"
  else
    cp -a "$src/." "$root/"
  fi
  # reinstall CLI entrypoints
  if [ -x "$root/core/bin/ge" ]; then
    install -m 0755 "$root/core/bin/ge" /usr/local/bin/ge
  elif [ -x "$root/bin/ge" ]; then
    install -m 0755 "$root/bin/ge" /usr/local/bin/ge
  fi
  echo "$new_ver" >"$state/kit-version"
  date -u +%Y-%m-%dT%H:%M:%SZ >"$state/upgraded-at"
  # rebuild catalog
  GOLDENEYE_KIT_ROOT="$root" ge_catalog_build "$root/catalog.yaml" || true
  # upgrade installed modules that are behind
  ge_step "upgrade installed modules"
  local id cur want
  for id in $(ge_list_module_ids); do
    ge_module_installed "$id" || continue
    cur="$(ge_module_installed_version "$id")"
    want="$(ge_module_field "$id" version)"
    want="${want:-0.0.0}"
    if [ -z "$cur" ] || ge_semver_lt "$cur" "$want"; then
      ge_info "module $id: $cur → $want"
      ge_module_run_hook "$id" upgrade 0 || ge_info "module $id upgrade failed (continuing)"
    else
      ge_info "module $id@$cur current"
    fi
  done
  ge_info "kit upgrade complete: $new_ver"
}
