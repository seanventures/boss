"""Repo and models › Pick repos, after "Sign in with GitHub" (identity/github_device.py).

The picker lists what GitHub says THIS person can push to where the BOSS App is installed; binding
one is checked on the server against that same list; and the project's connection is the person's
own sign-in (`identity_github:user:<person>`) — never copied into the org's GitHub token, so no other
person's build is lent it.
"""
import base64
import json

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.setup_helpers import PROJECT, as_ceo, fakes, stub
from boss_engine.db import begin
from boss_engine.identity import github_app, github_device, github_setup, vault
from boss_engine.services import setup_actions

PERSON = "usr_signin_picker"


@pytest.fixture()
def signed_in(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("BOSS_GITHUB_APP_CLIENT_ID", "Iv23-boss-published")
    monkeypatch.setenv("BOSS_GITHUB_APP_SLUG", "boss-hq")
    for name in ("BOSS_GITHUB_API_URL", "BOSS_GITHUB_WEB_URL", "BOSS_GITHUB_APP_ID"):
        monkeypatch.delenv(name, raising=False)
    vault.reset_master_key_cache()
    gh = stub("github_device_fake").DeviceFlowFake()
    gh.fixed_token = "ghp_e2e_full"              # the probes' full-access profile (github_probes)

    def transport(method, url, headers, body):
        got = gh.handle(method, url, headers, body or b"")
        return github_app.Response(got[0], json.dumps(got[1]).encode()) if got else github_app.Response(404, b"{}")

    monkeypatch.setattr(github_app, "TRANSPORT", transport)
    with begin() as db:
        vault.check_master_key(db=db)
        org = db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                              "WHERE p.id = :p"), {"p": PROJECT}).scalar()
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                        "(:i, :o, 'user', 'active', 'Picker') ON CONFLICT (id) DO UPDATE SET status = 'active'"),
                   {"i": PERSON, "o": org})
        db.execute(text("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'legacy_pat' "
                        "AND owner_principal_id IS NULL"))
    started = github_device.start(PERSON)
    gh.approve(started["user_code"])
    with begin() as db:
        assert github_device.poll(person_id=PERSON, org_id=org, handle=started["handle"], db=db)["state"] == "connected"
    yield gh
    vault.reset_master_key_cache()


def test_the_picker_offers_only_this_persons_pushable_repos_and_names_installs_needed(signed_in):
    with begin() as db:
        found = setup_actions.repository_from_signin(db, api_main.settings, PROJECT, person_id=PERSON)
        ref = db.execute(text("SELECT credential_ref FROM github_connections WHERE id = :c"),
                         {"c": found["connection_id"]}).scalar()
    assert [r["full_name"] for r in found["repos"]] == ["acme/web"]
    assert found["account_login"] == "octo-dev" and found["source"] == "github_signin"
    assert [n["account"] for n in found["install_needed"]] == ["octo-dev", "globex"]
    assert ref == f"identity_github:user:{PERSON}"
    assert "ghp_" not in json.dumps(found)


def test_binding_a_repo_outside_the_persons_reach_is_refused_on_the_server(signed_in):
    with begin() as db:
        found = setup_actions.repository_from_signin(db, api_main.settings, PROJECT, person_id=PERSON)
        for repo in ("acme/secret-infra", "acme/api", "someone-else/dotfiles"):
            with pytest.raises(setup_actions.SetupActionError) as e:
                setup_actions.select_repository(db, api_main.settings, PROJECT, found["connection_id"], repo)
            assert e.value.detail["state"] == "repo_not_reachable", repo


def test_a_picked_repo_uses_the_persons_sign_in_and_is_never_the_orgs_token(signed_in):
    with begin() as db:
        found = setup_actions.repository_from_signin(db, api_main.settings, PROJECT, person_id=PERSON)
        binding = setup_actions.select_repository(db, api_main.settings, PROJECT, found["connection_id"],
                                                  "acme/web", make_default=False)
        checks = db.execute(text("SELECT checks FROM github_repository_bindings WHERE id = :b"),
                            {"b": binding}).scalar()
        assert github_setup.org_token(db) is None              # not mirrored: nobody else is lent it
        from boss_engine.services import setup_view

        builds = setup_view.build(db, api_main.settings, PROJECT)["repository"]["connection"]["builds"]
        assert builds["ok"] is False and "as me" in builds["message"]   # said plainly, not "paste a token"
    assert checks["read"]["ok"] is True and checks["write"]["ok"] is True


def test_only_a_signed_in_person_can_connect_their_own_github(signed_in):
    with begin() as db, pytest.raises(setup_actions.SetupActionError) as e:
        setup_actions.repository_from_signin(db, api_main.settings, PROJECT, person_id="ceo")
    assert e.value.detail["state"] == "people_only"
    with begin() as db, pytest.raises(setup_actions.SetupActionError) as e:
        setup_actions.repository_from_signin(db, api_main.settings, PROJECT, person_id="usr_never_signed_in")
    assert e.value.detail["state"] == "not_signed_in"
