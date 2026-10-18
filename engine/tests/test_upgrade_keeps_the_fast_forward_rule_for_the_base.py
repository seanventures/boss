"""P0: upgrades keep local BOSS self-fixes, and keep the fast-forward rule for the release
itself (BOSSAPP-571 R8, spec §6.4, F7, operator decision 2).

The REAL scripts/install/upgrade.sh and the REAL self-installer against real git repositories:
an "origin" with releases v0.0.1 and v0.0.2, an install at v0.0.1 carrying one self-fix on its
stack ref, a fake Docker. The fetched installer is a stub that checks the release out (as
install.sh does, keeping the stack record); everything about the stack is the real code.

  a release that does not fast-forward the base      → refused, nothing changed
  the fix applies on the new release                  → carried: the new stack is the release + it
  the new release already has it (same patch)         → dropped
  an upstream commit names it (BOSS-Self-Fix trailer) → dropped
  it conflicts with the new release                   → the upgrade waits (nothing changed);
                                                        --without SFX upgrades without it
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ._self_fix_support import FAKE_DOCKER, git

ROOT = Path(__file__).resolve().parents[2]
SFX = "sfx_0123456789abcdef0123"
STUB_INSTALLER = r'''#!/usr/bin/env bash
set -euo pipefail
DIR=""; REF=""
while [[ $# -gt 0 ]]; do case "$1" in --dir) DIR="$2"; shift 2;; --ref) REF="$2"; shift 2;; *) shift;; esac; done
git -C "$DIR" checkout -q --detach "$REF"
stack_ref="$(grep -m1 '^BOSS_STACK_REF=' "$DIR/.boss-release" | cut -d= -f2- || true)"
stack_head="$(grep -m1 '^BOSS_STACK_HEAD=' "$DIR/.boss-release" | cut -d= -f2- || true)"
printf 'BOSS_RELEASE_REF=%s\n' "${BOSS_RELEASE_REF_NAME:-$REF}" > "$DIR/.boss-release"
[[ -z "$stack_ref" ]] || printf 'BOSS_STACK_REF=%s\nBOSS_STACK_HEAD=%s\n' "$stack_ref" "$stack_head" >> "$DIR/.boss-release"
echo "stub installed $REF"
'''
BUG = "def double(x):\n    return x + 2\n"
FIXED = "def double(x):\n    return x * 2\n"


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    _write(origin, "engine/widget.py", BUG)
    _write(origin, "install.sh", STUB_INSTALLER)
    _write(origin, "scripts/install/backup.sh", "#!/usr/bin/env bash\necho backup\n")
    _write(origin, "scripts/install/community-compose.yml", "services: {}\n")
    shutil.copy(ROOT / "scripts/install/self_fix_installer.py",
                origin / "scripts/install/self_fix_installer.py")
    _write(origin, ".gitignore", ".env\n.boss-release\n.boss/\n")
    v1 = _commit(origin, "release 1")
    git(origin, "tag", "v0.0.1")
    install = tmp_path / "install"
    git(tmp_path, "clone", "-q", str(origin), str(install))
    git(install, "checkout", "-q", "--detach", "v0.0.1")
    (install / ".env").write_text("BOSS_COMMUNITY_WEB_ENABLED=0\n")
    # THE STACK: one self-fix commit over v0.0.1, on refs/boss/stack/v0.0.1 (HEAD stays put).
    work = tmp_path / "fixwork"
    git(install, "worktree", "add", "-q", "--detach", str(work), v1)
    _write(work, "engine/widget.py", FIXED)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", f"fix(engine): double doubles\n\nBOSS-Self-Fix: {SFX}\n")
    fix = git(work, "rev-parse", "HEAD")
    git(install, "worktree", "remove", "--force", str(work))
    git(install, "update-ref", "refs/boss/stack/v0.0.1", fix)
    (install / ".boss-release").write_text(
        f"BOSS_RELEASE_REF=v0.0.1\nBOSS_STACK_REF=refs/boss/stack/v0.0.1\nBOSS_STACK_HEAD={fix}\n")
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    state = tmp_path / "docker.json"
    state.write_text(json.dumps({"images": {"boss-community-api:boss-community": v1},
                                 "calls": [], "running": v1}))
    return {"origin": origin, "install": install, "v1": v1, "fix": fix, "docker": docker,
            "state": state, "tmp": tmp_path}


def _release(world, name: str, edit: dict[str, str], message: str = "release 2") -> str:
    origin = world["origin"]
    for rel, body in edit.items():
        _write(origin, rel, body)
    sha = _commit(origin, message)
    git(origin, "tag", name)
    return sha


def _upgrade(world, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BOSS_SELF_FIX_DOCKER": str(world["docker"]),
           "FAKE_DOCKER_STATE": str(world["state"]), "BOSS_SELF_FIX_API": "http://127.0.0.1:9",
           "BOSS_SELF_INSTALLER_KEY": "boss_sak_test", "HOME": str(world["tmp"])}
    return subprocess.run(["bash", str(ROOT / "scripts/install/upgrade.sh"), "--dir",
                           str(world["install"]), *args], capture_output=True, text=True,
                          env=env, timeout=180)


def _record(world) -> dict[str, str]:
    out = {}
    for line in (world["install"] / ".boss-release").read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def _plan(world) -> dict:
    return json.loads((world["install"] / ".boss" / "stack-plan.json").read_text())


def test_a_base_that_does_not_fast_forward_is_still_refused(world):
    origin = world["origin"]
    git(origin, "checkout", "-q", "--orphan", "elsewhere")
    _write(origin, "engine/widget.py", "x = 1\n")
    _commit(origin, "unrelated history")
    git(origin, "tag", "v0.0.9")
    done = _upgrade(world, "--ref", "v0.0.9")
    assert done.returncode != 0 and "cannot fast-forward" in done.stderr
    assert git(world["install"], "rev-parse", "HEAD") == world["v1"]
    assert git(world["install"], "rev-parse", "refs/boss/stack/v0.0.1") == world["fix"]


def test_a_fix_that_still_applies_is_carried_onto_the_new_release(world):
    v2 = _release(world, "v0.0.2", {"docs/notes.md": "release notes\n"})
    done = _upgrade(world, "--ref", "v0.0.2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "local fix sfx_0123456789abcdef0123: carried" in done.stdout
    install = world["install"]
    assert git(install, "rev-parse", "HEAD") == v2, "the checkout is the plain release"
    record = _record(world)
    assert record["BOSS_STACK_REF"] == "refs/boss/stack/v0.0.2"
    head = git(install, "rev-parse", "refs/boss/stack/v0.0.2")
    assert git(install, "rev-parse", f"{head}^") == v2
    assert git(install, "show", f"{head}:engine/widget.py") == FIXED.strip()
    assert record["BOSS_STACK_HEAD"] == head
    assert [f["outcome"] for f in _plan(world)["fixes"]] == ["carried"]
    calls = json.loads(world["state"].read_text())["calls"]
    assert any("build" in c for c in calls) and any("up" in c for c in calls)


def test_a_fix_the_release_already_has_is_dropped(world):
    _release(world, "v0.0.2", {"engine/widget.py": FIXED})
    done = _upgrade(world, "--ref", "v0.0.2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "dropped upstream" in done.stdout
    plan = _plan(world)["fixes"]
    assert plan[0]["outcome"] == "dropped_upstream" and plan[0]["how"] == "commit"
    assert _record(world)["BOSS_STACK_HEAD"] == git(world["install"], "rev-parse", "HEAD")


def test_a_fix_an_upstream_commit_names_is_dropped(world):
    _release(world, "v0.0.2", {"engine/widget.py": "def double(x):\n    return 2 * x\n"},
             message=f"fix(engine): double, upstream's way\n\nBOSS-Self-Fix: {SFX}\n")
    done = _upgrade(world, "--ref", "v0.0.2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _plan(world)["fixes"][0] == {**_plan(world)["fixes"][0], "outcome": "dropped_upstream",
                                        "how": "trailer"}


def test_a_fix_that_conflicts_waits_for_its_rework_and_nothing_changes(world):
    _release(world, "v0.0.2", {"engine/widget.py": "def double(x):\n    return x + x + 0\n"})
    done = _upgrade(world, "--ref", "v0.0.2")
    assert done.returncode != 0 and "no longer apply" in done.stderr
    assert git(world["install"], "rev-parse", "HEAD") == world["v1"]
    assert _record(world)["BOSS_STACK_REF"] == "refs/boss/stack/v0.0.1"


def test_upgrading_without_it_is_a_choice_that_is_recorded(world):
    v2 = _release(world, "v0.0.2", {"engine/widget.py": "def double(x):\n    return x + x + 0\n"})
    done = _upgrade(world, "--ref", "v0.0.2", "--without", SFX)
    assert done.returncode == 0, done.stdout + done.stderr
    assert git(world["install"], "rev-parse", "HEAD") == v2
    assert _plan(world)["fixes"][0]["outcome"] == "removed"
    assert _record(world)["BOSS_STACK_HEAD"] == v2
