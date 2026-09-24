"""A run works on the repository its task named, not on a global default.

BOSS was asked to fix a bug in its own source (`example-org/boss`). The run resolved
`spec["owner"]/spec["repo"]` correctly, minted a token scoped to that repository, and then
cloned, branched and opened its pull request against `BOSS_TARGET_GITHUB_*` instead --
because SoftwareFactoryService built `GitHubAdapter(settings)` with no owner or repo.

The failure was silent and convincing. The PR opened, the pipeline reported `completed`, and
the diff showed +16,927 lines "added" -- BOSS's own source copied wholesale into a repository
that had never contained it. Nothing anywhere said "wrong repository".

This pins the property: whatever repo the run names is the repo the work happens in.
"""

from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services.software_factory import SoftwareFactoryService


def _settings() -> Settings:
    return Settings(
        BOSS_TARGET_GITHUB_OWNER="default-owner",
        BOSS_TARGET_GITHUB_REPO="default-repo",
        BOSS_GITHUB_TOKEN="t",
    )


def test_a_run_that_names_a_repo_uses_that_repo():
    sf = SoftwareFactoryService(_settings(), owner="example-org", repo="boss")
    assert sf.github.repository_full_name == "example-org/boss"


def test_a_run_that_names_nothing_still_uses_the_configured_default():
    sf = SoftwareFactoryService(_settings())
    assert sf.github.repository_full_name == "default-owner/default-repo"


@pytest.mark.parametrize("owner,repo", [("", ""), (None, None)])
def test_blank_targets_fall_back_rather_than_producing_a_broken_slug(owner, repo):
    """An empty override must not yield "/" -- that would fail far from its cause."""
    sf = SoftwareFactoryService(_settings(), owner=owner, repo=repo)
    assert sf.github.repository_full_name == "default-owner/default-repo"


def test_the_pipeline_adapter_shares_the_same_repository():
    """The PR and the CI/checks calls must agree; two adapters drifting is the original bug."""
    sf = SoftwareFactoryService(_settings(), owner="example-org", repo="boss")
    inner = getattr(sf.pipeline, "github", None)
    if inner is not None:
        assert inner.repository_full_name == "example-org/boss"
