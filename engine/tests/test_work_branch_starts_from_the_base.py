"""A new work branch starts from the task's base, not from the default branch.

Asked to add a backend to an existing app, BOSS produced a branch containing `main` plus a
freshly written app: no tsconfig.json, no vite.config.ts, none of the application it was sent
to extend, and a `src/App.tsx` written from nothing. The run reported success and the pull
request looked plausible. It was a different app.

ensure_branch created every new branch from default_branch_sha() regardless of what the task
said its base was. The sandbox checked out the right branch; the commits landed on one that
began at main.

It hid behind the common case. A follow-up that carries its lineage targets a branch that
already exists, so ensure_branch returns early and never reaches the creation path. Only a task
with a fresh root and an explicit base fell through -- exactly the "change this existing app"
case, and the one nobody had been able to ask for.
"""

from __future__ import annotations

import httpx
import pytest

from boss_engine.config import Settings
from boss_engine.services.github_adapter import GitHubAdapter


class _Repo:
    """Minimal GitHub: some branches exist, and creating a ref records its start point."""

    def __init__(self, existing: dict):
        self.existing = dict(existing)
        self.created: list[tuple[str, str]] = []

    def __call__(self, method, path, body=None):
        if method == "GET" and "/git/ref/heads/" in path:
            branch = path.split("/git/ref/heads/", 1)[1]
            if branch not in self.existing:
                raise httpx.HTTPStatusError(
                    "404", request=httpx.Request("GET", "http://x"),
                    response=httpx.Response(404, request=httpx.Request("GET", "http://x")))
            return {"object": {"sha": self.existing[branch]}}
        if method == "POST" and path.endswith("/git/refs"):
            branch = body["ref"].removeprefix("refs/heads/")
            self.created.append((branch, body["sha"]))
            self.existing[branch] = body["sha"]
            return {"object": {"sha": body["sha"]}}
        raise AssertionError(f"unexpected call {method} {path}")


def _adapter(repo: _Repo, monkeypatch) -> GitHubAdapter:
    adapter = GitHubAdapter(
        Settings(BOSS_GITHUB_TOKEN="t", BOSS_TARGET_GITHUB_OWNER="o",
                 BOSS_TARGET_GITHUB_REPO="r"))
    monkeypatch.setattr(adapter, "_request", repo)
    monkeypatch.setattr(adapter, "default_branch_sha", lambda: ("main", "sha-main"))
    return adapter


def test_a_new_branch_starts_from_the_base_it_was_given(monkeypatch):
    """The defect: it started from main, so the app being changed was not there."""
    repo = _Repo({"main": "sha-main", "boss/code/crun_app": "sha-app"})
    adapter = _adapter(repo, monkeypatch)

    adapter.ensure_branch("boss/code/crun_new", "boss/code/crun_app")

    assert repo.created == [("boss/code/crun_new", "sha-app")], \
        "the new branch must begin at the app's code, not at main"


def test_with_no_base_it_still_starts_from_the_default_branch(monkeypatch):
    """New work with no existing code is the other half of the contract."""
    repo = _Repo({"main": "sha-main"})
    adapter = _adapter(repo, monkeypatch)

    adapter.ensure_branch("boss/code/crun_new")

    assert repo.created == [("boss/code/crun_new", "sha-main")]


def test_an_existing_branch_is_returned_untouched(monkeypatch):
    """The case that hid the bug: a lineage follow-up targets a branch that already exists."""
    repo = _Repo({"main": "sha-main", "boss/code/crun_app": "sha-app"})
    adapter = _adapter(repo, monkeypatch)

    assert adapter.ensure_branch("boss/code/crun_app", "main") == "sha-app"
    assert repo.created == [], "an existing branch must never be recreated or moved"


def test_a_missing_base_falls_back_rather_than_failing_the_run(monkeypatch):
    """A bad base is a caller error; losing the run over it helps nobody."""
    repo = _Repo({"main": "sha-main"})
    adapter = _adapter(repo, monkeypatch)

    adapter.ensure_branch("boss/code/crun_new", "boss/code/does-not-exist")

    assert repo.created == [("boss/code/crun_new", "sha-main")]


def test_a_base_equal_to_the_branch_is_not_a_lookup_loop(monkeypatch):
    repo = _Repo({"main": "sha-main"})
    adapter = _adapter(repo, monkeypatch)

    adapter.ensure_branch("boss/code/crun_new", "boss/code/crun_new")

    assert repo.created == [("boss/code/crun_new", "sha-main")]


def test_the_runner_passes_the_base_through():
    """A fixed adapter is useless if the caller never tells it the base."""
    import inspect

    from boss_engine.services import governed_code_task_service as gcts

    source = inspect.getsource(gcts)
    assert "ensure_branch(work_branch, base_branch)" in source
    assert "ensure_branch(work_branch)" not in source
