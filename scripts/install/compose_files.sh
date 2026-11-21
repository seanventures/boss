#!/usr/bin/env bash
# THE ONE COMPOSE-FILE RESOLVER. Every path that creates or recreates BOSS's containers (boss.sh,
# install.sh, upgrade.sh, rollback.sh, restore.sh, remote.sh update, checkup.sh --repair,
# self_fix_installer.py, mcp_access.py) gets its `-f` arguments here, so an install's opt-ins
# survive every lifecycle step. Before, only `boss` knew about the engine-sandbox overlay and a
# rollback, restore, remote update or self-fix install silently recreated the containers without
# it.
#
# Engine inner-sandbox support (docs/community/ENGINE_SANDBOX.md) is read from the install's .env
# ONLY, never from the shell's environment:
#
#   BOSS_ENGINE_INNER_SANDBOX=            off (the default): the base compose file alone
#   BOSS_ENGINE_INNER_SANDBOX=seccomp     + BOSS's seccomp profile (Docker Desktop, no AppArmor)
#   BOSS_ENGINE_INNER_SANDBOX=seccomp+apparmor
#                                         + the AppArmor profile boss-engine (Colima, Ubuntu)
#
# When it is on, the overlay is written to <install>/.boss/engine-sandbox.compose.yml with the
# seccomp file's absolute path (Compose does not resolve a relative seccomp= path). It applies to
# api, worker and runner, the services that run coding engines, and adds only security_opt and
# one environment variable: never a capability, privileged, or an unconfined profile.
#
# Sourced: boss_compose_files INSTALL_DIR [ENV_FILE] sets the array BOSS_COMPOSE_FILES.
# Run:     compose_files.sh INSTALL_DIR [ENV_FILE] prints the arguments, one per line.
# INSTALL_DIR is used as given in the -f paths ("." gives relative ones).

boss_engine_sandbox_mode() {
  local env_file="$1" raw
  raw="$(grep -E '^[[:space:]]*BOSS_ENGINE_INNER_SANDBOX=' "$env_file" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
  raw="${raw%%#*}"
  raw="$(printf '%s' "$raw" | tr -d "\"' \t\r" | tr '[:upper:]' '[:lower:]')"
  case "$raw" in
    ""|0|off|false|no|none) echo off ;;
    seccomp) echo seccomp ;;
    seccomp+apparmor|apparmor+seccomp) echo seccomp+apparmor ;;
    *)
      echo "BOSS_ENGINE_INNER_SANDBOX=$raw in $env_file is not one of: seccomp, seccomp+apparmor (or empty for off). docs/community/ENGINE_SANDBOX.md" >&2
      return 1 ;;
  esac
}

_boss_yaml_path() {  # a YAML double-quoted scalar Compose will not interpolate
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//\$/\$\$}"
  printf '"%s"' "$s"
}

boss_compose_files() {
  local dir="$1" env_file="${2:-$1/.env}" mode abs overlay tmp prefix
  prefix="${dir%/}/"; [[ "$dir" != . ]] || prefix=""
  BOSS_COMPOSE_FILES=(-f "${prefix}scripts/install/community-compose.yml")
  mode="$(boss_engine_sandbox_mode "$env_file")" || return 1
  [[ "$mode" != off ]] || return 0
  abs="$(cd "$dir" && pwd -P)" || return 1
  [[ -f "$abs/scripts/sandbox/engine-seccomp.json" ]] || {
    echo "BOSS_ENGINE_INNER_SANDBOX is set but $abs/scripts/sandbox/engine-seccomp.json is missing" >&2
    return 1; }
  overlay="${prefix}.boss/engine-sandbox.compose.yml"
  mkdir -p "$abs/.boss" || return 1
  tmp="$abs/.boss/.engine-sandbox.compose.yml.$$"
  {
    echo "# Generated from BOSS_ENGINE_INNER_SANDBOX=$mode in .env by scripts/install/compose_files.sh."
    echo "# Rewritten on every lifecycle command; edit .env, not this file. docs/community/ENGINE_SANDBOX.md"
    echo "x-boss-engine-sandbox: &boss-engine-sandbox"
    echo "  security_opt:"
    echo "    - $(_boss_yaml_path "seccomp=$abs/scripts/sandbox/engine-seccomp.json")"
    [[ "$mode" != seccomp+apparmor ]] || echo "    - \"apparmor=boss-engine\""
    echo "  environment:"
    echo "    BOSS_ENGINE_INNER_SANDBOX: \"$mode\""
    echo "services:"
    echo "  api: *boss-engine-sandbox"
    echo "  worker: *boss-engine-sandbox"
    echo "  runner: *boss-engine-sandbox"
  } > "$tmp" && mv -f "$tmp" "$abs/.boss/engine-sandbox.compose.yml" || { rm -f "$tmp"; return 1; }
  BOSS_COMPOSE_FILES+=(-f "$overlay")
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  [[ $# -ge 1 ]] || { echo "usage: compose_files.sh INSTALL_DIR [ENV_FILE]" >&2; exit 2; }
  boss_compose_files "$@" || exit 1
  printf '%s\n' "${BOSS_COMPOSE_FILES[@]}"
fi
