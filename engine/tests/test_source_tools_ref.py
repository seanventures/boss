"""read_code / search_code with an explicit `ref` (banking case study, 2026-10-24).

Two faults read as one: "repository unavailable in this project" for a repository bound in
ANOTHER project of the org (the agent's conversation was in boss-dogfood), and "no such ref"
for an abbreviated sha — a remote serves only full object names.
"""
from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager

import pytest

from boss_engine.tools import build_default_registry, builtin


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def origin(tmp_path):
    """A repository with two commits; the file differs between them."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "README.md").write_text("first\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    first = _git("rev-parse", "HEAD", cwd=work)
    (work / "README.md").write_text("second\n")
    _git("commit", "-q", "-am", "two", cwd=work)
    bare = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    # A treeless clone needs the server to allow filters.
    _git("config", "uploadpack.allowFilter", "true", cwd=bare)
    return f"file://{bare}", first


def test_an_abbreviated_sha_reads_that_commit(origin, tmp_path):
    url, first = origin
    dest = str(tmp_path / "cache")
    root, err = builtin._abbreviated_sha_root(dest, url, dict(os.environ), first[:12], "o/r")
    assert err is None
    assert open(os.path.join(root, "README.md")).read() == "first\n"
    # The cache is reused: a second read of the same prefix needs no clone.
    root, err = builtin._abbreviated_sha_root(dest, url, dict(os.environ), first[:7], "o/r")
    assert err is None and open(os.path.join(root, "README.md")).read() == "first\n"


def test_an_unknown_sha_says_so(origin, tmp_path):
    url, _ = origin
    root, err = builtin._abbreviated_sha_root(str(tmp_path / "c"), url, dict(os.environ),
                                              "abcdef1234", "o/r")
    assert root is None and "no commit 'abcdef1234' in o/r" in err


def test_repo_root_routes_a_short_sha_and_not_a_branch(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(builtin, "_REPO_CACHE", str(tmp_path))
    monkeypatch.setattr(builtin, "_abbreviated_sha_root",
                        lambda dest, url, env, ref, repo: (seen.append(ref) or dest, None))
    settings = type("S", (), {"target_repo_host": "github", "github_token": ""})()
    builtin._repo_root(settings, "o/r", "63dd10e4150b")
    assert seen == ["63dd10e4150b"]
    assert builtin._ABBREVIATED_SHA.fullmatch("main") is None
    assert builtin._ABBREVIATED_SHA.fullmatch("0" * 40) is None     # a full sha fetches as is


class _Rows:
    def __init__(self, first=None, scalars=()):
        self._first, self._scalars = first, list(scalars)

    def first(self):
        return self._first

    def scalars(self):
        return iter(self._scalars)


class _Conn:
    """A repository bound in project-b only; the session is in project-a (same org)."""

    def execute(self, statement, params=None):
        sql = str(statement)
        if "JOIN workspaces" in sql:
            assert params["p"] == "project-a"
            return _Rows(scalars=["project-b"])
        if "FROM github_repository_bindings" in sql:
            return _Rows(first=("b1",) if params["p"] == "project-b" else None)
        raise AssertionError(sql)


class _Ctx:
    settings = type("S", (), {"boss_edition": "community", "target_github_owner": "o",
                              "github_token": ""})()

    @contextmanager
    def begin(self):
        yield _Conn()


@pytest.mark.parametrize("tool,args", [
    ("read_code", {"repo": "o/bank", "file": "README.md", "ref": "63dd10e4150b"}),
    ("search_code", {"repo": "o/bank", "pattern": "x", "ref": "main"}),
])
def test_a_repository_bound_elsewhere_names_the_project_to_pass(tool, args, monkeypatch):
    monkeypatch.setattr(builtin, "_repo_root", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not clone a repository the project does not have")))
    result = build_default_registry().invoke(tool, _Ctx(), project_id="project-a",
                                             actor="member", args=dict(args))
    assert result["ok"] is False
    assert result["error"] == "repository unavailable in this project"
    assert result["bound_in"] == ["project-b"]
    assert "project='project-b'" in result["do_this"]
