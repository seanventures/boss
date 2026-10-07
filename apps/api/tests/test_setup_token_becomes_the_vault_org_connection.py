"""A token pasted in Setup › Repo becomes the vault's org GitHub connection, scoped to the repos picked
for it — the only way a build can use it under enforce (boss-egress reads it; the sandbox never does)."""
import base64

import pytest

from sqlalchemy import text

from apps.api.tests.setup_helpers import as_ceo, connect_repository, fakes
from boss_engine.db import begin
from boss_engine.identity import github_setup, vault


@pytest.fixture(autouse=True)
def _forget_the_test_key():
    yield
    vault.reset_master_key_cache()


def test_a_pasted_setup_token_is_the_org_connection_for_the_picked_repos(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())   # the identity suites' test key
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    with begin() as db:
        vault.check_master_key(db=db)
        db.execute(text("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'legacy_pat' "
                        "AND owner_principal_id IS NULL"))
    connect_repository(client, token="ghp_e2e_full", full_name="e2e/app")
    with begin() as db:
        org = github_setup.org_token(db)
        assert org is not None and org["repos"] == ["e2e/app"]
        assert vault.org_github_token(org["id"], db=db) == "ghp_e2e_full"
        assert github_setup.mode(db)["mode"] in ("token", "app")
    connect_repository(client, token="ghp_e2e_full", full_name="e2e/other")   # same token, another repo
    with begin() as db:
        again = github_setup.org_token(db)
        assert again["id"] == org["id"] and again["repos"] == ["e2e/app", "e2e/other"]
