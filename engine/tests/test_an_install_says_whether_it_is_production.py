"""Every BOSS install says whether it is development or production (#307 review P1-1, BOSSAPP-1441).

The operator, 2026-11-23: "for local development I dont need to validate just make the output..
then human can validate it… it makes sense for PROD deployment and other fixes that release to
PROD." Agreed rule: a local install (laptop, Colima, localhost) is development; an unlabelled
remote is production.

Dev autonomy read BOSS_INSTALL_TIER and treated "unset" as development, and nothing set it: compose
did not pass it to the api or the worker, and neither installer wrote it, so every install —
a customer's server included — counted as development. Now:
  * compose passes BOSS_INSTALL_TIER to the api and the worker;
  * install.sh writes it (dev on a laptop, prod with a public console origin, --install-tier wins,
    a rerun keeps the previous one);
  * remote.sh writes prod on the hosts it installs or updates unless --install-tier dev is passed;
  * unset, the engine reads BOSS's own public URL (test_one_production_resolver).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from . import test_remote_sh_marks_a_dedicated_host as remote

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "scripts" / "install" / "community-compose.yml").read_text()
INSTALL = (ROOT / "install.sh").read_text()
RESTORE = ROOT / "scripts" / "install" / "restore_preserved_env.sh"


def _service_block(name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z0-9_-]*:\n)", COMPOSE, re.S | re.M)
    assert match, name
    return match.group(1)


@pytest.mark.parametrize("service", ["api", "worker"])
def test_compose_passes_the_tier_to_the_services_that_decide(service):
    assert "BOSS_INSTALL_TIER: ${BOSS_INSTALL_TIER:-}" in _service_block(service)


def test_install_sh_writes_the_tier_and_takes_a_flag():
    assert "BOSS_INSTALL_TIER=${INSTALL_TIER}" in INSTALL
    assert "--install-tier)" in INSTALL and "--install-tier dev|prod" in INSTALL
    assert 'BOSS_INSTALL_TIER_EXPLICIT="$INSTALL_TIER_EXPLICIT"' in INSTALL


def _restore(tmp_path: Path, old: str, new: str, explicit: str) -> str:
    old_f, new_f = tmp_path / "old.env", tmp_path / "new.env"
    old_f.write_text(old)
    new_f.write_text(new)
    env = {**os.environ, "BOSS_INSTALL_TIER_EXPLICIT": explicit}
    subprocess.run(["sh", str(RESTORE), str(old_f), str(new_f)], check=True, env=env,
                   capture_output=True, text=True, timeout=30)
    return [l for l in new_f.read_text().splitlines() if l.startswith("BOSS_INSTALL_TIER=")][0]


def test_a_rerun_keeps_the_previous_tier_unless_the_flag_says_otherwise(tmp_path):
    assert _restore(tmp_path, "BOSS_INSTALL_TIER=prod\n", "BOSS_INSTALL_TIER=dev\n", "0") \
        == "BOSS_INSTALL_TIER=prod"
    assert _restore(tmp_path, "BOSS_INSTALL_TIER=prod\n", "BOSS_INSTALL_TIER=dev\n", "1") \
        == "BOSS_INSTALL_TIER=dev"
    # An old install without the line: the installer's own answer stands.
    assert _restore(tmp_path, "BOSS_API_KEY=x\n", "BOSS_INSTALL_TIER=dev\n", "0") \
        == "BOSS_INSTALL_TIER=dev"


def _tiers(env_file: Path) -> list[str]:
    return [l for l in env_file.read_text().splitlines() if l.startswith("BOSS_INSTALL_TIER=")]


@pytest.mark.parametrize("cmd", ["install", "update"])
def test_a_remote_host_is_production_unless_the_operator_says_dev(tmp_path, cmd):
    result, env_file, _ = remote._run(tmp_path, cmd, host_env="BOSS_API_KEY=x\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _tiers(env_file) == ["BOSS_INSTALL_TIER=prod"]


@pytest.mark.parametrize("cmd", ["install", "update"])
def test_the_flag_sets_a_remote_hosts_tier(tmp_path, cmd):
    result, env_file, _ = remote._run(tmp_path, cmd,
                                      host_env="BOSS_API_KEY=x\nBOSS_INSTALL_TIER=prod\n",
                                      args=("--install-tier", "dev"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _tiers(env_file) == ["BOSS_INSTALL_TIER=dev"]


@pytest.mark.parametrize("cmd", ["install", "update"])
def test_an_update_keeps_a_tier_already_set(tmp_path, cmd):
    result, env_file, _ = remote._run(tmp_path, cmd,
                                      host_env="BOSS_API_KEY=x\nBOSS_INSTALL_TIER=dev\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _tiers(env_file) == ["BOSS_INSTALL_TIER=dev"]


def test_a_first_remote_install_hands_the_installer_prod(tmp_path):
    result, _, log = remote._run(tmp_path, "install", host_env=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--install-tier prod" in log


def test_the_readme_says_it_in_one_line():
    readme = (ROOT / "README.md").read_text()
    assert "BOSS_INSTALL_TIER" in readme and "--install-tier dev" in readme


def _default_tier(origin: str, uname: str) -> str:
    """Run install.sh's own default_install_tier, as written in the file."""
    start = INSTALL.index("default_install_tier() {")
    end = INSTALL.index("\n}\n", start) + 3
    out = subprocess.run(["bash", "-c", INSTALL[start:end] + 'default_install_tier "$1" "$2"',
                          "_", origin, uname], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.parametrize("origin, uname, tier", [
    ("", "Darwin", "dev"), ("http://localhost:3000", "Darwin", "dev"),
    ("http://127.0.0.1:3000", "Darwin", "dev"), ("http://[::1]:3000", "Darwin", "dev"),
    # A server reached through an SSH tunnel has a loopback origin too: prod off a Mac.
    ("", "Linux", "prod"), ("http://localhost:3000", "Linux", "prod"),
    ("https://boss.example.com", "Darwin", "prod"),
    ("http://localhost.example.com", "Darwin", "prod"),
])
def test_the_installer_never_writes_a_blank_tier(origin, uname, tier):
    assert _default_tier(origin, uname) == tier
