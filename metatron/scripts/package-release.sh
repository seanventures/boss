#!/usr/bin/env bash
# Package a built Rust Metatron binary with Apache-2.0 notices and SHA-256 checksums (`MT-11`).
# Optionally signs SHA256SUMS with minisign if METATRON_MINISIGN_KEY is set.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: package-release.sh --bin PATH --version SEMVER --os darwin|linux --arch arm64|x86_64 --out-dir DIR
EOF
}

BIN_PATH=""
VERSION=""
TARGET_OS=""
TARGET_ARCH=""
OUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin) BIN_PATH="${2:?--bin needs path}"; shift 2 ;;
    --version) VERSION="${2:?--version needs semver}"; shift 2 ;;
    --os) TARGET_OS="${2:?--os needs darwin|linux}"; shift 2 ;;
    --arch) TARGET_ARCH="${2:?--arch needs arm64|x86_64}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:?--out-dir needs directory}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "package-release.sh: unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -x "$BIN_PATH" ]] || { echo "package-release.sh: binary not executable: $BIN_PATH" >&2; exit 2; }
[[ -n "$VERSION" && -n "$TARGET_OS" && -n "$TARGET_ARCH" && -n "$OUT_DIR" ]] || { usage >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
mkdir -p "$OUT_DIR"

ARTIFACT="metatron-${VERSION}-${TARGET_OS}-${TARGET_ARCH}.tar.gz"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PKG_DIR="$STAGE/metatron-${VERSION}-${TARGET_OS}-${TARGET_ARCH}"
mkdir -p "$PKG_DIR"
cp "$BIN_PATH" "$PKG_DIR/metatron"
chmod 0755 "$PKG_DIR/metatron"
[[ -f "$REPO_ROOT/LICENSE" ]] && cp "$REPO_ROOT/LICENSE" "$PKG_DIR/LICENSE"
[[ -f "$REPO_ROOT/THIRD-PARTY-NOTICES.md" ]] && cp "$REPO_ROOT/THIRD-PARTY-NOTICES.md" "$PKG_DIR/THIRD-PARTY-NOTICES.md"
[[ -f "$REPO_ROOT/metatron/THIRD-PARTY" ]] && cp "$REPO_ROOT/metatron/THIRD-PARTY" "$PKG_DIR/THIRD-PARTY"

tar -C "$STAGE" -czf "$OUT_DIR/$ARTIFACT" "metatron-${VERSION}-${TARGET_OS}-${TARGET_ARCH}"

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

SUM="$(sha256_file "$OUT_DIR/$ARTIFACT")"
SUMS_FILE="$OUT_DIR/SHA256SUMS"
if [[ -f "$SUMS_FILE" ]]; then
  grep -v "  ${ARTIFACT}\$" "$SUMS_FILE" > "${SUMS_FILE}.tmp" || true
  mv -f "${SUMS_FILE}.tmp" "$SUMS_FILE"
fi
printf '%s  %s\n' "$SUM" "$ARTIFACT" >> "$SUMS_FILE"

if [[ -n "${METATRON_MINISIGN_KEY:-}" ]] && command -v minisign >/dev/null 2>&1; then
  minisign -Sm "$SUMS_FILE" -s "$METATRON_MINISIGN_KEY"
fi

printf '%s\n' "$OUT_DIR/$ARTIFACT"
