"""MT-11 release engineering & Python TUI retirement plumbing tests (`BOSSAPP-1382`).

Covers:
1. Packaging (`package-release.sh`) + checksum verification (`verify-release.sh`) passing on valid
   archives and failing closed on tampered archives.
2. Standalone zero-Python installer (`install-metatron.sh`) working with NO `python3` in PATH.
3. `boss metatron --rust` running cleanly with NO `python3` in PATH (and never leaking API keys).
4. `boss metatron --legacy` invoking legacy Python TUI with deprecation notice.
5. Upgrade path migrating a legacy Python install to the verified Rust binary.
6. Homebrew formula renderer (`render-homebrew-formula.sh`) generating valid multi-arch Ruby.
7. Parity checker (`metatron/scripts/parity-check.py`) confirming 100% slash-command parity.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOSS_SH = ROOT / "scripts" / "install" / "boss.sh"
MT_SCRIPTS = ROOT / "metatron" / "scripts"


def _os_arch() -> tuple[str, str]:
    sys_name = platform.system().lower()
    os_tok = "darwin" if sys_name == "darwin" else "linux"
    mach = platform.machine().lower()
    arch_tok = "arm64" if mach in {"arm64", "aarch64"} else "x86_64"
    return os_tok, arch_tok


def _make_fake_rust_binary(path: Path, version: str = "0.1.0-test") -> None:
    path.write_text(
        f"#!/bin/sh\n"
        f"if [ \"$1\" = \"--version\" ]; then echo 'metatron {version}'; exit 0; fi\n"
        f"printf 'RUST_METATRON_OK url=%s project=%s has_key=%s args=%s\\n' "
        f"\"$BOSS_API_URL\" \"$BOSS_PROJECT_ID\" \"$([ -n \"$BOSS_API_KEY\" ] && echo yes || echo no)\" \"$*\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _no_python_path(tmp_path: Path) -> str:
    """Create a minimal PATH directory with POSIX tools needed by boss.sh/install-metatron.sh
    and STRICTLY NO python/python3 binary."""
    bin_dir = tmp_path / "no_py_bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in [
        "bash", "sh", "awk", "sed", "grep", "cut", "head", "tail", "tr", "cat",
        "mkdir", "cp", "mv", "rm", "chmod", "dirname", "basename", "uname",
        "mktemp", "tar", "find", "shasum", "sha256sum", "cksum", "env",
    ]:
        found = shutil.which(tool)
        if found:
            target = bin_dir / tool
            if not target.exists():
                target.symlink_to(found)
    assert not (bin_dir / "python").exists()
    assert not (bin_dir / "python3").exists()
    return str(bin_dir)


def _make_fake_install(tmp_path: Path) -> Path:
    inst = tmp_path / "install"
    (inst / "scripts" / "install").mkdir(parents=True)
    (inst / "metatron" / "scripts").mkdir(parents=True)
    shutil.copy2(BOSS_SH, inst / "scripts" / "install" / "boss.sh")
    for name in ["install-metatron.sh", "verify-release.sh"]:
        src = MT_SCRIPTS / name
        dst = inst / "metatron" / "scripts" / name
        shutil.copy2(src, dst)
        dst.chmod(0o755)
    # Minimal .env + compose stub so boss.sh `need_install` succeeds without Docker
    (inst / "scripts" / "install" / "community-compose.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (inst / ".env").write_text(
        "BOSS_API_PORT=8123\n"
        "BOSS_DEFAULT_PROJECT_ID=boss-dogfood\n"
        "BOSS_HOST_API_KEY=boss_pat_fake_host_key_for_test\n",
        encoding="utf-8",
    )
    # Minimal fake legacy scripts/metatron_tui.py
    (inst / "scripts" / "metatron_tui.py").write_text(
        "import os, sys\n"
        "print(f'LEGACY_PYTHON_TUI_OK project={os.environ.get(\"BOSS_PROJECT_ID\")} args={sys.argv[1:]}')\n",
        encoding="utf-8",
    )
    return inst


def test_package_and_verify_release_checksum_passes_and_fails_on_tamper(tmp_path: Path) -> None:
    os_tok, arch_tok = _os_arch()
    fake_bin = tmp_path / "metatron"
    _make_fake_rust_binary(fake_bin)
    out_dir = tmp_path / "dist"

    pkg = subprocess.run(
        [
            "bash",
            str(MT_SCRIPTS / "package-release.sh"),
            "--bin", str(fake_bin),
            "--version", "0.1.0",
            "--os", os_tok,
            "--arch", arch_tok,
            "--out-dir", str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pkg.returncode == 0, pkg.stderr
    archive = Path(pkg.stdout.strip())
    assert archive.is_file()
    sums = out_dir / "SHA256SUMS"
    assert sums.is_file()

    # Valid archive passes verification
    ok = subprocess.run(
        [
            "bash",
            str(MT_SCRIPTS / "verify-release.sh"),
            "--archive", str(archive),
            "--checksums", str(sums),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.startswith("OK ")

    # Tampered archive MUST fail closed!
    archive.write_bytes(archive.read_bytes() + b"TAMPERED")
    bad = subprocess.run(
        [
            "bash",
            str(MT_SCRIPTS / "verify-release.sh"),
            "--archive", str(archive),
            "--checksums", str(sums),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode != 0
    assert "SHA-256 mismatch" in bad.stderr


def test_install_and_boss_metatron_rust_run_with_zero_python_and_legacy_fallback(
    tmp_path: Path,
) -> None:
    os_tok, arch_tok = _os_arch()
    inst = _make_fake_install(tmp_path)
    no_py_path = _no_python_path(tmp_path)

    # 1. Package a release archive for this OS/arch
    src_bin = tmp_path / "src_metatron"
    _make_fake_rust_binary(src_bin, version="0.1.0-mt11")
    rel_dir = tmp_path / "release_dir"
    subprocess.run(
        [
            "bash",
            str(MT_SCRIPTS / "package-release.sh"),
            "--bin", str(src_bin),
            "--version", "0.1.0",
            "--os", os_tok,
            "--arch", arch_tok,
            "--out-dir", str(rel_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # 2. Install via `boss metatron --install-binary --from-dir ...` with NO python3 in PATH!
    env_no_py = {"PATH": no_py_path, "HOME": str(tmp_path)}
    install_res = subprocess.run(
        [
            "bash",
            str(inst / "scripts" / "install" / "boss.sh"),
            "--dir", str(inst),
            "metatron",
            "--install-binary",
            "--from-dir", str(rel_dir),
        ],
        env=env_no_py,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install_res.returncode == 0, install_res.stderr
    installed_bin = inst / "bin" / "metatron"
    assert installed_bin.is_file()
    assert bool(installed_bin.stat().st_mode & stat.S_IXUSR)

    # 3. `boss metatron` (default Step 4 when binary installed) AND `boss metatron --rust`
    # both run cleanly on a machine with ZERO Python!
    for extra in [[], ["--rust"]]:
        run_res = subprocess.run(
            [
                "bash",
                str(inst / "scripts" / "install" / "boss.sh"),
                "--dir", str(inst),
                "metatron",
                *extra,
                "--plain",
            ],
            env=env_no_py,
            capture_output=True,
            text=True,
            check=False,
        )
        assert run_res.returncode == 0, run_res.stderr
        assert "RUST_METATRON_OK" in run_res.stdout
        assert "has_key=yes" in run_res.stdout
        assert "boss_pat_fake_host_key_for_test" not in run_res.stdout
        assert "boss_pat_fake_host_key_for_test" not in run_res.stderr

    # 4. `boss metatron --legacy` still invokes the Python TUI with a deprecation notice.
    env_with_py = dict(os.environ)
    env_with_py["BOSS_METATRON_NO_INSTALL"] = "1"
    legacy_res = subprocess.run(
        [
            "bash",
            str(inst / "scripts" / "install" / "boss.sh"),
            "--dir", str(inst),
            "metatron",
            "--legacy",
            "--plain",
        ],
        env=env_with_py,
        capture_output=True,
        text=True,
        check=False,
    )
    assert legacy_res.returncode == 0, legacy_res.stderr
    assert "LEGACY_PYTHON_TUI_OK" in legacy_res.stdout
    assert "legacy Python Metatron TUI" in legacy_res.stderr


def test_render_homebrew_formula_generates_multi_arch_ruby(tmp_path: Path) -> None:
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(
        "1111111111111111111111111111111111111111111111111111111111111111  metatron-0.2.0-darwin-arm64.tar.gz\n"
        "2222222222222222222222222222222222222222222222222222222222222222  metatron-0.2.0-darwin-x86_64.tar.gz\n"
        "3333333333333333333333333333333333333333333333333333333333333333  metatron-0.2.0-linux-arm64.tar.gz\n"
        "4444444444444444444444444444444444444444444444444444444444444444  metatron-0.2.0-linux-x86_64.tar.gz\n",
        encoding="utf-8",
    )
    out = tmp_path / "Formula" / "metatron.rb"
    res = subprocess.run(
        [
            "bash",
            str(MT_SCRIPTS / "render-homebrew-formula.sh"),
            "--version", "0.2.0",
            "--base-url", "https://github.com/seanventures/boss/releases/download/metatron-v0.2.0",
            "--checksums", str(sums),
            "--out", str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    rb = out.read_text(encoding="utf-8")
    assert "class Metatron < Formula" in rb
    assert 'version "0.2.0"' in rb
    assert "1111111111111111111111111111111111111111111111111111111111111111" in rb
    assert "4444444444444444444444444444444444444444444444444444444444444444" in rb


def test_parity_check_script_reports_all_python_commands_present_in_rust() -> None:
    res = subprocess.run(
        [sys.executable, str(MT_SCRIPTS / "parity-check.py"), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    report = json.loads(res.stdout)
    assert report["parity_green"] is True
    assert report["missing_in_rust"] == []
    assert report["python_command_count"] == 31
    assert report["rust_command_count"] >= 41


def test_boss_upgrade_migrates_legacy_python_install_to_verified_rust_binary(
    tmp_path: Path,
) -> None:
    """MT-11 upgrade-path test: an install starting with ONLY legacy Python Metatron ends up
    running the verified Rust binary after `boss upgrade`."""
    os_tok, arch_tok = _os_arch()
    inst = _make_fake_install(tmp_path)
    # Stub upgrade.sh to succeed without touching git/docker
    upgrade_sh = inst / "scripts" / "install" / "upgrade.sh"
    upgrade_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    upgrade_sh.chmod(0o755)

    # Before upgrade: no Rust binary installed -> `boss metatron` falls back to legacy Python TUI.
    env_py = dict(os.environ)
    env_py["BOSS_METATRON_NO_INSTALL"] = "1"
    before = subprocess.run(
        [
            "bash",
            str(inst / "scripts" / "install" / "boss.sh"),
            "--dir", str(inst),
            "metatron",
            "--plain",
        ],
        env=env_py,
        capture_output=True,
        text=True,
        check=False,
    )
    assert before.returncode == 0, before.stderr
    assert "LEGACY_PYTHON_TUI_OK" in before.stdout

    # Build a release bundle and run `boss upgrade` with BOSS_METATRON_RELEASE_DIR set.
    src_bin = tmp_path / "upgraded_metatron"
    _make_fake_rust_binary(src_bin, version="0.2.0-upgraded")
    rel_dir = tmp_path / "upgrade_release"
    subprocess.run(
        [
            "bash",
            str(MT_SCRIPTS / "package-release.sh"),
            "--bin", str(src_bin),
            "--version", "0.2.0",
            "--os", os_tok,
            "--arch", arch_tok,
            "--out-dir", str(rel_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    env_upg = dict(os.environ)
    env_upg["BOSS_METATRON_RELEASE_DIR"] = str(rel_dir)
    upg = subprocess.run(
        [
            "bash",
            str(inst / "scripts" / "install" / "boss.sh"),
            "--dir", str(inst),
            "upgrade",
        ],
        env=env_upg,
        capture_output=True,
        text=True,
        check=False,
    )
    assert upg.returncode == 0, upg.stderr
    assert (inst / "bin" / "metatron").is_file()

    # After upgrade: `boss metatron` launches the new verified Rust binary with ZERO Python!
    no_py_path = _no_python_path(tmp_path)
    after = subprocess.run(
        [
            "bash",
            str(inst / "scripts" / "install" / "boss.sh"),
            "--dir", str(inst),
            "metatron",
            "--plain",
        ],
        env={"PATH": no_py_path, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert after.returncode == 0, after.stderr
    assert "RUST_METATRON_OK" in after.stdout
