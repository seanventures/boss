#!/usr/bin/env bash
# Verify SHA-256 checksum (and optional minisign signature) of a Metatron release tarball (`MT-11`).
# Fails closed on missing entry, checksum mismatch, or signature failure.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: verify-release.sh --archive PATH --checksums PATH [--sig PATH --pubkey KEY_OR_PATH]
EOF
}

ARCHIVE=""
CHECKSUMS=""
SIG_FILE=""
PUBKEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) ARCHIVE="${2:?--archive needs path}"; shift 2 ;;
    --checksums) CHECKSUMS="${2:?--checksums needs path}"; shift 2 ;;
    --sig) SIG_FILE="${2:?--sig needs path}"; shift 2 ;;
    --pubkey) PUBKEY="${2:?--pubkey needs key}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "verify-release.sh: unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$ARCHIVE" ]] || { echo "verify-release.sh: archive not found: $ARCHIVE" >&2; exit 2; }
[[ -f "$CHECKSUMS" ]] || { echo "verify-release.sh: checksums not found: $CHECKSUMS" >&2; exit 2; }

if [[ -n "$SIG_FILE" || -n "$PUBKEY" ]]; then
  [[ -f "$SIG_FILE" && -n "$PUBKEY" ]] || { echo "verify-release.sh: both --sig and --pubkey are required for signature verification" >&2; exit 2; }
  command -v minisign >/dev/null 2>&1 || { echo "verify-release.sh: minisign is required to verify $SIG_FILE" >&2; exit 2; }
  if [[ -f "$PUBKEY" ]]; then
    minisign -Vm "$CHECKSUMS" -x "$SIG_FILE" -p "$PUBKEY" >/dev/null
  else
    minisign -Vm "$CHECKSUMS" -x "$SIG_FILE" -P "$PUBKEY" >/dev/null
  fi
fi

NAME="$(basename "$ARCHIVE")"
EXPECTED="$(awk -v target="$NAME" '$2 == target { print $1; exit }' "$CHECKSUMS")"
[[ -n "$EXPECTED" ]] || { echo "verify-release.sh: no checksum entry for $NAME in $CHECKSUMS" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
else
  ACTUAL="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
fi

if [[ "$ACTUAL" != "$EXPECTED" ]]; then
  echo "verify-release.sh: SHA-256 mismatch for $NAME (expected $EXPECTED, got $ACTUAL)" >&2
  exit 1
fi

printf 'OK %s %s\n' "$NAME" "$ACTUAL"
