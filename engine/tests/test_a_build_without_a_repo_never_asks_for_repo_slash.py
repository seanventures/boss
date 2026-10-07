"""A build with no repository never asks consent for a repo called "/" (round 3, R3-STACK).

The upgrade walk started a repo-less build as the Owner: the 409 offered github repos ["/"], the
grant without GitHub did not cover it, and the start answered consent_required for good.
"""
from boss_engine.services.code_task_service import _consent_repos


def test_no_owner_or_repo_means_no_github_repo_to_cover():
    assert _consent_repos("", "", None) == []
    assert _consent_repos(None, "app", "github") == []
    assert _consent_repos("acme", None, None) == []


def test_a_github_build_names_its_repo_and_another_host_names_none():
    assert _consent_repos("acme", "app", None) == ["acme/app"]
    assert _consent_repos("acme", "app", "github") == ["acme/app"]
    assert _consent_repos("acme", "app", "bitbucket") == []


def test_both_consent_paths_use_it():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "boss_engine/services/code_task_service.py").read_text()
    assert src.count("repos=_consent_repos(owner, repo, repo_host)") == 2
    assert 'repos=[f"{owner}/{repo}"]' not in src
