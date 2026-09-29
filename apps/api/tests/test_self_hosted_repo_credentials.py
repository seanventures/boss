"""BOSS must be able to work on repositories its GitHub App was never installed on.

The App is installed per-repo by a customer. BOSS's own repository is not one of those
installs — nobody installs an App on the vendor's source tree to use the product. So every
attempt to have BOSS improve itself died at credential minting:

    Failed to mint installation token (422): There is at least one repository that does not
    exist or is not accessible to the parent installation.

...while a Personal Access Token with push rights to that exact repository sat configured and
unused. Three call sites each decided "App if configured, else PAT" independently, and none
of them treated "the App doesn't cover this repo" as anything but fatal.

These tests assert the PROPERTY — a token usable for this repo comes back — not which branch
produced it. An implementation that reorders the preference, caches, or adds a third
credential source should keep them green.
"""

from __future__ import annotations

import pytest

from apps.api.app.config import Settings
from apps.api.app.services.github_app_adapter import (
    GitHubAppConfigurationError,
    repo_token,
)


class _AppInstalledOnOneRepo:
    """Mirrors real behaviour: covered repos mint, everything else 422s."""

    configured = True
    covered = ("customer", "their-app")
    missing_permissions: list[str] = []

    def usable_token_for_repo(self, owner: str, repo: str) -> tuple[str, list[str]]:
        if (owner, repo) != self.covered:
            raise GitHubAppConfigurationError(
                "Failed to mint installation token (422): There is at least one repository "
                "that does not exist or is not accessible to the parent installation."
            )
        return "installation-token", list(self.missing_permissions)


def _settings(**over) -> Settings:
    base = dict(
        BOSS_GITHUB_TOKEN="pat-with-push-to-boss",
        BOSS_GITHUB_APP_ID=None,
        BOSS_GITHUB_APP_PRIVATE_KEY=None,
        BOSS_GITHUB_APP_PRIVATE_KEY_PATH=None,
        BOSS_GITHUB_APP_INSTALLATION_ID=None,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def app_on_one_repo(monkeypatch):
    app = _AppInstalledOnOneRepo()
    monkeypatch.setattr(
        "apps.api.app.services.github_app_adapter.GitHubAppAdapter",
        lambda *a, **k: app,
    )
    return app


def test_boss_can_get_a_token_for_its_own_repo(app_on_one_repo):
    """The regression that blocked self-improvement entirely."""
    assert repo_token(_settings(), "example-org", "boss") == "pat-with-push-to-boss"


def test_covered_repo_still_uses_the_short_lived_installation_token(app_on_one_repo):
    """The fallback must not become the default: App credentials are revocable and scoped."""
    assert repo_token(_settings(), "customer", "their-app") == "installation-token"


def test_no_credentials_at_all_is_still_an_error(app_on_one_repo):
    """Falling back to nothing would turn a config error into a confusing clone failure."""
    with pytest.raises(GitHubAppConfigurationError):
        repo_token(_settings(BOSS_GITHUB_TOKEN=""), "example-org", "boss")


def test_every_credential_call_site_shares_one_decision():
    """Guards the actual root cause: three independent copies that drifted into the same bug.

    Not a style rule. Each duplicated copy is a place the next person fixes the App/PAT
    choice once and misses twice.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for path in (root / "engine" / "boss_engine" / "services").glob("*.py"):
        if path.name == "github_app_adapter.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\.token_for_repo\(", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "These call sites mint installation tokens directly instead of going through "
        "repo_token(), so they will hard-fail on repos outside the App installation:\n  "
        + "\n  ".join(offenders)
    )


def test_a_token_that_cannot_push_is_not_treated_as_success(app_on_one_repo):
    """Observed live on example-user/boss-sandbox.

    The App *is* installed, so minting succeeds — but the installation was never granted
    `contents:write`, and `installation_token` silently retries with the granted set. The
    resulting token reports push=False and fails at git-push time with a bare 403, far from
    the cause. A usable credential exists; it must be preferred.
    """
    app_on_one_repo.missing_permissions = ["contents", "pull_requests"]
    assert repo_token(_settings(), "customer", "their-app") == "pat-with-push-to-boss"


def test_underpermissioned_token_is_still_returned_when_it_is_all_we_have(app_on_one_repo):
    """No PAT configured: a limited token beats no attempt, and GitHub's own error is clearer
    than one BOSS invents about credentials the operator never set."""
    app_on_one_repo.missing_permissions = ["contents"]
    assert (
        repo_token(_settings(BOSS_GITHUB_TOKEN=""), "customer", "their-app")
        == "installation-token"
    )
