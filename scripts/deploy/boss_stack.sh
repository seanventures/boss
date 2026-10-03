#!/usr/bin/env bash
# BOSS deploying BOSS: the steps boss.deploy.yaml declares, run on the host by a governed
# stack deployment (docs/community/DEPLOY_A_STACK.md).
#
#   boss_stack.sh install  --dir DIR --commit SHA --state DIR/.boss/boss --origin https://HOST
#   boss_stack.sh update   (same flags)
#   boss_stack.sh https    --origin https://HOST [--web-port 3000]
#   boss_stack.sh teardown --dir DIR
#
# NOTHING HERE REPLACES THE INSTALLER. install/update run the repository's own install.sh with
# the flags README.md documents for an unattended install; this file only does what a stack
# deployment's layout needs around it:
#
#   * The deployment extracts each commit to DIR/releases/SHA and points DIR/current at it.
#     That is a shipped tree, exactly what remote.sh produces, so it is stamped with
#     .boss-shipped-commit and install.sh uses it as the source without fetching anything.
#   * install.sh keeps its credentials in INSTALL_DIR/.env. A new release directory has none,
#     and an installer that finds none generates new ones — which no longer open the Postgres
#     volume the previous release created. So .env (and the shell env file) live in STATE and
#     are copied into each release before the installer runs, and back out after it, even when
#     it fails part-way (it may have written fresh credentials the database already uses).
#   * An update backs up first with scripts/install/backup.sh, as remote.sh update does.
#
# Credentials are never printed: only file names are mentioned.
set -euo pipefail
umask 077

die() { echo "boss_stack: $*" >&2; exit 2; }

action="${1:-}"; shift || true
DIR="" COMMIT="" STATE="" ORIGIN="" WEB_PORT=3000
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="${2:?--dir needs a path}"; shift 2 ;;
    --commit) COMMIT="${2:?--commit needs a sha}"; shift 2 ;;
    --state) STATE="${2:?--state needs a path}"; shift 2 ;;
    --origin) ORIGIN="${2:?--origin needs an https origin}"; shift 2 ;;
    --web-port) WEB_PORT="${2:?--web-port needs a number}"; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done

PRIVATE_FILES=(.env boss-community.env.sh)

carry_in() {
  mkdir -p "$STATE"
  chmod 700 "$STATE"
  local f
  for f in "${PRIVATE_FILES[@]}"; do
    if [[ -f "$STATE/$f" ]]; then
      cp -p "$STATE/$f" "$DIR/$f"
      chmod 600 "$DIR/$f"
    fi
  done
}

carry_out() {
  local f
  for f in "${PRIVATE_FILES[@]}"; do
    if [[ -f "$DIR/$f" ]]; then
      install -m 600 "$DIR/$f" "$STATE/$f"
    fi
  done
}

run_installer() {
  [[ -n "$DIR" && -n "$COMMIT" && -n "$STATE" && -n "$ORIGIN" ]] ||
    die "$action needs --dir, --commit, --state and --origin"
  [[ "$COMMIT" =~ ^[0-9a-fA-F]{7,40}$ ]] || die "--commit must be a commit sha"
  [[ -f "$DIR/install.sh" && -f "$DIR/scripts/install/community-compose.yml" ]] ||
    die "$DIR is not a BOSS source tree"
  printf '%s\n' "$COMMIT" > "$DIR/.boss-shipped-commit"
  carry_in
  if [[ "$action" == update && -f "$DIR/.env" ]]; then
    echo "boss_stack: backing up the running install before the update"
    bash "$DIR/scripts/install/backup.sh" --dir "$DIR"
  fi
  local rc=0
  # README.md "Deploy", unattended: --yes answers no to every optional question and
  # --skip-model-key leaves models to be connected from the console.
  bash "$DIR/install.sh" --dir "$DIR" --yes --skip-model-key --console-origin "$ORIGIN" || rc=$?
  carry_out
  return "$rc"
}

# README.md step 3, "Put HTTPS in front", as a command. It never replaces a Caddyfile that
# serves anything else: it writes only over a missing file, the package's default, or the
# block it wrote itself, and otherwise stops and prints the block to add by hand.
https_proxy() {
  [[ -n "$ORIGIN" ]] || die "https needs --origin"
  case "$ORIGIN" in
    https://*) ;;
    *) echo "boss_stack: $ORIGIN is not https; no proxy configured"; return 0 ;;
  esac
  local host="${ORIGIN#https://}"
  host="${host%%/*}"
  [[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] || die "cannot proxy $host: use a hostname or an IPv4 address"
  local tls=""
  [[ "$host" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && tls=$'\n    tls internal'
  local marker="# Written by BOSS (scripts/deploy/boss_stack.sh https) for $host"
  local block
  block="$marker
$host {$tls
    reverse_proxy 127.0.0.1:$WEB_PORT {
        header_up Host $host
        header_up X-Forwarded-Host $host
        header_up X-Forwarded-Proto https
    }
}"
  local caddyfile=/etc/caddy/Caddyfile
  if ! command -v caddy >/dev/null 2>&1; then
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y caddy
  fi
  # The package default serves a static page and proxies nothing; any live proxy or app
  # directive means someone else's site is in this file.
  if [[ -f "$caddyfile" ]] && ! grep -qF "$marker" "$caddyfile" &&
     grep -qE '^[^#]*(reverse_proxy|php_fastcgi|redir)' "$caddyfile"; then
    echo "boss_stack: $caddyfile already serves other sites; it was left unchanged." >&2
    echo "boss_stack: add this block to it, then run: sudo systemctl reload caddy" >&2
    printf '%s\n' "$block" >&2
    return 1
  fi
  local tmp
  tmp="$(mktemp)"
  printf '%s\n' "$block" > "$tmp"
  sudo -n caddy validate --adapter caddyfile --config "$tmp" >/dev/null
  sudo -n install -m 644 "$tmp" "$caddyfile"
  rm -f "$tmp"
  sudo -n systemctl reload caddy || sudo -n systemctl restart caddy
  echo "boss_stack: Caddy serves $ORIGIN -> 127.0.0.1:$WEB_PORT"
}

case "$action" in
  install|update) run_installer ;;
  https) https_proxy ;;
  teardown)
    [[ -n "$DIR" ]] || die "teardown needs --dir"
    # The safe uninstall: stops the stack, keeps data, credentials and backups.
    bash "$DIR/scripts/install/uninstall.sh" --dir "$DIR" --yes ;;
  *) die "usage: boss_stack.sh install|update|https|teardown [options]" ;;
esac
