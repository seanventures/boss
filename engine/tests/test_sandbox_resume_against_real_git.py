"""The resume path, exercised against a real git repository.

The unit tests around this stub `_git` and assert on the commands, which proves the decision
but not the commands themselves. A shallow `fetch` of one branch and a `checkout -b <branch>
refs/remotes/origin/<branch>` either work or they do not, and the only code path that runs them
is the one that recovers an interrupted run — the worst possible place to discover a typo.

So this one drives the actual binary against an actual repo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services import sandbox as sandbox_module
from boss_engine.services.sandbox import EphemeralSandbox

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    """A repo with `main`, plus a work branch carrying one checkpointed file."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=repo)
    _run("git", "config", "user.email", "t@example.com", cwd=repo)
    _run("git", "config", "user.name", "t", cwd=repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-q", "-m", "base", cwd=repo)

    _run("git", "checkout", "-q", "-b", "boss/code/crun_1", cwd=repo)
    (repo / "test_thing.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-q", "-m", "checkpoint after write_tests", cwd=repo)
    _run("git", "checkout", "-q", "main", cwd=repo)
    return repo


@pytest.fixture(autouse=True)
def _clone_from_local(origin, monkeypatch):
    monkeypatch.setattr(sandbox_module, "clone_url_for",
                        lambda host, owner, repo, token: str(origin), raising=False)
    monkeypatch.setattr("boss_engine.services.repo_host.clone_url_for",
                        lambda host, owner, repo, token: str(origin))


def _provision(work_branch: str) -> EphemeralSandbox:
    sandbox = EphemeralSandbox(Settings(), task_id="crun_1")
    sandbox.provision("owner", "repo", base_branch="main",
                      work_branch=work_branch, token=None)
    return sandbox


def test_the_checkpointed_file_is_actually_in_the_tree():
    """The whole point. Before this, the run got a clone of `main` with none of its own
    completed work in it and re-derived the suite from nothing."""
    sandbox = _provision("boss/code/crun_1")
    try:
        assert sandbox.resumed_from_checkpoint is True
        assert (sandbox.path / "test_thing.py").exists(), \
            "the checkpointed work was not restored"
        assert (sandbox.path / "README.md").exists(), "the base tree is missing"
    finally:
        sandbox.teardown()


def test_the_branch_is_checked_out_not_merely_fetched():
    sandbox = _provision("boss/code/crun_1")
    try:
        head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                              cwd=sandbox.path, capture_output=True, text=True, check=True)
        assert head.stdout.strip() == "boss/code/crun_1"
    finally:
        sandbox.teardown()


def test_a_resumed_tree_reports_no_pending_diff():
    """`diff()` is `add -A` against HEAD. Resuming on the branch means the restored files are
    already committed, so the next stage produces only its OWN delta — not a re-push of
    everything that came before it."""
    sandbox = _provision("boss/code/crun_1")
    try:
        assert sandbox.diff() == []
    finally:
        sandbox.teardown()


def test_a_new_run_still_branches_from_base():
    """The untouched common case, against real git: a branch that does not exist yet."""
    sandbox = _provision("boss/code/crun_brand_new")
    try:
        assert sandbox.resumed_from_checkpoint is False
        assert not (sandbox.path / "test_thing.py").exists()
        head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                              cwd=sandbox.path, capture_output=True, text=True, check=True)
        assert head.stdout.strip() == "boss/code/crun_brand_new"
    finally:
        sandbox.teardown()


def test_a_resumed_run_can_still_commit_new_work_on_top():
    """Recovery is worthless if the run cannot then continue."""
    sandbox = _provision("boss/code/crun_1")
    try:
        (sandbox.path / "app.py").write_text("print('hi')\n", encoding="utf-8")
        changed = {fc.path for fc in sandbox.diff()}
        assert changed == {"app.py"}, f"expected only the new file, got {changed}"
    finally:
        sandbox.teardown()
