#!/usr/bin/env bash
set -euo pipefail

DEFAULT_PIN="05ae1b0fdd5ab412d24248acc76c5d9f6f275440"
DEFAULT_CRATES="utils/head-tail-buffer,utils/pty,keyring-store"
EXPECTED_LICENSE_SHA256="d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc"
EXPECTED_NOTICE_SHA256="9d71575ecfd9a843fc1677b0efb08053c6ba9fd686a0de1a6f5382fd3c220915"

PIN="$DEFAULT_PIN"
CRATES="$DEFAULT_CRATES"
VERIFY_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --pin)
      PIN="${2:?--pin requires a commit SHA}"
      shift 2
      ;;
    --crates)
      CRATES="${2:?--crates requires a comma-separated list}"
      shift 2
      ;;
    --verify)
      VERIFY_ONLY=1
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: vendor-codex.sh [--pin <sha>] [--crates <csv>] [--verify]

Without --verify, downloads openai/codex at <sha>, verifies LICENSE and NOTICE
SHA-256 digests, extracts the listed true-leaf crates into
metatron/third_party/codex/, applies minimal BOSS build/test adaptations marked
"changed by BOSS", and injects SPDX/copyright/pin headers on all .rs files.

With --verify, checks retained LICENSE/NOTICE digests and provenance headers
offline without touching the network.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
METATRON_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENDOR_DIR="$METATRON_DIR/third_party/codex"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

verify_vendored() {
  local lic="$VENDOR_DIR/LICENSE"
  local not="$VENDOR_DIR/NOTICE"
  [ -f "$lic" ] || { echo "Missing $lic" >&2; exit 1; }
  [ -f "$not" ] || { echo "Missing $not" >&2; exit 1; }

  if [ "$PIN" = "$DEFAULT_PIN" ]; then
    local got_lic got_not
    got_lic="$(sha256_of "$lic")"
    got_not="$(sha256_of "$not")"
    if [ "$got_lic" != "$EXPECTED_LICENSE_SHA256" ]; then
      echo "LICENSE sha256 mismatch: $got_lic != $EXPECTED_LICENSE_SHA256" >&2
      exit 1
    fi
    if [ "$got_not" != "$EXPECTED_NOTICE_SHA256" ]; then
      echo "NOTICE sha256 mismatch: $got_not != $EXPECTED_NOTICE_SHA256" >&2
      exit 1
    fi
  fi

  python3 - "$VENDOR_DIR" "$PIN" "$CRATES" <<'PY'
import sys
from pathlib import Path

vendor_dir = Path(sys.argv[1])
pin = sys.argv[2]
crates = [c.strip() for c in sys.argv[3].split(",") if c.strip()]

rs_files = sorted(vendor_dir.rglob("*.rs"))
if not rs_files:
    raise SystemExit(f"No .rs files found under {vendor_dir}")

for path in rs_files:
    text = path.read_text(encoding="utf-8")
    for needle in (
        "Copyright 2025 OpenAI",
        "SPDX-License-Identifier: Apache-2.0",
        pin,
    ):
        if needle not in text:
            raise SystemExit(f"Missing {needle!r} in {path}")

if "utils/pty" in crates:
    pty_cargo = vendor_dir / "utils/pty/Cargo.toml"
    if "changed by BOSS" not in pty_cargo.read_text(encoding="utf-8"):
        raise SystemExit(f"Missing 'changed by BOSS' note in {pty_cargo}")
    for rel in ("utils/pty/src/lib.rs", "utils/pty/src/unix_io.rs"):
        p = vendor_dir / rel
        if "changed by BOSS" not in p.read_text(encoding="utf-8"):
            raise SystemExit(f"Missing 'changed by BOSS' comment in {p}")

print(f"Verified {len(rs_files)} vendored .rs files and notices at pin {pin}")
PY
}

if [ "$VERIFY_ONLY" -eq 1 ]; then
  verify_vendored
  exit 0
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

TARBALL="$TMP_DIR/codex.tar.gz"
ARCHIVE_URL="https://github.com/openai/codex/archive/${PIN}.tar.gz"
echo "Fetching $ARCHIVE_URL ..."
curl -fsSL "$ARCHIVE_URL" -o "$TARBALL"

EXTRACT_DIR="$TMP_DIR/src"
mkdir -p "$EXTRACT_DIR"
tar -xzf "$TARBALL" -C "$EXTRACT_DIR"

TOP_DIR="$EXTRACT_DIR/codex-${PIN}"
if [ ! -d "$TOP_DIR" ]; then
  TOP_DIR="$(find "$EXTRACT_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
fi
[ -d "$TOP_DIR" ] || { echo "Extracted archive directory missing" >&2; exit 1; }

if [ "$PIN" = "$DEFAULT_PIN" ]; then
  got_lic="$(sha256_of "$TOP_DIR/LICENSE")"
  got_not="$(sha256_of "$TOP_DIR/NOTICE")"
  [ "$got_lic" = "$EXPECTED_LICENSE_SHA256" ] || { echo "Upstream LICENSE sha256 mismatch: $got_lic" >&2; exit 1; }
  [ "$got_not" = "$EXPECTED_NOTICE_SHA256" ] || { echo "Upstream NOTICE sha256 mismatch: $got_not" >&2; exit 1; }
fi

rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR"
cp -p "$TOP_DIR/LICENSE" "$VENDOR_DIR/LICENSE"
cp -p "$TOP_DIR/NOTICE" "$VENDOR_DIR/NOTICE"

IFS=',' read -ra CRATE_LIST <<< "$CRATES"
for crate in "${CRATE_LIST[@]}"; do
  crate="$(echo "$crate" | xargs)"
  [ -n "$crate" ] || continue
  src_crate="$TOP_DIR/codex-rs/$crate"
  [ -d "$src_crate" ] || { echo "Crate not found in upstream pin: $src_crate" >&2; exit 1; }
  dst_crate="$VENDOR_DIR/$crate"
  mkdir -p "$(dirname "$dst_crate")"
  cp -R "$src_crate" "$dst_crate"
  find "$dst_crate" -name 'BUILD.bazel' -delete
done

python3 - "$VENDOR_DIR" "$PIN" <<'PY'
import re
import sys
from pathlib import Path

vendor_dir = Path(sys.argv[1])
pin = sys.argv[2]

pty_dir = vendor_dir / "utils/pty"
modified_notes: dict[Path, str] = {}

if pty_dir.is_dir():
    cargo_toml = pty_dir / "Cargo.toml"
    text = cargo_toml.read_text(encoding="utf-8")
    new_lines = []
    removed = False
    for line in text.splitlines():
        if "codex-utils-cargo-bin" in line:
            removed = True
            new_lines.append("# changed by BOSS: dropped codex-utils-cargo-bin dev-dep (avoids git runfiles dep)")
            continue
        new_lines.append(line)
    if removed:
        cargo_toml.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    lib_rs = pty_dir / "src/lib.rs"
    lib_text = lib_rs.read_text(encoding="utf-8")
    helper = (
        "\n// changed by BOSS: local test_support::write_executable replaces codex-utils-cargo-bin\n"
        "#[cfg(test)]\n"
        "pub(crate) mod test_support {\n"
        "    use std::path::Path;\n\n"
        "    pub(crate) fn write_executable(\n"
        "        path: &Path,\n"
        "        script: &str,\n"
        "    ) -> std::io::Result<()> {\n"
        "        if let Some(parent) = path.parent() {\n"
        "            std::fs::create_dir_all(parent)?;\n"
        "        }\n"
        "        std::fs::write(path, script)?;\n"
        "        #[cfg(unix)]\n"
        "        {\n"
        "            use std::os::unix::fs::PermissionsExt;\n"
        "            let mut perms = std::fs::metadata(path)?.permissions();\n"
        "            perms.set_mode(0o755);\n"
        "            std::fs::set_permissions(path, perms)?;\n"
        "        }\n"
        "        Ok(())\n"
        "    }\n"
        "}\n"
    )
    if "pub(crate) mod test_support" not in lib_text:
        lib_rs.write_text(lib_text.rstrip() + "\n" + helper, encoding="utf-8")
        modified_notes[lib_rs] = "added test_support::write_executable to drop codex-utils-cargo-bin dev-dep"

    for test_rs in pty_dir.rglob("*.rs"):
        if test_rs == lib_rs:
            continue
        content = test_rs.read_text(encoding="utf-8")
        updated = content
        if "codex_utils_cargo_bin::write_executable" in updated:
            updated = updated.replace(
                "codex_utils_cargo_bin::write_executable",
                "crate::test_support::write_executable",
            )
            modified_notes[test_rs] = "replaced codex_utils_cargo_bin::write_executable with crate::test_support::write_executable"
        if "codex_utils_cargo_bin" in updated:
            updated = re.sub(
                r"use\s+codex_utils_cargo_bin::copy_executable;\s*\n?",
                "",
                updated,
            )
            updated = updated.replace(
                "codex_utils_cargo_bin::copy_executable(",
                "std::fs::copy(",
            )
            updated = updated.replace(
                "copy_executable(",
                "std::fs::copy(",
            )
            modified_notes[test_rs] = "replaced codex_utils_cargo_bin helper with std::fs::copy"
        if test_rs.name == "unix_io.rs" and "AsyncFd::new(" in updated:
            updated = updated.replace(
                "AsyncFd::new(",
                "{\n            #[allow(deprecated)] // changed by BOSS: tokio >= 1.54 deprecates AsyncFd::new\n            AsyncFd::new(",
            )
            # Close the added block right after AsyncFd::new(File::from(fd))
            updated = updated.replace(
                "AsyncFd::new(File::from(fd))",
                "AsyncFd::new(File::from(fd))\n        }",
            )
            modified_notes[test_rs] = "allowed tokio AsyncFd::new deprecation for clippy -D warnings"
        if updated != content:
            test_rs.write_text(updated, encoding="utf-8")

for rs_path in sorted(vendor_dir.rglob("*.rs")):
    rel = rs_path.relative_to(vendor_dir).as_posix()
    text = rs_path.read_text(encoding="utf-8")
    header_lines = [
        "// Copyright 2025 OpenAI",
        "// SPDX-License-Identifier: Apache-2.0",
        f"// Vendored from openai/codex@{pin} (codex-rs/{rel})",
    ]
    if rs_path in modified_notes:
        header_lines.append(f"// changed by BOSS: {modified_notes[rs_path]}")
    header = "\n".join(header_lines) + "\n\n"
    if not text.startswith("// Copyright 2025 OpenAI"):
        rs_path.write_text(header + text, encoding="utf-8")
PY

if command -v cargo >/dev/null 2>&1 && [ -f "$METATRON_DIR/Cargo.toml" ]; then
  cargo fmt --manifest-path "$METATRON_DIR/Cargo.toml" --all
fi

verify_vendored
