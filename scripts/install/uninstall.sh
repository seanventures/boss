#!/usr/bin/env bash
# Remove a BOSS Community install, so the next one starts from nothing.
#
# WHY THIS EXISTS. An installer that cannot be undone can only be tested once. Proving the
# setup works means running it on a clean machine repeatedly, and "clean" has to mean
# something precise: no containers, no images, no volumes, no .env — not "I deleted the
# folder and hoped".
#
# WHAT IT REFUSES TO DO QUIETLY. The database holds every run, decision and cost record, and
# the .env holds the secret-store encryption key — the only thing that can read what the
# secret store wrote. Both are destroyed here and neither is recoverable, so nothing happens
# without a typed confirmation unless --yes is passed.
set -uo pipefail

INSTALL_DIR="${BOSS_INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)}"
ASSUME_YES=0
KEEP_ENV=0
KEEP_IMAGES=0

# A WHILE LOOP, BECAUSE `--dir PATH` TAKES A VALUE.
#
# This was `for arg in "$@"` with a `shift` inside it. A for-loop has already captured the list,
# so shifting moves $1 without moving the loop — `--dir /opt/boss` set INSTALL_DIR to the
# literal string "--dir" and silently ignored the path. It went unnoticed because the paths
# that matter were computed from the script's own location before any of this ran, so the flag
# had no effect either way: wrong twice, cancelling out, until someone passed --dir and meant it.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)      ASSUME_YES=1 ;;
    --keep-env)    KEEP_ENV=1 ;;
    --keep-images) KEEP_IMAGES=1 ;;
    --dir)         INSTALL_DIR="${2:?--dir needs a path}"; shift ;;
    -h|--help)
      cat <<'USAGE'
usage: uninstall.sh [--yes] [--keep-env] [--keep-images] [--dir PATH]

  --yes          do not ask (for scripted reinstall loops)
  --keep-env     leave .env in place — keeps the secret-store encryption key and your
                 API key, so a reinstall reuses the same credentials
  --keep-images  leave the built images, so a reinstall skips the slow rebuild
  --dir PATH     the install directory (default: this script's repo root)

Removes: containers, volumes (INCLUDING THE DATABASE), images, .env, and the generated
env shim. Does not remove the source checkout.
USAGE
      exit 0 ;;
    *) printf "unknown option: %s (try --help)\n" "$1" >&2; exit 2 ;;
  esac
  shift
done

# AFTER parsing, so --dir actually points them somewhere.
COMPOSE="$INSTALL_DIR/scripts/install/community-compose.yml"
ENV_FILE="$INSTALL_DIR/.env"

G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'
say() { printf "  %s\n" "$1"; }

printf "\n${B}Uninstall BOSS Community${N}  (%s)\n\n" "$INSTALL_DIR"
printf "  This removes:\n"
printf "    • all boss-community containers\n"
printf "    ${R}• the database volume — every run, decision and cost record${N}\n"
[ "$KEEP_IMAGES" -eq 0 ] && printf "    • the built images (a reinstall rebuilds them)\n"
[ "$KEEP_ENV" -eq 0 ] && printf "    ${R}• .env — including the secret-store encryption key, which is unrecoverable${N}\n"
printf "\n"

if [ "$ASSUME_YES" -ne 1 ]; then
  printf "  Type ${B}remove${N} to continue: "
  read -r reply
  [ "$reply" = "remove" ] || { printf "\n  Nothing was removed.\n\n"; exit 1; }
  printf "\n"
fi

# Compose first, so it takes its own volumes and network with it. Every profile, or the
# profiled services are left orphaned.
if [ -f "$COMPOSE" ]; then
  docker compose -f "$COMPOSE" --env-file "$ENV_FILE" --profile web --profile secrets \
    down --volumes --remove-orphans >/dev/null 2>&1 \
    && say "${G}✓${N} compose stack down (containers, volumes, network)" \
    || say "${Y}!${N} compose down reported an error — falling back to direct removal"
fi

# Belt and braces: anything the compose file no longer knows about.
left=$(docker ps -aq --filter "name=boss-community" 2>/dev/null)
[ -n "$left" ] && docker rm -f $left >/dev/null 2>&1 && say "${G}✓${N} removed leftover containers"
vols=$(docker volume ls -q 2>/dev/null | grep -E 'boss[-_]community' || true)
[ -n "$vols" ] && docker volume rm $vols >/dev/null 2>&1 && say "${G}✓${N} removed leftover volumes"

if [ "$KEEP_IMAGES" -eq 0 ]; then
  imgs=$(docker images -q 'boss-community-*' 2>/dev/null)
  [ -n "$imgs" ] && docker rmi -f $imgs >/dev/null 2>&1 && say "${G}✓${N} removed built images"
else
  say "  kept images (--keep-images): a reinstall will skip the rebuild"
fi

if [ "$KEEP_ENV" -eq 0 ]; then
  [ -f "$ENV_FILE" ] && rm -f "$ENV_FILE" && say "${G}✓${N} removed .env"
  rm -f "$INSTALL_DIR/boss-community.env.sh" 2>/dev/null && say "${G}✓${N} removed env shim"
else
  say "  kept .env (--keep-env): a reinstall reuses the same keys and credentials"
fi

printf "\n  ${G}${B}Clean.${N}  Reinstall: %s/install.sh\n\n" "$INSTALL_DIR"
