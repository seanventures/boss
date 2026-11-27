"""BOSSAPP-1160: ONE credential resolver for making GitHub repositories.

Add project › New GitHub repo said "no GitHub credential" on an install holding a Setup-registered
App (installed on @paramg) and two token connections (@paramg, @example-org-b): default_reach
read only the environment App and BOSS_GITHUB_TOKEN. The resolver tries, in order, the
Identity App's installation on the owner (an organisation that accepted administration: write),
the environment App, the connections' tokens for their own account, the install token — and says
which one will create, or exactly which permission each one lacks.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import repo_creation
from boss_engine.services.repo_creation import Candidate, RepoCreationCredential
from boss_engine.services.setup_checks import HttpAnswer

from ._multirepo_support import make_engine, make_world


# ---- what a token can do, from GitHub's own header ---------------------------------------------------------

def _http(status=200, scopes=None, body=None):
    def http(method, url, *, headers, json_body=None, timeout=15.0):
        assert "Authorization" in headers and url.endswith("/user")
        hdrs = {} if scopes is None else {"x-oauth-scopes": scopes}
        return HttpAnswer(status, hdrs, body if body is not None else {"login": "paramg"})
    return http


def test_a_classic_token_with_the_repo_scope_creates_repositories():
    got = repo_creation.pat_abilities("ghp_x", http=_http(scopes="repo, read:org"))
    assert got["ok"] and got["can_create"] is True and got["login"] == "paramg"
    assert "administration" in got["granted"] and got["missing"] == []


def test_a_classic_token_without_the_repo_scope_names_the_missing_permission():
    got = repo_creation.pat_abilities("ghp_x", http=_http(scopes="read:user, gist"))
    assert got["can_create"] is False and got["missing"] == ["administration", "contents", "pull_requests"]
    assert "missing: administration" in got["words"]


def test_a_fine_grained_token_is_unverified_until_the_attempt():
    got = repo_creation.pat_abilities("github_pat_x", http=_http(scopes=None))
    assert got["ok"] and got["can_create"] is None and got["granted"] == [] and got["missing"] == []
    assert "Administration" in got["words"]
    rejected = repo_creation.pat_abilities("ghp_x", http=_http(status=401, body={}))
    assert not rejected["ok"] and rejected["can_create"] is None


def test_scopes_map_to_flow_permissions():
    assert repo_creation.scopes_to_permissions("repo") == {"contents": "write", "pull_requests": "write",
                                                           "administration": "write"}
    assert repo_creation.scopes_to_permissions("public_repo")["administration"] == "write"
    assert repo_creation.scopes_to_permissions("") == {}
    assert repo_creation.scopes_to_permissions(None) == {}


# ---- the order, and the reasons -------------------------------------------------------------------------------

def _wire(monkeypatch, *, app=(), env=(), conns=(), install=()):
    monkeypatch.setattr(repo_creation, "_identity_app_candidates", lambda db, owner: list(app))
    monkeypatch.setattr(repo_creation, "_env_app_candidates", lambda settings, owner, owner_type="": list(env))
    monkeypatch.setattr(repo_creation, "_connection_candidates",
                        lambda db, settings, owner, project_id, owner_type="", probe=True, actor=None: list(conns))
    monkeypatch.setattr(repo_creation, "_install_candidates", lambda settings: list(install))


def _app(can, **kw):
    return Candidate("identity_app", "boss-localhost App", "app", can, mint=lambda: "ghs_app", **kw)


def _pat(can, login="paramg", token="ghp_pat", **kw):
    return Candidate("connection", f"{login} (access token, Acme Shop)", "user", can, account_login=login,
                     mint=(lambda: token), **kw)


def test_the_identity_app_wins_when_its_installation_holds_administration(monkeypatch):
    _wire(monkeypatch, app=[_app(True, words="the installation accepted Administration: Read and write")],
          conns=[_pat(True)], install=[Candidate("install", "BOSS_GITHUB_TOKEN", "user", None, mint=lambda: "ghp_env")])
    got = repo_creation.repo_creation_credential(None, get_settings(), "acme")
    assert got.ok and got.kind == "identity_app" and got.token == "ghs_app" and got.token_kind == "app"
    assert got.verified and got.words.startswith("Will create as boss-localhost App")
    assert "ghs_app" not in str(got.as_dict())        # never a token in words or summaries


def test_a_personal_account_falls_through_to_the_owners_own_token(monkeypatch):
    """The App is installed on @paramg (a User): no App creates there. The connection whose
    account IS paramg does — the laptop install's case."""
    _wire(monkeypatch, app=[_app(False, account_login="paramg", account_type="User",
                                words="a GitHub App cannot create repositories in a personal account",
                                fix="Add a personal access token with the repo scope for paramg")],
          conns=[_pat(True)])
    got = repo_creation.repo_creation_credential(None, get_settings(), "paramg")
    assert got.ok and got.kind == "connection" and got.token == "ghp_pat" and got.token_kind == "user"
    assert "paramg (access token, Acme Shop)" in got.words


def test_a_confirmed_credential_beats_an_unverified_one_whatever_the_order(monkeypatch):
    _wire(monkeypatch, env=[Candidate("env_app", "GitHub App (server settings)", "app", None, mint=lambda: "ghs_env")],
          conns=[_pat(True)])
    got = repo_creation.repo_creation_credential(None, get_settings(), "paramg")
    assert got.kind == "connection" and got.verified
    _wire(monkeypatch, env=[Candidate("env_app", "GitHub App (server settings)", "app", None, mint=lambda: "ghs_env")],
          conns=[_pat(None)])
    got = repo_creation.repo_creation_credential(None, get_settings(), "paramg")
    assert got.ok and got.kind == "env_app" and not got.verified        # unverified: the first in order


def test_a_refused_mint_moves_to_the_next_candidate(monkeypatch):
    _wire(monkeypatch, app=[Candidate("identity_app", "boss-localhost App", "app", True, mint=lambda: None)],
          conns=[_pat(True)])
    got = repo_creation.repo_creation_credential(None, get_settings(), "acme")
    assert got.ok and got.kind == "connection"
    picked = repo_creation.repo_creation_credential(None, get_settings(), "acme", mint=False)
    assert picked.ok and picked.kind == "identity_app" and picked.token is None    # mint=False: decided, not minted


def test_when_none_can_the_reason_names_each_missing_permission_and_fix(monkeypatch):
    _wire(monkeypatch,
          app=[_app(False, account_login="acme", account_type="Organization", missing=["administration"],
                    words="the installation lacks administration: write",
                    fix="Grant Administration: Read and write to the boss-localhost App")],
          conns=[_pat(False, missing=["administration"], words="its token lacks the repo scope (missing: administration)",
                      fix="Replace the token for paramg with one holding the repo scope (Setup)")])
    got = repo_creation.repo_creation_credential(None, get_settings(), "acme")
    assert not got.ok and got.token is None and got.reason == "no_github_credential"
    assert "no GitHub credential" in got.words and "Administration" in got.words
    assert got.fixes == ["Grant Administration: Read and write to the boss-localhost App",
                         "Replace the token for paramg with one holding the repo scope (Setup)"]
    assert [c["kind"] for c in got.candidates] == ["identity_app", "connection"]


def test_no_credential_at_all_says_both_ways_to_connect_one(monkeypatch):
    _wire(monkeypatch)
    got = repo_creation.repo_creation_credential(None, get_settings(), "paramg")
    assert not got.ok and "no GitHub credential" in got.words and "Administration: Read and write" in got.words
    assert "repo scope for paramg" in got.words and got.fixes == []


# ---- the App's flows (identity/github_setup) ----------------------------------------------------------------------

def test_flow_abilities_say_what_the_installation_can_do():
    from boss_engine.identity import github_setup

    links = {"permissions": "https://github.test/settings/apps/boss/permissions"}
    org = github_setup.flow_abilities({"contents": "write", "pull_requests": "write"}, account_type="Organization",
                                      links=links, account_login="acme")
    assert org["build"]["ok"] is True
    assert org["create_repository"]["ok"] is False and org["create_repository"]["missing"] == ["administration"]
    assert "Can create repos: no (missing: administration)" in org["create_repository"]["words"]
    assert org["create_repository"]["fix_url"] == links["permissions"]
    granted = github_setup.flow_abilities({"contents": "write", "pull_requests": "write", "administration": "write"},
                                          account_type="Organization", links=links)
    assert granted["create_repository"] == {"ok": True, "missing": [], "words": "Can create repos: yes.", "fix_url": None, "reason": ""}
    person = github_setup.flow_abilities({"contents": "write", "pull_requests": "write", "administration": "write"},
                                         account_type="User", links=links, account_login="paramg")
    assert person["create_repository"]["ok"] is False and person["create_repository"]["reason"] == "personal_account"
    assert person["create_repository"]["missing"] == [] and "not for personal accounts" in person["create_repository"]["words"]
    assert "repo scope for paramg" in person["create_repository"]["words"]
    unknown = github_setup.flow_abilities({}, account_type="Organization", links=links, known=False)
    assert unknown["create_repository"]["ok"] is None


def test_the_app_pill_is_amber_when_it_cannot_create_repos():
    from boss_engine.identity.github_setup import _verdict

    cannot = {"state": "ok", "ok": True, "flows": {"create_repository": {
        "ok": False, "missing": ["administration"], "words": "Can create repos: no (missing: administration). Grant …"}}}
    pill = _verdict("app", "paramg", False, cannot)
    assert pill["tone"] == "amber" and "can't create repos" in pill["label"] and "administration" in pill["tip"]
    can = {"state": "ok", "ok": True, "flows": {"create_repository": {"ok": True, "missing": [], "words": ""}}}
    assert _verdict("app", "acme", False, can)["tone"] == "green"
    # A personal account: GitHub's rule, nothing to grant — green, said for information only.
    person = {"state": "ok", "ok": True, "flows": {"create_repository": {
        "ok": False, "missing": [], "reason": "personal_account", "words": "Creates repos: not for personal accounts"}}}
    assert _verdict("app", "paramg", False, person)["tone"] == "green"
    assert _verdict("app", "acme", False, {"state": "ok", "ok": True})["tone"] == "green"   # not checked: as before


# ---- on the database: connections, owners, default_reach ----------------------------------------------------------

def test_a_connection_for_the_owner_is_found_with_its_recorded_scopes(monkeypatch):
    engine = make_engine()
    world = make_world(engine)
    pid, other = world["projects"]
    cid, cid2 = world["connections"][pid], world["connections"][other]
    settings = get_settings()
    with engine.begin() as c:
        c.execute(text("UPDATE github_connections SET account_login = 'paramg', granted_permissions = "
                       "'[\"pull\",\"push\",\"administration\",\"contents\",\"pull_requests\"]'::jsonb WHERE id = :c"), {"c": cid})
        c.execute(text("UPDATE github_connections SET account_login = 'example-org-b', missing_permissions = "
                       "'[\"administration\"]'::jsonb, created_by = 'usr_example_b' WHERE id = :c"), {"c": cid2})
    from boss_engine.services.github_connections import GitHubConnectionService

    monkeypatch.setattr(GitHubConnectionService, "token_for_connection",
                        lambda self, db, *, project_id, connection_id: f"ghp_for_{connection_id}")
    monkeypatch.setattr(repo_creation, "_identity_app_candidates", lambda db, owner: [])
    monkeypatch.setattr(repo_creation, "_env_app_candidates", lambda settings: [])
    with engine.begin() as c:
        got = repo_creation.repo_creation_credential(c, settings, "paramg", project_id=pid)
        assert got.ok and got.kind == "connection" and got.token == f"ghp_for_{cid}" and got.verified
        # this project's own token says so (BOSSAPP-1338: which project a credential is from)
        assert "paramg (access token, this project)" in got.words
        # ANOTHER PROJECT'S TOKEN IS ITS OWNER'S (#179 review): not considered for anyone else …
        stranger = repo_creation.repo_creation_credential(c, settings, "example-org-b", project_id=pid,
                                                          actor="usr_someone")
        assert not stranger.ok and not stranger.candidates and "example-org-b (access" not in stranger.words
        # … and used for the person who made it.
        no = repo_creation.repo_creation_credential(c, settings, "example-org-b", project_id=pid,
                                                    actor="usr_example_b")
        assert not no.ok and "missing: administration" in no.words and no.fixes
        assert "example-org-b" in no.fixes[0] and "repo scope" in no.fixes[0]
        # Case does not matter for a GitHub login; a stranger gets the no-credential words.
        assert repo_creation.repo_creation_credential(c, settings, "PARAMG", project_id=pid).ok
        owners = repo_creation.creating_owners(c, settings, pid, ["someone-else"], actor="usr_example_b")
    by = {o["login"]: o for o in owners}
    assert by["paramg"]["can_create"] and by["paramg"]["via"] == "paramg (access token, this project)" and by["paramg"]["verified"]
    assert not by["example-org-b"]["can_create"] and by["example-org-b"]["fixes"]
    assert not by["someone-else"]["can_create"] and "no GitHub credential" in by["someone-else"]["words"]


def test_default_reach_creates_with_the_resolved_credential(monkeypatch):
    from boss_engine.services import local_publish

    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    monkeypatch.setattr(repo_creation, "repo_creation_credential",
                        lambda db, settings, owner, project_id=None, mint=True, owner_type="", actor=None: RepoCreationCredential(
                            owner, True, token="ghp_pat", token_kind="user", kind="connection",
                            label="paramg (access token, Acme Shop)", verified=True,
                            words="Will create as paramg (access token, Acme Shop): its token has the repo scope."))
    with engine.begin() as c:
        reach = local_publish.default_reach(c, get_settings(), pid, owner="paramg", repo="shop")
    assert reach.create_token == "ghp_pat" and reach.create_token_kind == "user"
    assert reach.create_label == "paramg (access token, Acme Shop)" and reach.create_reason == ""
    monkeypatch.setattr(repo_creation, "repo_creation_credential",
                        lambda db, settings, owner, project_id=None, mint=True, owner_type="", actor=None: RepoCreationCredential(
                            owner, False, reason="no_github_credential",
                            words="BOSS has no GitHub credential that can create repositories in paramg. "
                                  "boss-localhost App: a GitHub App cannot create repositories in a personal account. "
                                  "Add a personal access token with the repo scope for paramg.",
                            fixes=["Add a personal access token with the repo scope for paramg"]))
    with engine.begin() as c:
        reach = local_publish.default_reach(c, get_settings(), pid, owner="paramg", repo="shop")
    assert reach.create_token is None and reach.create_reason == "no_github_credential"
    said = local_publish.check_repository(owner="paramg", name="shop", token=None, credential_words=reach.create_words)
    assert said["reason"] == "no_github_credential" and "personal access token" in said["words"]
    assert "no GitHub credential" in said["words"]
    # Without the resolver's words the old sentence stands (an older caller).
    assert "Administration" in local_publish.check_repository(owner="paramg", name="shop", token=None)["words"]


# ---- the review's cases (PR #108) ------------------------------------------------------------------------------

def test_the_environment_app_creates_only_in_its_own_organisation(monkeypatch):
    from boss_engine.services import github_app_adapter

    monkeypatch.setattr(github_app_adapter.GitHubAppAdapter, "configured", property(lambda self: True))
    account = {"login": "acme", "type": "Organization", "permissions": {"administration": "write", "contents": "write"}}
    monkeypatch.setattr(github_app_adapter.GitHubAppAdapter, "installation_account", lambda self, installation_id=None: account)
    settings = get_settings()
    assert repo_creation._env_app_candidates(settings, "paramg", "User") == []          # a person: never
    [other] = repo_creation._env_app_candidates(settings, "paramg")
    assert other.can_create is False and "not on paramg" in other.words                   # another account: never
    [own] = repo_creation._env_app_candidates(settings, "ACME")
    assert own.can_create is True and own.account_type == "Organization"
    account["permissions"] = {"contents": "write"}
    [lacks] = repo_creation._env_app_candidates(settings, "acme")
    assert lacks.can_create is False and lacks.missing == ["administration"] and "Administration" in lacks.fix
    account.update(login="paramg", type="User")
    [person] = repo_creation._env_app_candidates(settings, "paramg")
    assert person.can_create is False and "personal account" in person.words
    monkeypatch.setattr(github_app_adapter.GitHubAppAdapter, "installation_account", lambda self, installation_id=None: None)
    [unknown] = repo_creation._env_app_candidates(settings, "acme")
    assert unknown.can_create is None                                                     # GitHub did not say


def test_the_owner_picker_reads_no_secret_and_asks_no_membership(monkeypatch):
    """mint=False (GET …/owners): an organisation owner's candidates from other accounts' tokens are
    listed as unverified, without resolving a secret or calling GitHub."""
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    cid = world["connections"][pid]
    with engine.begin() as c:
        c.execute(text("UPDATE github_connections SET account_login = 'paramg', granted_permissions = "
                       "'[\"administration\",\"contents\",\"pull_requests\"]'::jsonb WHERE id = :c"), {"c": cid})
    from boss_engine.services.github_connections import GitHubConnectionService

    reads, asks = [], []
    monkeypatch.setattr(GitHubConnectionService, "token_for_connection",
                        lambda self, db, *, project_id, connection_id: reads.append(connection_id) or "ghp_x")
    monkeypatch.setattr(repo_creation, "_org_member", lambda token, owner, api_base: asks.append(owner) or True)
    monkeypatch.setattr(repo_creation, "_identity_app_candidates", lambda db, owner: [])
    monkeypatch.setattr(repo_creation, "_env_app_candidates", lambda settings, owner, owner_type="": [])
    with engine.begin() as c:
        picked = repo_creation.repo_creation_credential(c, get_settings(), "acme", project_id=pid, mint=False,
                                                        owner_type="Organization")
        assert picked.ok and not picked.verified and "checked when you press Create" in picked.words
        assert reads == [] and asks == []
        made = repo_creation.repo_creation_credential(c, get_settings(), "acme", project_id=pid, owner_type="Organization")
        assert made.ok and made.token == "ghp_x" and not made.verified       # an org may forbid personal tokens
        assert "member of acme" in made.words and asks == ["acme"] and reads
        mine = repo_creation.repo_creation_credential(c, get_settings(), "paramg", project_id=pid, mint=False)
        assert mine.ok and mine.verified                                       # its own account: confirmed by scopes


def test_an_installation_whose_account_kind_is_unknown_is_unverified(monkeypatch):
    from boss_engine.identity import github_setup

    monkeypatch.setattr(github_setup, "app_config", lambda db, org_id=None: {
        "source": "setup", "app_id": "7", "slug": "boss-localhost", "installation_id": "4242",
        "account_login": "acme", "account_type": None, "owner_login": "acme", "owner_type": None, "verified": None})
    monkeypatch.setattr(github_setup, "recorded_installations", lambda db, org_id=None: [])
    monkeypatch.setattr(github_setup, "links", lambda cfg: {"permissions": None})
    monkeypatch.setattr(github_setup, "installation_permissions", lambda db, inst: {"administration": "write"})
    [cand] = repo_creation._identity_app_candidates(None, "acme")
    assert cand.can_create is None and "not recorded yet" in cand.words
