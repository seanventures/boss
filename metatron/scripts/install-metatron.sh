#!/usr/bin/env bash
# Zero-Python installer for the native Rust Metatron CLI binary (`MT-11`).
# Detects OS/arch, fetches or copies the per-OS tarball, verifies SHA-256 checksum
# (and optional minisign signature), and installs atomically into --bin-dir.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install-metatron.sh [--bin-dir DIR] [--version SEMVER]
                           [--from-dir LOCAL_RELEASE_DIR | --base-url URL]
                           [--pubkey MINISIGN_PUBKEY]
EOF
}

BIN_DIR="${BOSS_METATRON_BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/boss/bin}"
VERSION="${BOSS_METATRON_VERSION:-}"
FROM_DIR="${BOSS_METATRON_RELEASE_DIR:-}"
BASE_URL="${BOSS_METATRON_RELEASE_URL:-}"
PUBKEY="${BOSS_METATRON_PUBKEY:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir) BIN_DIR="${2:?--bin-dir needs dir}"; shift 2 ;;
    --version) VERSION="${2:?--version needs version}"; shift 2 ;;
    --from-dir) FROM_DIR="${2:?--from-dir needs dir}"; shift 2 ;;
    --base-url) BASE_URL="${2:?--base-url needs url}"; shift 2 ;;
    --pubkey) PUBKEY="${2:?--pubkey needs key}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install-metatron.sh: unknown option: $1" >&2; exit 2 ;;
  esac
done

detect_os() {
  case "$(uname -s)" in
    Darwin) echo darwin ;;
    Linux) echo linux ;;
    other) echo "unsupported OS: $(uname -s)" >&2; exit 2 ;;
    *) echo "unsupported OS: $(uname -s)" >&2; exit 2 ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    arm64|aarch64) echo arm64 ;;
    x86_64|amd64) echo x86_64 ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; exit 2 ;;
  esac
}

OS="$(detect_os)"
ARCH="$(detect_arch)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFY_SCRIPT="$SCRIPT_DIR/verify-release.sh"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if [[ -n "$FROM_DIR" ]]; then
  [[ -f "$FROM_DIR/SHA256SUMS" ]] || { echo "install-metatron.sh: missing SHA256SUMS in $FROM_DIR" >&2; exit 1; }
  cp "$FROM_DIR/SHA256SUMS" "$TMP_DIR/SHA256SUMS"
  [[ -f "$FROM_DIR/SHA256SUMS.minisig" ]] && cp "$FROM_DIR/SHA256SUMS.minisig" "$TMP_DIR/SHA256SUMS.minisig"
  if [[ -n "$VERSION" ]]; then
    ARTIFACT="metatron-${VERSION}-${OS}-${ARCH}.tar.gz"
  else
    ARTIFACT="$(awk -v pat="-${OS}-${ARCH}\\.tar\\.gz\$" '$2 ~ pat { print $2; exit }' "$TMP_DIR/SHA256SUMS")"
  fi
  [[ -n "$ARTIFACT" && -f "$FROM_DIR/$ARTIFACT" ]] || {
    echo "install-metatron.sh: no release archive for ${OS}-${ARCH} in $FROM_DIR" >&2; exit 1;
  }
  cp "$FROM_DIR/$ARTIFACT" "$TMP_DIR/$ARTIFACT"
elif [[ -n "$BASE_URL" ]]; then
  command -v curl >/dev/null 2>&1 || { echo "install-metatron.sh: curl is required for --base-url" >&2; exit 1; }
  BASE="${BASE_URL%/}"
  curl -fsSL "$BASE/SHA256SUMS" -o "$TMP_DIR/SHA256SUMS"
  if [[ -n "$PUBKEY" ]]; then
    curl -fsSL "$BASE/SHA256SUMS.minisig" -o "$TMP_DIR/SHA256SUMS.minisig"
  fi
  if [[ -n "$VERSION" ]]; then
    ARTIFACT="metatron-${VERSION}-${OS}-${ARCH}.tar.gz"
  else
    ARTIFACT="$(awk -v pat="-${OS}-${ARCH}\\.tar\\.gz\$" '$2 ~ pat { print $2; exit }' "$TMP_DIR/SHA256SUMS")"
  fi
  [[ -n "$ARTIFACT" ]] || { echo "install-metatron.sh: no release archive for ${OS}-${ARCH} in SHA256SUMS" >&2; exit 1; }
  curl -fsSL "$BASE/$ARTIFACT" -o "$TMP_DIR/$ARTIFACT"
else
  echo "install-metatron.sh: pass --from-dir PATH or --base-url URL" >&2
  exit 2
fi

VERIFY_ARGS=(--archive "$TMP_DIR/$ARTIFACT" --checksums "$TMP_DIR/SHA256SUMS")
if [[ -n "$PUBKEY" ]]; then
  VERIFY_ARGS+=(--sig "$TMP_DIR/SHA256SUMS.minisig" --pubkey "$PUBKEY")
fi
bash "$VERIFY_SCRIPT" "${VERIFY_ARGS[@]}" >/dev/null

tar -C "$TMP_DIR" -xzf "$TMP_DIR/$ARTIFACT"
BIN_FOUND="$(find "$TMP_DIR" -maxdepth 2 -type f -name metatron | head -n 1)"
[[ -n "$BIN_FOUND" ]] || { echo "install-metatron.sh: archive $ARTIFACT did not contain a metatron binary" >&2; exit 1; }

mkdir -p "$BIN_DIR"
DEST="$BIN_DIR/metatron"
cp "$BIN_FOUND" "${DEST}.new.$$"
chmod 0755 "${DEST}.new.$$"
mv -f "${DEST}.new.$$" "$DEST"

printf 'Installed verified metatron (%s-%s) to %s\n' "$OS" "$ARCH" "$DEST"
