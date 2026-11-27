"""ENT-REC-54: the installer records where BOSS was installed from and seeds its self-source
mirror, offline, on every install and upgrade, and never fails the install doing it. An archive
install says plainly why BOSS cannot fix itself. On a development install the person who ran the
installer also gets the host self-installer, enrolled and running as a user service.

These run install.sh's own blocks (between their markers) against throwaway checkouts, with the
real scripts/install/self_fix_installer.py and stand-ins for Docker and the service manager.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not shutil.which("git") or not shutil.which("bash"),
                                reason="needs git and bash")
UI = ('ui_ok() { echo "OK $*"; }\nui_warn() { echo "WARN $*"; }\n'
      'ui_info() { echo "INFO $*"; }\n')


def _block(name: str) -> str:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    start = text.index(f"# >>> {name}")
    return text[start:text.index(f"# <<< {name}")]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@boss.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _tree(tmp: Path) -> Path:
    co = tmp / "install"
    (co / "scripts/install").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/install/self_fix_installer.py", co / "scripts/install/")
    (co / "app.py").write_text("print('boss')\n")
    (co / ".boss/selfsrc").mkdir(parents=True)
    return co


def _checkout(tmp: Path) -> Path:
    co = _tree(tmp)
    (co / ".gitignore").write_text(".boss/\n")
    _git(co, "init", "-q", "-b", "main")
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "release 1")
    return co


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k not in ("CI", "BOSS_SELF_FIX", "BOSS_SELF_FIX_INSTALLER") and
            not k.startswith("GIT_")}


def _source(co: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + UI + _block("self-fix source")],
                          cwd=co, capture_output=True, text=True, env=_env())


def _record(co: Path) -> dict:
    return json.loads((co / ".boss/selfsrc/install.json").read_text())


def _holds(co: Path, sha: str) -> bool:
    return subprocess.run(["git", "--git-dir", str(co / ".boss/selfsrc/boss.git"), "cat-file",
                           "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0


# ── the record and the mirror ────────────────────────────────────────────────────────────────

def test_a_fresh_install_records_its_checkout_and_seeds_the_mirror(tmp_path):
    co = _checkout(tmp_path)
    _git(co, "tag", "v1.0-rc10.37")
    _git(co, "remote", "add", "origin", "https://x-access-token:s3cr3t@github.com/o/boss.git")
    out = _source(co)
    assert out.returncode == 0 and "OK Self-fix source recorded and mirrored" in out.stdout, out
    head = _git(co, "rev-parse", "HEAD")
    record = _record(co)
    assert record["path"] == str(co.resolve()) and record["kind"] == "git"
    assert record["commit"] == head and record["dirty"] is False
    assert record["release_tag"] == "v1.0-rc10.37" and record["branch"] == "main"
    assert record["mirror"]["seeded"] is True and _holds(co, head)
    assert record["upstream"] == "https://github.com/o/boss.git", "never a credential"
    assert "s3cr3t" not in (co / ".boss/selfsrc/install.json").read_text() + out.stdout
    mode = stat.S_IMODE((co / ".boss/selfsrc/install.json").stat().st_mode)
    assert mode == 0o644, "the containers' user reads it through the read-only mount"


def test_an_upgrade_moves_the_record_and_the_mirror_to_the_new_commit(tmp_path):
    co = _checkout(tmp_path)
    assert _source(co).returncode == 0
    (co / "app.py").write_text("print('boss 2')\n")
    _git(co, "commit", "-qam", "release 2")
    new = _git(co, "rev-parse", "HEAD")
    assert not _holds(co, new)
    out = _source(co)
    assert out.returncode == 0 and _holds(co, new) and _record(co)["commit"] == new


def test_a_dirty_checkout_is_recorded_as_such_and_warned(tmp_path):
    co = _checkout(tmp_path)
    (co / "app.py").write_text("print('edited here')\n")
    (co / "notes.txt").write_text("an untracked note changes nothing that runs\n")
    out = _source(co)
    assert out.returncode == 0, out
    record = _record(co)
    assert record["dirty"] is True and record["changed_files"] == 1
    assert "not committed" in record["why"] and str(co.resolve()) in record["why"]
    assert "WARN This checkout has uncommitted changes" in out.stdout
    assert _holds(co, _git(co, "rev-parse", "HEAD")), "the committed base is still mirrored"


def test_an_untracked_file_alone_is_clean(tmp_path):
    co = _checkout(tmp_path)
    (co / "root.crt").write_text("a local CA\n")
    assert _source(co).returncode == 0 and _record(co)["dirty"] is False


def test_an_archive_install_says_plainly_why_it_cannot_fix_itself(tmp_path):
    co = _tree(tmp_path)                       # no .git: what remote.sh unpacks
    out = _source(co)
    assert out.returncode == 0, out
    assert "INFO BOSS cannot fix itself here" in out.stdout and "archive" in out.stdout
    record = _record(co)
    assert record["kind"] == "archive" and record["path"] == str(co.resolve())
    assert "git clone" in record["why"] and record["mirror"]["seeded"] is False
    assert not (co / ".boss/selfsrc/boss.git").exists()


def test_a_mirror_that_cannot_be_made_warns_and_never_stops_the_install(tmp_path):
    co = _checkout(tmp_path)
    mirror = co / ".boss/selfsrc/boss.git"
    mirror.mkdir()
    (mirror / "HEAD").write_text("not a repository\n")
    out = _source(co)
    assert out.returncode == 0 and "WARN Self-fix source mirror was not refreshed" in out.stdout
    assert _record(co)["mirror"]["error"], "the record says why, for Govern › Self-fix"


# ── the host self-installer on a development install ────────────────────────────────────────

def _stub(tmp: Path, name: str, body: str) -> Path:
    path = tmp / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def _enrol(tmp: Path, co: Path, *, tier: str, docker_ok: bool = True,
           extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    calls = tmp / "docker-calls"
    docker = _stub(tmp, "docker", f'echo "$*" >> {calls}\n' + (
        'for a in "$@"; do [[ "$a" == self-installer-key ]] && echo boss_sak_testkey; done\n'
        'exit 0\n' if docker_ok else "exit 1\n"))
    script = (UI + f'INSTALL_TIER={tier}\nINSTALL_DIR="{co}"\nAPI_URL=http://127.0.0.1:9\n'
              f'DOCKER=("{docker}")\nENV_FILE="{co}/.env"\n'
              'env_value() { grep -m 1 "^$1=" "$ENV_FILE" | cut -d= -f2-; }\n'
              + _block("self-fix installer enrolment"))
    env = {**_env(), "BOSS_SELF_FIX_SERVICE_CTL": "true",
           "BOSS_SELF_FIX_SERVICE_DIR": str(tmp / "units"), "BOSS_SELF_FIX_PLATFORM": "linux",
           **(extra or {})}
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], cwd=co,
                          capture_output=True, text=True, env=env)


@pytest.fixture()
def seeded(tmp_path):
    co = _checkout(tmp_path)
    assert _source(co).returncode == 0
    return co


def test_a_development_install_enrols_its_self_installer(tmp_path, seeded):
    out = _enrol(tmp_path, seeded, tier="dev")
    assert out.returncode == 0 and "OK Self-installer enrolled" in out.stdout, out
    key = seeded / ".boss/self-installer.key"
    assert key.read_text().strip() == "boss_sak_testkey"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert "boss_sak_testkey" not in out.stdout + out.stderr, "the key is never printed"
    unit = (tmp_path / "units" / "boss-self-fix-watch.service").read_text()
    assert "watch" in unit and str(seeded.resolve()) in unit and "boss_sak" not in unit
    assert "exec -T -u boss api python -m boss_engine.identity.cli self-installer-key" in \
        (tmp_path / "docker-calls").read_text()


@pytest.mark.parametrize("tier, extra", [
    ("prod", {}),                                      # production: a person enrols it
    ("dev", {"BOSS_SELF_FIX_INSTALLER": "off"}),       # the operator said no
    ("dev", {"BOSS_SELF_FIX": "off"}),                 # self-fix is off on this install
    ("dev", {"CI": "true"}),                           # nobody's own machine
])
def test_otherwise_the_installer_leaves_it_to_a_person(tmp_path, seeded, tier, extra):
    out = _enrol(tmp_path, seeded, tier=tier, extra=extra)
    assert out.returncode == 0 and out.stdout == "", out
    assert not (seeded / ".boss/self-installer.key").exists()
    assert not (tmp_path / "docker-calls").exists()


def test_an_archive_install_is_not_enrolled(tmp_path):
    co = _tree(tmp_path)
    assert _source(co).returncode == 0
    out = _enrol(tmp_path, co, tier="dev")
    assert out.returncode == 0 and out.stdout == "" and not (tmp_path / "docker-calls").exists()


def test_an_enrolment_that_fails_warns_and_never_stops_the_install(tmp_path, seeded):
    out = _enrol(tmp_path, seeded, tier="dev", docker_ok=False)
    assert out.returncode == 0, out
    assert "WARN The self-installer was not set up" in out.stdout
    assert "boss self-fix enable" in out.stdout
    assert not (seeded / ".boss/self-installer.key").exists()
