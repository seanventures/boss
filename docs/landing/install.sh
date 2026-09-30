#!/usr/bin/env bash
# BOSS installer, served at https://boss.sean.network/install.sh
#
#   curl -fsSL https://boss.sean.network/install.sh | bash
#   curl -fsSL https://boss.sean.network/install.sh | bash -s -- --console-origin https://boss.example.com
#
# Downloads the installer from the reviewed release commit below and runs it with that
# same commit pinned, so the checkout, and any rerun of this command, stays on the release.
# All arguments are passed through to install.sh; run with --help to see them.

set -euo pipefail

BOSS_RELEASE="v1.0-rc1"
BOSS_RELEASE_COMMIT="7b88a18a2b4cd0b14b09a47fc415f7ffd4580066"
BOSS_REPO="seanventures/boss"

main() {
  command -v curl >/dev/null 2>&1 || { echo "BOSS needs curl to install." >&2; exit 1; }

  local tmp
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT

  echo "Installing BOSS $BOSS_RELEASE (${BOSS_RELEASE_COMMIT:0:7})"
  if ! curl -fsSL "https://raw.githubusercontent.com/$BOSS_REPO/$BOSS_RELEASE_COMMIT/install.sh" -o "$tmp"; then
    echo "Could not download the BOSS $BOSS_RELEASE installer from GitHub." >&2
    exit 22
  fi

  BOSS_REPO_REF="$BOSS_RELEASE_COMMIT" bash "$tmp" "$@"
}

# Everything runs from main, so a partially downloaded script never executes.
main "$@"
