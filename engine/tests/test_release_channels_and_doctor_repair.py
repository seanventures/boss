"""BOSSAPP-895: release channels for `boss upgrade`, and `boss doctor --repair`.

CHANNELS: the REAL scripts/install/upgrade.sh (--dry-run) against a real origin with release
tags: BOSS_CHANNEL=rc follows release candidates, `stable` final releases only, and a candidate
install switched to stable never steps back to an older release. The real install.sh keeps the
choice in .boss-release on a rerun.

REPAIR: the REAL scripts/install/checkup.sh with a fake `docker` and `curl` on PATH. --repair
without --apply changes nothing; --apply starts the stopped services from images already on the
host (never a build, a pull, a tag or a removal — 839's prev-<tag> images are untouched),
restarts a silent API, and checks again; unsafe remedies are only ever printed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ._self_fix_support import git
from .test_community_installer_upgrade_backup import _install, install_pair  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
UPGRADE = ROOT / "scripts/install/upgrade.sh"
CHECKUP = ROOT / "scripts/install/checkup.sh"


def _commit(repo: Path, name: str, tag: str) -> None:
    (repo / "version").write_text(name + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    git(repo, "tag", tag)


@pytest.fixture()
def releases(tmp_path):
    """An origin with v1.0.0 and v1.1.0-rc1, and a function making an install at a tag."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "scripts/install").mkdir(parents=True)
    (origin / "scripts/install/backup.sh").write_text("#!/usr/bin/env bash\n")
    (origin / "scripts/install/community-compose.yml").write_text("services: {}\n")
    (origin / ".gitignore").write_text(".env\n.boss-release\n")
    _commit(origin, "one", "v1.0.0")
    _commit(origin, "two", "v1.1.0-rc1")

    def install(at: str, channel: str = "") -> Path:
        path = tmp_path / f"install-{at}-{channel or 'none'}"
        git(tmp_path, "clone", "-q", str(origin), str(path))
        git(path, "checkout", "-q", "--detach", at)
        (path / ".env").write_text("BOSS_DB_PASSWORD=x\n")
        (path / ".boss-release").write_text(
            f"BOSS_RELEASE_REF={at}\n" + (f"BOSS_CHANNEL={channel}\n" if channel else ""))
        return path

    return origin, install


def _upgrade(install: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(UPGRADE), "--dir", str(install), "--dry-run", *args],
                          capture_output=True, text=True, env={**os.environ, "HOME": str(install)})


def test_without_a_channel_a_stable_install_stays_on_stable_releases(releases):
    _origin, install = releases
    out = _upgrade(install("v1.0.0"))
    assert out.returncode == 0, out.stderr
    assert "Already on the newest release, v1.0.0" in out.stdout


def test_the_rc_channel_follows_release_candidates(releases):
    _origin, install = releases
    recorded = _upgrade(install("v1.0.0", "rc"))
    assert "Newest release: v1.1.0-rc1 (installed: v1.0.0)" in recorded.stdout, recorded.stderr
    asked = install("v1.0.0")
    flagged = _upgrade(asked, "--channel", "rc")
    assert "Would follow the rc channel" in flagged.stdout
    assert "Newest release: v1.1.0-rc1" in flagged.stdout
    assert "BOSS_CHANNEL" not in (asked / ".boss-release").read_text()   # a dry run records nothing


def test_stable_never_steps_a_candidate_back_and_moves_on_the_next_final(releases):
    origin, install = releases
    at_rc = install("v1.1.0-rc1", "stable")
    held = _upgrade(at_rc)
    assert held.returncode == 0, held.stderr
    assert "No stable release is newer than v1.1.0-rc1 yet; staying on it." in held.stdout
    _commit(origin, "three", "v1.1.0")
    moved = _upgrade(at_rc)
    assert "Newest release: v1.1.0 (installed: v1.1.0-rc1)" in moved.stdout, moved.stderr


def test_a_bad_channel_is_refused(releases):
    _origin, install = releases
    out = _upgrade(install("v1.0.0"), "--channel", "nightly")
    assert out.returncode == 2 and "--channel takes stable or rc" in out.stderr


def test_the_installer_keeps_the_channel_on_a_rerun(install_pair):  # noqa: F811
    installed, _old, _new, env, _tmp = install_pair
    _install(installed, env)
    assert "BOSS_CHANNEL" not in (installed / ".boss-release").read_text()   # unset: no line
    _install(installed, {**env, "BOSS_CHANNEL": "rc"})
    assert "BOSS_CHANNEL=rc" in (installed / ".boss-release").read_text()
    _install(installed, env)                                                  # a plain rerun
    assert "BOSS_CHANNEL=rc" in (installed / ".boss-release").read_text()


# ── doctor --repair ──────────────────────────────────────────────────────────────────────────

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  info) exit 0 ;;
  ps)
    name="$(printf '%s' "$*" | sed -n 's/.*name=\([^ ]*\).*/\1/p')"
    for svc in db api worker; do
      if [[ -f "$FAKE_STATE/$svc" && "$name" == *"-$svc-" ]]; then echo "${name}1"; fi
    done
    if [[ "$name" == *- && "$name" != *-*-*- ]]; then
      for svc in db api worker; do [[ -f "$FAKE_STATE/$svc" ]] && echo "x-$svc-1"; done
    fi
    exit 0 ;;
  exec) exit 1 ;;
  compose)
    if [[ "$*" == *" up -d"* ]]; then touch "$FAKE_STATE/db" "$FAKE_STATE/api" "$FAKE_STATE/worker" "$FAKE_STATE/health"; fi
    if [[ "$*" == *" restart api"* ]]; then touch "$FAKE_STATE/health"; fi
    exit 0 ;;
  *) exit 0 ;;
esac
"""
FAKE_CURL = r"""#!/usr/bin/env bash
for a in "$@"; do
  if [[ "$a" == */health ]]; then [[ -f "$FAKE_STATE/health" ]] && exit 0 || exit 7; fi
done
exit 7
"""


@pytest.fixture()
def host(tmp_path):
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("BOSS_COMMUNITY_WEB_ENABLED=0\n")
    log = tmp_path / "docker.log"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(tmp_path), "FAKE_STATE": str(state), "FAKE_LOG": str(log),
           "BOSS_ENV_FILE": str(env_file), "BOSS_REPAIR_WAIT": "2"}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(CHECKUP), *args], capture_output=True, text=True,
                              env=env)

    def calls() -> str:
        return log.read_text() if log.exists() else ""

    return run, state, calls


def test_without_repair_the_checkup_prints_no_repairs(host):
    run, _state, calls = host
    out = run()
    assert out.returncode == 1 and "Repairs" not in out.stdout
    assert " up " not in calls() and "restart" not in calls()


def test_repair_is_a_dry_run_by_default(host):
    run, _state, calls = host
    out = run("--repair")
    assert out.returncode == 1
    assert "would start the stopped services" in out.stdout
    assert "Nothing was changed" in out.stdout
    assert "compose" not in calls()


def test_apply_starts_the_services_without_building_and_checks_again(host):
    run, _state, calls = host
    out = run("--repair", "--apply")
    assert out.returncode == 0, out.stdout
    assert "Checking again" in out.stdout and "BOSS is ready" in out.stdout
    compose = [line for line in calls().splitlines() if line.startswith("compose")]
    assert len(compose) == 1 and "up -d --no-build --pull never" in compose[0]
    for forbidden in ("build ", "pull ", "rmi", "image rm", "tag ", "prune"):
        assert not any(line.startswith(forbidden) or f" {forbidden}" in line.replace(
            "--no-build", "").replace("--pull never", "") for line in calls().splitlines()), forbidden


def test_a_silent_api_is_restarted(host):
    run, state, calls = host
    for svc in ("db", "api", "worker"):
        (state / svc).touch()
    out = run("--repair", "--apply")
    assert out.returncode == 0, out.stdout
    assert any("restart api" in line for line in calls().splitlines())


def test_unsafe_remedies_are_only_printed(host, tmp_path):
    run, state, calls = host
    for svc in ("db", "api", "worker", "health"):
        (state / svc).touch()
    (tmp_path / ".env").write_text('BOSS_COMMUNITY_WEB_ENABLED=0\nBROKEN="never closed\n')
    out = run("--repair", "--apply")
    assert out.returncode == 1
    assert "you   .env line 2: close the quote" in out.stdout
    assert "compose" not in calls()
