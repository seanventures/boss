#!/bin/sh
# Load (or remove) BOSS's AppArmor profile for engine inner-sandbox support on the DOCKER HOST:
# the Linux machine, or the VM Docker runs in (Colima: `colima --profile boss ssh -- sudo ...`).
# docs/community/ENGINE_SANDBOX.md.
#
#   sudo scripts/sandbox/load_apparmor.sh            # install + load /etc/apparmor.d/boss-engine
#   sudo scripts/sandbox/load_apparmor.sh --check    # say whether it is loaded, change nothing
#   sudo scripts/sandbox/load_apparmor.sh --remove   # unload and delete it
#
# AppArmor 4 (Ubuntu 24.04) mediates user namespaces and needs the profile's `userns,` rule.
# AppArmor 3 has no such rule (the parser rejects it) and does not mediate user namespaces, so
# there the rule is left out instead of the load failing. A host without AppArmor needs nothing:
# Docker ignores the apparmor= option there.
set -eu

NAME=boss-engine
HERE=$(CDPATH= cd "$(dirname "$0")" && pwd)
SRC="$HERE/boss-engine.apparmor"
DEST="/etc/apparmor.d/$NAME"
SECFS=/sys/kernel/security/apparmor
MODE="${1:-load}"

loaded() { [ -r "$SECFS/profiles" ] && grep -q "^$NAME (" "$SECFS/profiles"; }

if [ ! -d "$SECFS" ]; then
  echo "AppArmor is not enabled on this host: nothing to load. Docker ignores apparmor=$NAME here;"
  echo "the seccomp profile alone applies (BOSS_ENGINE_INNER_SANDBOX=seccomp)."
  exit 0
fi
case "$MODE" in
  --check)
    if loaded; then echo "loaded: $NAME"; exit 0; fi
    echo "not loaded: $NAME. Load it: sudo $0"; exit 1 ;;
  --remove)
    [ "$(id -u)" = 0 ] || { echo "run as root (sudo $0 --remove)" >&2; exit 2; }
    if [ -f "$DEST" ]; then apparmor_parser -R "$DEST" 2>/dev/null || true; rm -f "$DEST"; fi
    echo "removed: $NAME"; exit 0 ;;
  load|--load) ;;
  *) echo "usage: $0 [--check|--remove]" >&2; exit 2 ;;
esac

[ "$(id -u)" = 0 ] || { echo "run as root (sudo $0)" >&2; exit 2; }
command -v apparmor_parser >/dev/null 2>&1 ||
  { echo "apparmor_parser is missing: install the apparmor package, then run this again" >&2; exit 1; }

TMP=$(mktemp)
trap 'rm -f "$TMP" "$TMP.out"' EXIT
if apparmor_parser -Q -K "$SRC" >"$TMP.out" 2>&1; then
  cp "$SRC" "$TMP"
  echo "AppArmor with user-namespace rules: loading the full profile"
else
  grep -v '^[[:space:]]*userns,[[:space:]]*$' "$SRC" > "$TMP"
  if ! apparmor_parser -Q -K "$TMP" >"$TMP.out" 2>&1; then
    echo "apparmor_parser rejects the profile:" >&2; cat "$TMP.out" >&2; exit 1
  fi
  if [ -e "$SECFS/features/namespaces/userns_create" ]; then
    # The kernel mediates user namespaces but this parser cannot write the rule that allows them.
    echo "this kernel restricts user namespaces through AppArmor, but apparmor_parser is too old" >&2
    echo "to grant them (it has no 'userns' rule). Upgrade the apparmor package (4.0 or later)." >&2
    exit 1
  fi
  echo "AppArmor 3 (user namespaces not mediated): loading the profile without the userns rule"
fi
install -m 0644 "$TMP" "$DEST"
apparmor_parser -r -W "$DEST"
loaded && echo "loaded: $NAME (survives reboots: $DEST)"
