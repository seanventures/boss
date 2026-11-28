# Metatron Release Engineering & Python TUI Retirement (`MT-11`)

Status: Release packaging, checksum/signature verification, Homebrew formula generation, `boss metatron --rust` / `--legacy` plumbing, and parity harness implemented (`BOSSAPP-1382`).

## 1. Two-Release Retirement Plan (`ARCHITECTURE.md` §10)

| Step | State | `boss metatron` behaviour |
|---|---|---|
| Step 2 (side-by-side) | Rust binary optional | `boss metatron --rust` launches Rust binary; default falls back to Python TUI if binary is not installed |
| Step 4 (Rust default) | Rust binary installed by installer / `boss upgrade` | `boss metatron` launches Rust binary by default (zero Python needed); `--legacy` keeps Python TUI for one release with deprecation notice |
| Step 5 (retirement) | After one full release on Step 4 | Delete `scripts/metatron_tui.py`, `scripts/metatron_render.py`, `scripts/metatron_shell.py`, `engine/boss_engine/cli/metatron.py`, and `metatron_python()` venv bootstrap in `scripts/install/boss.sh` |

## 2. Supported Release Targets & Packaging

Targets (matching `metatron/ci/metatron.yml` and `metatron/deny.toml`):
- `darwin-arm64` (`aarch64-apple-darwin`)
- `darwin-x86_64` (`x86_64-apple-darwin`)
- `linux-x86_64` (`x86_64-unknown-linux-musl`)
- `linux-aarch64` (`aarch64-unknown-linux-musl`)

Packaging and verification scripts under `metatron/scripts/`:
- `package-release.sh`: creates `metatron-<version>-<os>-<arch>.tar.gz` containing `metatron`, `LICENSE`, `THIRD-PARTY-NOTICES.md`, and `THIRD-PARTY`, appends SHA-256 to `SHA256SUMS`, and signs `SHA256SUMS` with `minisign` when `METATRON_MINISIGN_KEY` is configured.
- `verify-release.sh`: verifies SHA-256 checksum (and optional `minisign` signature), failing closed on mismatch or missing entry.
- `install-metatron.sh`: zero-Python installer for clean macOS and Linux hosts (`--from-dir` or `--base-url`), verifying checksums before atomic installation.
- `render-homebrew-formula.sh`: generates `Formula/metatron.rb` from version + `SHA256SUMS`.
- `parity-check.py`: verifies 100% slash-command parity (all 31 Python TUI commands present in Rust `metatron-tui`) and checks `metatron schema` v1.0 compliance.

## 3. Exit Criteria Checklist Before Step 5 Deletion

- [x] Slash-command parity table green (`python3 metatron/scripts/parity-check.py` -> 31/31 Python commands present in Rust + 10 Rust additions).
- [x] Versioned JSONL golden-transcript tests pass (`wave_c_e2e.rs`, `wave_d_e2e.rs`).
- [x] First run works on a machine with **no Python** (`scripts/tests/test_metatron_release_plumbing.py::test_boss_metatron_rust_runs_without_python`).
- [x] `boss upgrade` / `--install-binary` migrates an existing legacy Python install to a checksum-verified Rust binary.
- [ ] Publish signed release binaries for all 4 targets via CI tag release and soak one full release cycle before Step 5 deletion.
