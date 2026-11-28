#!/usr/bin/env bash
# Render a Homebrew Formula (`Formula/metatron.rb`) from version + SHA256SUMS (`MT-11`).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: render-homebrew-formula.sh --version SEMVER --base-url URL --checksums PATH [--out PATH]
EOF
}

VERSION=""
BASE_URL=""
CHECKSUMS=""
OUT_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:?--version needs semver}"; shift 2 ;;
    --base-url) BASE_URL="${2:?--base-url needs url}"; shift 2 ;;
    --checksums) CHECKSUMS="${2:?--checksums needs path}"; shift 2 ;;
    --out) OUT_PATH="${2:?--out needs path}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "render-homebrew-formula.sh: unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$VERSION" && -n "$BASE_URL" && -f "$CHECKSUMS" ]] || { usage >&2; exit 2; }
BASE="${BASE_URL%/}"

sum_for() {
  local target="metatron-${VERSION}-${1}.tar.gz"
  awk -v t="$target" '$2 == t { print $1; exit }' "$CHECKSUMS"
}

SHA_DARWIN_ARM64="$(sum_for darwin-arm64)"
SHA_DARWIN_X64="$(sum_for darwin-x86_64)"
SHA_LINUX_ARM64="$(sum_for linux-arm64)"
SHA_LINUX_X64="$(sum_for linux-x86_64)"

render() {
  cat <<EOF
class Metatron < Formula
  desc "BOSS governed operator CLI (Rust)"
  homepage "https://github.com/seanventures/boss"
  version "${VERSION}"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "${BASE}/metatron-${VERSION}-darwin-arm64.tar.gz"
      sha256 "${SHA_DARWIN_ARM64}"
    end
    on_intel do
      url "${BASE}/metatron-${VERSION}-darwin-x86_64.tar.gz"
      sha256 "${SHA_DARWIN_X64}"
    end
  end

  on_linux do
    on_arm do
      url "${BASE}/metatron-${VERSION}-linux-arm64.tar.gz"
      sha256 "${SHA_LINUX_ARM64}"
    end
    on_intel do
      url "${BASE}/metatron-${VERSION}-linux-x86_64.tar.gz"
      sha256 "${SHA_LINUX_X64}"
    end
  end

  def install
    bin.install "metatron"
    prefix.install "LICENSE" if File.exist?("LICENSE")
    prefix.install "THIRD-PARTY-NOTICES.md" if File.exist?("THIRD-PARTY-NOTICES.md")
  end

  test do
    assert_match "metatron", shell_output("#{bin}/metatron --version")
  end
end
EOF
}

if [[ -n "$OUT_PATH" ]]; then
  mkdir -p "$(dirname "$OUT_PATH")"
  render > "$OUT_PATH"
else
  render
fi
