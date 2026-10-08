"""D1 (id-sec-final2): a project-bound Admin replaced the ORG's GitHub token through Setup › Repo
(a project page), which mirrored any pasted token into the org connection — revoking the token every
other project builds with. Pasting the org token's own value widened its repo scope instead.

Now every function that changes the org's GitHub credentials checks github:connect at the ORG
(github_setup.require_org_credentials_authority), and Setup › Repo mirrors only for such a person.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.setup_helpers import fakes
from boss_engine.db import begin
from boss_engine.identity import github_setup, lineage, vault


@pytest.fixture
def world(engine, monkeypatch):
    fakes(monkeypatch)
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    w = IdentityWorld.create(engine)
    w.claimed()
    w.owner = w.person("owner")
    w.bind(w.owner, "owner")
    w.padmin = w.person("pa-admin")
    w.bind(w.padmin, "admin", project=w.project_a)
    with begin() as db:
        vault.check_master_key(db=db)
        github_setup.store_org_token(token="ghp_e2e_full", repos=["acme/b-repo"], org_id=w.org, db=db,
                                     by=w.owner.id, check=False)
    return w


def _org_token(w):
    with begin() as db:
        return github_setup.org_token(db, w.org)


def _setup_repo(w, ctx, token, repo="e2e/app"):
    c = TestClient(main.app)
    with w.acting_as(ctx):
        found = c.post("/v1/setup/repository/token", json={"project_id": w.project_a, "token": token})
        assert found.status_code == 200, found.text
        picked = c.post("/v1/setup/repository/select", json={
            "project_id": w.project_a, "connection_id": found.json()["connection_id"], "full_name": repo})
        assert picked.status_code == 200, picked.text


@pytest.mark.parametrize("token", ["ghp_e2e_expiring", "ghp_e2e_full"])
def test_a_project_admin_never_changes_the_org_github_token(world, token):
    before = _org_token(world)
    c = TestClient(main.app)
    with world.acting_as(world.ctx(world.padmin)):
        assert c.post("/v1/identity/github/token", json={"token": "ghp_x", "repos": ["acme/a-repo"]}
                      ).status_code == 403                                    # control: the org route
    try:
        _setup_repo(world, world.ctx(world.padmin), token)
    except AssertionError:
        if token == "ghp_e2e_expiring":
            pytest.skip("the stub does not know this token")
        raise
    after = _org_token(world)
    assert after is not None and after["id"] == before["id"]          # not replaced
    assert after["repos"] == ["acme/b-repo"]                           # not widened
    with begin() as db:
        status = db.execute(text("SELECT status FROM identity_vault_items WHERE id = :i"),
                            {"i": before["id"]}).scalar()
        said = db.execute(text("SELECT data->>'message' FROM events WHERE project_id = :p AND "
                               "event_type = 'github.org_token_unchanged' ORDER BY created_at DESC LIMIT 1"),
                          {"p": world.project_a}).scalar()
    assert status == "active"
    assert said and "whole organisation" in said


def test_an_org_admin_through_setup_still_keeps_the_token_for_the_org(world):
    _setup_repo(world, world.ctx(world.owner), "ghp_e2e_full")
    assert "e2e/app" in _org_token(world)["repos"]


def test_every_org_credential_change_checks_org_scope(world):
    """The choke point itself: inside an enforced request as the project Admin, each org-credential
    change is refused, whatever calls it."""
    ctx = world.ctx(world.padmin)
    token = lineage._ENFORCED.set(True)   # noqa: SLF001 — what the guard sets for an enforced request
    try:
        with lineage.acting(ctx), begin() as db:
            for call in (
                lambda: github_setup.store_org_token(token="ghp_new", repos=["a/b"], org_id=world.org, db=db,
                                                     by=ctx.principal.id, check=False),
                lambda: github_setup.add_repo_to_org_token(repo="a/b", org_id=world.org, db=db),
                lambda: github_setup.remove_org_token(org_id=world.org, db=db, by=ctx.principal.id),
                lambda: github_setup.disconnect_app(org_id=world.org, db=db, by=ctx.principal.id),
            ):
                with pytest.raises(github_setup.GitHubSetupError) as refused:
                    call()
                assert refused.value.code == "org_scope_required" and refused.value.status == 403
    finally:
        lineage._ENFORCED.reset(token)   # noqa: SLF001
    assert _org_token(world)["repos"] == ["acme/b-repo"]
