"""ENT-REC-54: BOSS could not fix bugs in itself because it did not know its own source.

An enterprise run on a local-only install found no self-source mirror, self-fix off by default
for several people, and no host installer. BOSS now knows where it was installed from (the
installer's record, never the checkout itself) and says, in one place, what a self-fix still
needs and the one action for each missing piece. These are the engine halves: the record read
back, the readiness pieces, the switch's default, and the agents' `inspect_self` answer.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_source as src

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@boss.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """A git checkout, its selfsrc directory, and settings pointing at the mirror there."""
    co = tmp_path / "boss"
    co.mkdir()
    (co / "README.md").write_text("boss\n")
    _git(co, "init", "-q", "-b", "main")
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "release")
    sha = _git(co, "rev-parse", "HEAD")
    selfsrc = co / ".boss/selfsrc"
    selfsrc.mkdir(parents=True)
    settings = SimpleNamespace(self_fix_source=str(selfsrc / "boss.git"))
    monkeypatch.setenv("BOSS_SOURCE_SHA", sha)
    monkeypatch.delenv("BOSS_STACK_HEAD", raising=False)
    monkeypatch.delenv("BOSS_SELF_FIX", raising=False)
    return SimpleNamespace(co=co, sha=sha, selfsrc=selfsrc, settings=settings)


def _record(box, **fields) -> dict:
    body = {"path": str(box.co), "kind": "git", "commit": box.sha, "dirty": False,
            "changed_files": 0, "release_tag": "v1.0-rc10.37", "release_ref": "v1.0-rc10.37",
            "branch": None, "upstream": "https://github.com/seanventures/boss.git",
            "recorded_at": "2026-11-27T00:00:00+00:00",
            "mirror": {"path": str(box.selfsrc / "boss.git"), "seeded": True, "error": None},
            **fields}
    (box.selfsrc / "install.json").write_text(json.dumps(body))
    return body


def _mirror(box) -> None:
    _git(box.co, "clone", "-q", "--mirror", str(box.co), str(box.selfsrc / "boss.git"))


class _Conn:
    """No database: readiness's database reads are stood in by `no_db`."""


@pytest.fixture()
def no_db(monkeypatch):
    """Readiness reads the switch, the installer's sighting and its key through the database;
    the pieces under test here are the install's, so those reads are stood in."""
    state = {"enrolled": False, "watching": False, "on": True}
    monkeypatch.setattr(sf, "_installer_enrolled", lambda c: state["enrolled"])
    monkeypatch.setattr(sf, "switch", lambda c: {"on": state["on"], "why": "" if state["on"]
                                                 else "nobody has claimed this install yet"})
    from boss_engine.services import autonomy_policy, self_fix_install

    monkeypatch.setattr(self_fix_install, "installer_seen",
                        lambda c: {"last_seen_at": None, "watching": state["watching"]})
    monkeypatch.setattr(autonomy_policy, "load", lambda c, pid, **k: autonomy_policy.DEFAULT)
    return state


def _pieces(box, **kw) -> dict:
    got = sf.readiness(_Conn(), box.settings, project_id="p1", **kw)
    return {p["key"]: p for p in got["pieces"]} | {"_": got}


def test_the_record_says_where_boss_is_installed_and_what_runs(box):
    _record(box)
    _mirror(box)
    said = src.describe_install(box.settings)
    assert said["path"] == str(box.co) and said["commit"] == box.sha
    assert said["matches_running"] and said["release_tag"] == "v1.0-rc10.37"
    assert str(box.co) in said["sentence"] and "running now" in said["sentence"]


def test_a_checkout_that_moved_since_the_install_is_said(box, monkeypatch):
    _record(box)
    monkeypatch.setenv("BOSS_SOURCE_SHA", "f" * 40)
    said = src.describe_install(box.settings)
    assert not said["matches_running"] and "installer has not run" in said["sentence"]


def test_without_a_record_boss_says_how_to_get_one(box):
    said = src.describe_install(box.settings)
    assert not said["recorded"] and "boss self-fix seed" in said["sentence"]


def test_every_piece_ready_on_a_development_install(box, no_db, monkeypatch):
    _record(box)
    _mirror(box)
    no_db.update(enrolled=True, watching=True)
    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    got = _pieces(box)
    assert got["_"]["ready"] is True, got
    assert got["mode"]["state"] == "Installs on its own" and got["_"]["auto_install"] is True
    assert "Decided for you" in got["mode"]["why"] and "Remove" in got["mode"]["why"]
    assert got["source"]["state"].startswith(f"{box.co} @ {box.sha[:12]}")


def test_production_asks_a_person_for_each_install(box, no_db, monkeypatch):
    _record(box)
    _mirror(box)
    no_db.update(enrolled=True, watching=True)
    monkeypatch.setenv("BOSS_INSTALL_TIER", "prod")
    got = _pieces(box)
    assert got["mode"]["state"] == "You approve each install" and not got["_"]["auto_install"]


def test_ask_me_in_development_also_asks(box, no_db, monkeypatch):
    from boss_engine.services import autonomy_policy

    _record(box)
    _mirror(box)
    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    monkeypatch.setattr(autonomy_policy, "load", lambda c, pid, **k: autonomy_policy.Policy(
        dev=autonomy_policy.ASK))
    got = _pieces(box)
    assert got["mode"]["state"] == "You approve each install" and "Ask me" in got["mode"]["why"]


def test_each_missing_piece_names_one_action(box, no_db, monkeypatch):
    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    no_db.update(on=False)
    got = _pieces(box)
    assert got["_"]["ready"] is False
    assert got["source"]["action"]["command"] == sf.SEED_COMMAND
    assert got["mirror"]["state"] == "Not seeded"
    assert got["mirror"]["action"]["command"] == sf.SEED_COMMAND
    assert got["installer"]["state"] == "Not enrolled"
    assert got["installer"]["action"]["command"] == sf.ENABLE_COMMAND
    assert got["mode"]["action"] == {"kind": "switch", "label": "Turn on"}
    for piece in ("source", "mirror", "installer", "mode"):
        assert got[piece]["action"] and got[piece]["why"], piece


def test_an_enrolled_installer_that_is_not_running_is_said(box, no_db):
    no_db.update(enrolled=True, watching=False)
    got = _pieces(box)
    assert got["installer"]["state"] == "Not watching"
    assert got["installer"]["action"]["command"] == sf.ENABLE_COMMAND


def test_a_mirror_behind_the_running_build_is_said(box, no_db, monkeypatch):
    _record(box)
    _mirror(box)
    (box.co / "new.txt").write_text("2\n")
    _git(box.co, "add", "-A")
    _git(box.co, "commit", "-qm", "release 2")
    monkeypatch.setenv("BOSS_SOURCE_SHA", _git(box.co, "rev-parse", "HEAD"))
    got = _pieces(box)
    assert got["mirror"]["state"] == "Behind the running build"
    assert got["mirror"]["action"]["command"] == sf.SEED_COMMAND


def test_an_archive_install_says_plainly_why_and_what_to_do(box, no_db, monkeypatch):
    why = "BOSS was installed from an archive, not a git checkout, so it has no history."
    _record(box, kind="archive", commit=None, why=why)
    monkeypatch.setenv("BOSS_SOURCE_SHA", "unknown")
    got = _pieces(box)
    assert got["source"]["state"] == "An archive" and got["source"]["why"] == why
    assert "git clone" in got["source"]["action"]["command"]
    assert got["mirror"]["state"] == "Not possible"
    assert sf.install_capable(box.settings) == (False, why)


def test_a_dirty_checkout_cannot_fix_itself_and_says_why(box, no_db, monkeypatch):
    why = "2 tracked file(s) have changes that are not committed"
    _record(box, dirty=True, changed_files=2, why=why)
    _mirror(box)
    monkeypatch.setenv("BOSS_SOURCE_SHA", "unknown")
    got = _pieces(box)
    assert got["source"]["state"] == "Uncommitted changes"
    assert got["source"]["action"]["command"] == f"cd {box.co} && ./install.sh"
    ok, said = sf.install_capable(box.settings)
    assert not ok and said == why
    assert got["mirror"]["ok"], "the mirror still holds the committed base"


@pytest.mark.parametrize("tier, people, on", [
    ("dev", 1, True), ("dev", 3, True), ("prod", 1, True), ("prod", 3, False),
    ("dev", 0, False), ("prod", 0, False)])
def test_the_switch_defaults_on_for_a_development_install_whoever_uses_it(
        monkeypatch, tier, people, on):
    """Several people on a DEVELOPMENT install no longer switch self-fix off: BOSS-side fixes
    get full control there, and the autonomy policy decides whether an install asks. A shared
    PRODUCTION install still starts off: an Owner decides, once, whether BOSS may change its
    own code there."""
    monkeypatch.setenv("BOSS_INSTALL_TIER", tier)
    monkeypatch.delenv("BOSS_SELF_FIX", raising=False)
    monkeypatch.setattr(sf, "_state", lambda c: None)
    monkeypatch.setattr(sf, "_active_people", lambda c: people)
    got = sf.switch(object())
    assert got["on"] is on, got
    if not on and people > 1:
        assert "production install" in got["why"]


def test_inspect_self_answers_where_boss_is_installed(box, monkeypatch):
    from boss_engine.tools.builtin import build_default_registry

    _record(box)
    _mirror(box)
    spec = build_default_registry().get("inspect_self")
    assert "WHERE BOSS IS INSTALLED" in spec.description
    out = spec.handler(SimpleNamespace(settings=box.settings), project_id="p", actor="u", args={})
    assert out["install"]["path"] == str(box.co) and out["install"]["commit"] == box.sha
    assert out["install"]["can_fix_itself"] is True and "cpu" in out
