"""brec_069e56332d714ad08c01: after an upgrade the self-fix source mirror stayed on the old
commit until someone re-seeded it by hand, so a self-fix could diagnose stale code. An upgrade
reruns install.sh at the new commit; its self-fix source block brings the mirror up to the
checkout's HEAD and never fails the install. Since ENT-REC-54 it also seeds a mirror nobody seeded
(test_the_installer_records_where_boss_is_installed.py has the record, dirty and archive cases)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not shutil.which("git") or not shutil.which("bash"),
                                reason="needs git and bash")


def _block() -> str:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    start = text.index("# >>> self-fix source")
    return text[start:text.index("# <<< self-fix source")]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _checkout(tmp: Path) -> Path:
    co = tmp / "install"
    (co / "scripts/install").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/install/self_fix_installer.py", co / "scripts/install/")
    _git(co, "init", "-q")
    _git(co, "config", "user.email", "t@boss.invalid")
    _git(co, "config", "user.name", "t")
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "release 1")
    return co


def _run_block(co: Path) -> subprocess.CompletedProcess:
    script = ('ui_ok() { echo "OK $*"; }\nui_warn() { echo "WARN $*"; }\n'
              'ui_info() { echo "INFO $*"; }\n' + _block())
    return subprocess.run(["bash", "-c", script], cwd=co, capture_output=True, text=True)


def test_a_seeded_mirror_follows_the_new_release(tmp_path):
    co = _checkout(tmp_path)
    mirror = co / ".boss/selfsrc/boss.git"
    mirror.parent.mkdir(parents=True)
    _git(co, "clone", "-q", "--mirror", str(co), str(mirror))
    (co / "new.txt").write_text("release 2\n")
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "release 2")
    new = _git(co, "rev-parse", "HEAD")
    assert subprocess.run(["git", "--git-dir", str(mirror), "cat-file", "-e", new],
                          capture_output=True).returncode != 0  # stale before
    out = _run_block(co)
    assert out.returncode == 0 and "OK Self-fix source recorded and mirrored" in out.stdout, out
    _git(co, "--git-dir", str(mirror), "cat-file", "-e", new)  # the running commit is there


def test_an_unseeded_install_is_seeded(tmp_path):
    """ENT-REC-54: a mirror nobody seeded left BOSS unable to fix itself. Every install seeds it."""
    co = _checkout(tmp_path)
    (co / ".boss/selfsrc").mkdir(parents=True)
    out = _run_block(co)
    assert out.returncode == 0 and "OK Self-fix source recorded and mirrored" in out.stdout, out
    _git(co, "--git-dir", str(co / ".boss/selfsrc/boss.git"), "cat-file", "-e",
         _git(co, "rev-parse", "HEAD"))


def test_a_refresh_that_fails_warns_and_never_stops_the_install(tmp_path):
    co = _checkout(tmp_path)
    mirror = co / ".boss/selfsrc/boss.git"
    mirror.mkdir(parents=True)
    (mirror / "HEAD").write_text("not a repository\n")
    out = _run_block(co)
    assert out.returncode == 0 and "WARN Self-fix source mirror was not refreshed" in out.stdout
