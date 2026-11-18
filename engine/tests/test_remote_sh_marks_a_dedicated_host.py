"""A host remote.sh installs is marked dedicated, so BOSS may prune its Docker build cache.

boss-1, 2026-11-18: 80% disk with 46 GB of build cache. The worker's disk reaper
(resource_reaper.prune_disk), the steward and `boss prune` prune the build cache only when
.env says BOSS_DEDICATED_HOST=1, and the reaper's docstring said the remote installer wrote it.
Nothing did.

remote.sh `install` and `update` now write BOSS_DEDICATED_HOST=1 into the host's .env when the
line is absent. An explicit value — BOSS_DEDICATED_HOST=0 on a shared machine — is never
changed. These run the real cmd_install / cmd_update against a fixture host directory, through
a fake ssh; only the steps around the mark (preflight, backup, source swap, prune) and Docker
are stubbed.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REMOTE_SRC = (ROOT / "scripts" / "install" / "remote.sh").read_text()

FAKE_SSH = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-i|-p|-l) shift 2 ;;
    -t|-T|-q|-N) shift ;;
    *) break ;;
  esac
done
shift  # the host
exec bash -c "$1"
"""

#: Records each build's --env-file contents, so the test sees what the images were built with.
FAKE_DOCKER = """#!/usr/bin/env bash
log="$FAKE_LOG"
env_file=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  [[ "${args[$i]}" == --env-file ]] && env_file="${args[$((i+1))]}"
done
printf 'docker %s\\n' "$*" >> "$log"
if [[ " $* " == *" build "* && -n "$env_file" ]]; then
  printf -- '--- build env\\n' >> "$log"; cat "$env_file" >> "$log"
fi
exit 0
"""

#: Stands in for install.sh on the host: records the mark it was started with, then writes .env
#: the way the real one does (a rerun keeps unknown lines; a first run writes a fresh file).
FAKE_INSTALL = """#!/usr/bin/env bash
printf 'install.sh saw BOSS_DEDICATED_HOST=%s\\n' "${BOSS_DEDICATED_HOST-<unset>}" >> "$FAKE_LOG"
if [ -f .env ]; then
  { printf 'BOSS_EDITION=community\\n'; grep -v '^BOSS_EDITION=' .env; } > .env.new && mv .env.new .env
else
  printf 'BOSS_EDITION=community\\nBOSS_API_KEY=x\\n' > .env
fi
"""

STUBS = """
preflight() { :; }
report_capacity() { :; }
backup_remote() { :; }
ship_source() { mkdir -p "$DIR"; printf 'deadbeef\\n' > "$DIR/.boss-shipped-commit"; }
clear_prev() { :; }
prune_remote() { :; }
claim_next() { :; }
wait_for_builds() { :; }
"""


def _run(tmp_path: Path, cmd: str, host_env: str | None) -> tuple[subprocess.CompletedProcess, Path, str]:
    host_dir = tmp_path / "host" / "boss"
    (host_dir / "scripts" / "install").mkdir(parents=True)
    (host_dir / "install.sh").write_text(FAKE_INSTALL)
    (host_dir / "scripts" / "install" / "reconcile_api_credentials.py").write_text("")
    if host_env is not None:
        (host_dir / ".env").write_text(host_env)
        (host_dir / ".boss-shipped-commit").write_text("cafef00d\n")   # an existing install

    repo = tmp_path / "repo" / "scripts" / "install"
    repo.mkdir(parents=True)
    dispatch = REMOTE_SRC.index('case "$CMD" in')
    script = REMOTE_SRC[:dispatch] + STUBS + f"cmd_{cmd}\n"
    (repo / "remote_test.sh").write_text(script)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", FAKE_SSH), ("docker", FAKE_DOCKER)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if k != "BOSS_DEDICATED_HOST"}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", FAKE_LOG=str(log))
    result = subprocess.run(
        ["bash", str(repo / "remote_test.sh"), cmd, "--host", "fake-host", "--dir", str(host_dir)],
        env=env, text=True, capture_output=True, timeout=60)
    return result, host_dir / ".env", log.read_text()


def _marks(env_file: Path) -> list[str]:
    return [l for l in env_file.read_text().splitlines() if l.startswith("BOSS_DEDICATED_HOST=")]


def test_a_first_install_is_marked_and_its_containers_see_it(tmp_path: Path) -> None:
    result, env_file, log = _run(tmp_path, "install", host_env=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _marks(env_file) == ["BOSS_DEDICATED_HOST=1"]
    # install.sh starts the stack: the mark is already in its environment for compose.
    assert "install.sh saw BOSS_DEDICATED_HOST=1" in log
    assert "dedicated BOSS host" in result.stdout


def test_a_reinstall_over_an_unmarked_env_is_marked_before_install_sh(tmp_path: Path) -> None:
    result, env_file, log = _run(tmp_path, "install", host_env="BOSS_API_KEY=x")  # no newline
    assert result.returncode == 0, result.stdout + result.stderr
    assert _marks(env_file) == ["BOSS_DEDICATED_HOST=1"]
    assert "BOSS_API_KEY=x" in env_file.read_text().splitlines()   # appended, not glued on
    # The line was in .env before install.sh ran, so its rewrite carried it.
    assert "install.sh saw BOSS_DEDICATED_HOST=<unset>" in log


@pytest.mark.parametrize("cmd", ["install", "update"])
@pytest.mark.parametrize("explicit", ["0", "1", ""])
def test_an_explicit_value_is_never_changed(tmp_path: Path, cmd: str, explicit: str) -> None:
    result, env_file, _ = _run(tmp_path, cmd,
                               host_env=f"BOSS_API_KEY=x\nBOSS_DEDICATED_HOST={explicit}\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _marks(env_file) == [f"BOSS_DEDICATED_HOST={explicit}"]
    assert "dedicated BOSS host" not in result.stdout


def test_an_update_of_an_unmarked_install_marks_it_before_the_build(tmp_path: Path) -> None:
    result, env_file, log = _run(tmp_path, "update", host_env="BOSS_API_KEY=x\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _marks(env_file) == ["BOSS_DEDICATED_HOST=1"]
    build_env = log.split("--- build env\n", 1)[1]
    assert "BOSS_DEDICATED_HOST=1" in build_env.splitlines()
    # The api and worker are recreated after the mark, so the reaper reads it.
    assert "up -d --no-deps --force-recreate api worker" in log


def test_a_second_update_writes_nothing_twice(tmp_path: Path) -> None:
    result, env_file, _ = _run(tmp_path, "update", host_env="BOSS_API_KEY=x\nBOSS_DEDICATED_HOST=1\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _marks(env_file) == ["BOSS_DEDICATED_HOST=1"]


def test_the_engine_reads_the_name_remote_sh_writes() -> None:
    reaper = (ROOT / "engine" / "boss_engine" / "services" / "resource_reaper.py").read_text()
    assert 'os.environ.get("BOSS_DEDICATED_HOST"' in reaper
    compose = (ROOT / "scripts" / "install" / "community-compose.yml").read_text()
    assert "BOSS_DEDICATED_HOST: ${BOSS_DEDICATED_HOST:-}" in compose
