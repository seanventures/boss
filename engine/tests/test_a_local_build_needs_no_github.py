"""A BUILD ON A REPOSITORY ON THIS MACHINE NEEDS NO GITHUB, AND A MISSING GITHUB CREDENTIAL IS
NEVER A 500.

THE CASE (2026-11-18, laptop, after #202): an Easy Mode build, crun_3c87d5a3, failed at once:
"The control plane crashed running this step (HTTP 500): GitHubAppConfigurationError: No GitHub
credentials for _local/lucky". Easy Mode built against a placeholder repository, and the build
asked GitHub for a credential for it. Now:

  - the build's clone credential for a local repository (`local_<project>/<name>`, or the spec's
    repo_host "local") is resolved without GitHub at all;
  - a GitHub repository with no credential anywhere stops the run in words (blocked,
    needs_human, github_not_connected) and answers the worker, never raising into a 500.

No database and no model: the decision and the attempt's own error handling, with the run's
storage stood in.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from boss_engine.services import code_task_service as cts
from boss_engine.services.github_app_adapter import GitHubAppConfigurationError


def _no_github(*_a, **_kw):
    raise AssertionError("a local repository must never ask GitHub for a credential")


@pytest.mark.parametrize("owner,host", [("local_prj_csv", "github"), ("local_prj_csv", "local"),
                                        ("anyone", "local")])
def test_a_local_repository_clones_without_a_github_credential(monkeypatch, owner, host):
    monkeypatch.setattr("boss_engine.services.github_app_adapter.repo_token", _no_github)
    settings = SimpleNamespace(bitbucket_token="", github_token="")
    assert cts._clone_token(object(), settings, owner=owner, repo="csv-quality-desk",  # noqa: SLF001
                            project_id="prj_csv", repo_host_kind=host) == ""


def test_a_github_repository_still_asks_github(monkeypatch):
    seen = []

    def token(settings, owner, repo, *, connection=None, project_id=""):
        seen.append((owner, repo, project_id))
        return "ghs_x"

    monkeypatch.setattr("boss_engine.services.github_app_adapter.repo_token", token)
    monkeypatch.setattr(cts, "_transaction", lambda engine: contextlib.nullcontext(object()))
    settings = SimpleNamespace(bitbucket_token="", github_token="")
    assert cts._clone_token(object(), settings, owner="acme", repo="web",  # noqa: SLF001
                            project_id="prj_1", repo_host_kind="github") == "ghs_x"
    assert seen == [("acme", "web", "prj_1")]


def test_no_github_credential_blocks_the_run_in_words_never_a_500(monkeypatch):
    failed = []

    def crash(*_a, **_kw):
        raise GitHubAppConfigurationError(
            "No GitHub credentials for acme/web: the App has no installation for it, "
            "BOSS_GITHUB_TOKEN is not set, and this project has no repository connected in Setup.")

    def fail(c, run_id, project_id, reason, *, status="failed", failure_class="terminal",
             reason_code="unknown"):
        failed.append({"run_id": run_id, "status": status, "failure_class": failure_class,
                       "reason_code": reason_code, "reason": reason})
        return {"status": status, "done": True, "error": reason}

    monkeypatch.setattr(cts, "_run_code_task", crash)
    monkeypatch.setattr(cts, "_run_project", lambda engine, run_id: None)
    monkeypatch.setattr(cts, "_transaction", lambda engine: contextlib.nullcontext(object()))
    monkeypatch.setattr(cts, "load_code_task_run",
                        lambda c, run_id: {"status": "running", "project_id": "prj_1"})
    monkeypatch.setattr(cts, "_fail", fail)

    out = cts.run_code_task(object(), SimpleNamespace(), None, "crun_gh_missing")

    assert out["status"] == "blocked" and out["done"] is True
    assert [(f["status"], f["failure_class"], f["reason_code"]) for f in failed] == [
        ("blocked", "needs_human", "github_not_connected")]
    assert "Setup › GitHub" in failed[0]["reason"]
