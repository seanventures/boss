"""repo='boss' reads the WHOLE repository at the running commit, console included
(brec_44ad2f55).

list_source/read_code/search_code with repo='boss' read the api image's /app, which carries only
what the API executes — no apps/web — so a console bug could never be read with file:line
evidence. With the self-source mirror on the install, they read a checkout of it at the running
stack head (or a named ref); without it, the running tree. Either way the answer says which.
"""
from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from boss_engine.services import self_fix_source
from boss_engine.tools import build_default_registry, builtin

from ._self_fix_support import git, mini_boss

CONSOLE_FILE = "apps/web/app/govern/govern-home.tsx"
CONSOLE_BODY = 'export const LABEL = "Self-fix tab";\n'


class _Ctx:
    def __init__(self, settings):
        self.settings = settings

    def begin(self):  # no database: the token lookup fails closed to ""
        raise RuntimeError("no database in this test")


@pytest.fixture()
def boss_with_mirror(tmp_path, monkeypatch):
    repo, _ = mini_boss(tmp_path)
    (repo / "apps/web/app/govern").mkdir(parents=True)
    (repo / CONSOLE_FILE).write_text(CONSOLE_BODY)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "console")
    head = git(repo, "rev-parse", "HEAD")
    first = git(repo, "rev-parse", "HEAD~1")
    mirror = tmp_path / "selfsrc" / "boss.git"
    self_fix_source.seed(repo, mirror)
    monkeypatch.setattr(builtin, "_REPO_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("BOSS_STACK_HEAD", head)
    return SimpleNamespace(settings=SimpleNamespace(self_fix_source=str(mirror)),
                           head=head, first=first)


def _call(settings, tool, args):
    spec = build_default_registry().get(tool)
    return spec.handler(_Ctx(settings), project_id="p1", actor="ceo", args=args)


def test_list_source_shows_the_console(boss_with_mirror):
    out = _call(boss_with_mirror.settings, "list_source", {"repo": "boss", "path": "apps"})
    files = [f["file"] for f in out["files"]]
    assert CONSOLE_FILE in files
    assert out["source"] == "self-source mirror"
    assert out["ref"] == boss_with_mirror.head
    assert out["root"].endswith(f"boss-self__{boss_with_mirror.head}")


def test_read_code_and_search_code_find_a_literal_in_a_console_file(boss_with_mirror):
    read = _call(boss_with_mirror.settings, "read_code", {"repo": "boss", "file": CONSOLE_FILE})
    assert read["content"] == CONSOLE_BODY
    assert read["source"] == "self-source mirror" and read["ref"] == boss_with_mirror.head
    found = _call(boss_with_mirror.settings, "search_code",
                  {"repo": "boss", "pattern": "Self-fix tab", "path": "apps/web"})
    assert [(m["file"], m["line"]) for m in found["matches"]] == [(CONSOLE_FILE, 1)]
    assert found["source"] == "self-source mirror"


def test_the_checkout_is_cached_per_commit_and_read_only(boss_with_mirror):
    one = _call(boss_with_mirror.settings, "list_source", {"repo": "boss"})["root"]
    two = _call(boss_with_mirror.settings, "list_source", {"repo": "boss"})["root"]
    assert one == two
    mode = os.stat(os.path.join(one, CONSOLE_FILE)).st_mode
    assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    # nothing in it can push anywhere
    assert git(__import__("pathlib").Path(one), "remote") == ""


def test_a_named_ref_reads_that_commit(boss_with_mirror):
    out = _call(boss_with_mirror.settings, "list_source",
                {"repo": "boss", "path": "", "ref": boss_with_mirror.first})
    assert out["ref"] == boss_with_mirror.first
    assert CONSOLE_FILE not in [f["file"] for f in out["files"]]
    missing = _call(boss_with_mirror.settings, "list_source", {"repo": "boss", "ref": "f" * 40})
    assert missing["ok"] is False and "no commit" in missing["error"]


def test_without_a_mirror_the_running_tree_answers_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(builtin, "_REPO_CACHE", str(tmp_path / "cache"))
    settings = SimpleNamespace(self_fix_source=str(tmp_path / "absent.git"))
    out = _call(settings, "list_source", {"repo": "boss", "path": "engine", "limit": 5})
    assert out["root"] == builtin._SOURCE_ROOT
    assert out["source"] == "running tree"
    assert "mirror" in out["why_not_mirror"]


def test_a_running_commit_the_mirror_lacks_falls_back_to_the_running_tree(
        boss_with_mirror, monkeypatch):
    monkeypatch.setenv("BOSS_STACK_HEAD", "e" * 40)
    out = _call(boss_with_mirror.settings, "list_source", {"repo": "boss", "path": "engine",
                                                           "limit": 5})
    assert out["root"] == builtin._SOURCE_ROOT and out["source"] == "running tree"
    assert "not in the mirror" in out["why_not_mirror"]
