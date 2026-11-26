"""A small Docker disk keeps room to work after an install (BOSSAPP-1447).

The enterprise lane's fresh install on a 30 GB box left about 5 GB free: the image build's cache
stayed behind, uncapped on a first install, and the e2e gate (and any build) needs more. The
class fix: prune.sh, which every upgrade already runs, also trims the build cache down to what
fits above BOSS_DISK_FLOOR_GB on a small Docker disk, and the installer runs it after the build.
Before the build the installer says the disk a first install needs, as a number.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PRUNE = REPO / "scripts" / "install" / "prune.sh"

# Docker Desktop / Colima: the root dir is inside a VM, so the free space is read from inside
# the API image (`docker run … df`), which this fake answers with FAKE_FREE_KB.
FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info)
    [[ "$*" == *DockerRootDir* ]] && printf '/var/lib/docker|%s\n' "${FAKE_OS:-Docker Desktop}"
    exit 0 ;;
  run)
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted\n'
    printf 'overlay 31457280 1 %s 90%% /\n' "$FAKE_FREE_KB"
    exit 0 ;;
  system) printf 'Images|12.1GB\nBuild Cache|%s\n' "${FAKE_CACHE:-4.2GB}"; exit 0 ;;
  *) exit 0 ;;
esac
"""


def _world(tmp_path: Path, free_gb: int, cache: str = "4.2GB", room: bool = True, dry: bool = False, **extra: str) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "docker"
    exe.write_text(FAKE_DOCKER)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    install = tmp_path / "install"
    (install / "scripts" / "install").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOSS_")}
    env.update({"PATH": f"{bindir}{os.pathsep}{env['PATH']}", "FAKE_DOCKER_LOG": str(tmp_path / "log"),
                "FAKE_FREE_KB": str(free_gb * 1048576), "FAKE_CACHE": cache, **extra})
    args = ["--room"] if room else []
    out = subprocess.run(["bash", str(PRUNE), *(["--dry-run"] if dry else []), "--dir", str(install), *args], env=env,
                         capture_output=True, text=True, timeout=60, check=True)
    # Every docker call the script made, against the fake (a dry run never starts the container
    # that measures a VM's disk, so these tests run for real against the fake docker).
    log = tmp_path / "log"
    calls = ["docker " + ln for ln in (log.read_text().splitlines() if log.exists() else [])]
    log.unlink(missing_ok=True)
    return {"out": out.stdout, "calls": calls}


def test_a_30gb_box_after_its_first_build_drops_the_whole_build_cache(tmp_path):
    w = _world(tmp_path, free_gb=5, cache="4.2GB")
    assert "docker builder prune -af" in w["calls"], w["out"]
    assert "under the 20 GB floor (BOSS_DISK_FLOOR_GB): keeping 0 GB of build cache" in w["out"]


def test_a_tight_disk_keeps_only_the_cache_that_fits_above_the_floor(tmp_path):
    w = _world(tmp_path, free_gb=15, cache="10.4GB")
    assert "docker builder prune -af --keep-storage 5gb" in w["calls"], w["out"]


def test_the_floor_is_a_setting(tmp_path):
    w = _world(tmp_path, free_gb=5, cache="4GB", BOSS_DISK_FLOOR_GB="8")
    assert "docker builder prune -af --keep-storage 1gb" in w["calls"], w["out"]


def test_a_roomy_disk_keeps_its_capped_cache(tmp_path):
    w = _world(tmp_path, free_gb=60)
    assert not any("prune -af" in c for c in w["calls"]), w["out"]
    assert "docker builder prune -f --keep-storage 10gb" in w["calls"]     # the cap still holds


def test_an_unmeasurable_disk_says_so_and_keeps_the_cap(tmp_path):
    w = _world(tmp_path, free_gb=5, FAKE_FREE_KB="")
    # FAKE_FREE_KB="" overrides the measured value: df prints no number.
    assert not any("prune -af" in c for c in w["calls"]), w["out"]
    assert "could not be measured" in w["out"]


def test_the_installer_trims_after_the_build_and_states_the_disk_a_first_install_needs():
    install = (REPO / "install.sh").read_text()
    build = install.index('ui_step "Building and starting Community stack')
    need = install.index("a first install needs about ${BOSS_INSTALL_NEEDS_GB} GB")
    trim = install.index('bash "$INSTALL_DIR/scripts/install/prune.sh" --dir "$INSTALL_DIR" --room')
    assert need < build < trim
    # The lane checks what the install left; it no longer prunes for it.
    lane = (REPO / "scripts" / "enterprise-lane.sh").read_text()
    body = lane[lane.index("step_install() {"):lane.index("step_preflight() {")]
    after = body[body.index("install.sh --dir"):]
    assert "builder prune" not in after and '[ "$after" -ge 6 ]' in after


def test_a_shared_machine_keeps_other_projects_cache_unless_asked(tmp_path):
    """The cache is Docker-wide: a plain `boss prune` on a shared machine never drops it below the
    cap; only the installer (--room) or a dedicated host does."""
    w = _world(tmp_path, free_gb=5, room=False)
    assert not any("prune -af" in c for c in w["calls"]), w["out"]
    d = _world(tmp_path, free_gb=5, room=False, BOSS_DEDICATED_HOST="1")
    assert "docker builder prune -af" in d["calls"]


def test_colima_releases_cache_when_mac_backing_disk_is_tight(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"uname": "echo Darwin",
                       "df": "printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/data 50000000 40000000 10000000 80%% /data\\n'"}.items():
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n" + body + "\n")
        exe.chmod(0o755)
    local = _world(tmp_path, free_gb=30, DOCKER_CONTEXT="colima-qa", DOCKER_HOST="")
    assert "docker builder prune -af" in local["calls"]
    assert "The Mac backing this install" in local["out"]
    remote = _world(tmp_path, free_gb=30, DOCKER_CONTEXT="qa-remote", DOCKER_HOST="")
    assert "docker builder prune -af" not in remote["calls"]
