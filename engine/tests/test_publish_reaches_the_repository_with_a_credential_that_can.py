"""Publishing checks and pushes with a credential that REACHES the repository (BOSSAPP-1338, AR-41).

FXQuote (local) › Publish to GitHub › Existing empty repo › paramg/bank-fxquote said "There is
no paramg/bank-fxquote on GitHub that BOSS can see" and kept Publish disabled. The check (and the
push after it) used the repository-CREATING credential, a personal token borrowed from another
project, while the BOSS GitHub App's installation could reach the repository.

Every case here runs against a fake GitHub (httpx.MockTransport) and fake credential sources:
app-only access, token-only access, neither, and a new repository.
"""
from __future__ import annotations

import types
from typing import Any

import httpx
import pytest

from boss_engine.services import local_publish, repo_access
from boss_engine.services.repo_access import Candidate

OWNER, REPO = "paramg", "bank-fxquote"


def _github(reaches: dict[str, str]) -> httpx.Client:
    """GET /repos/paramg/bank-fxquote: 200 for a token in `reaches` (value "push" | "read" |
    "app"), 404 otherwise — GitHub hides a private repository from a credential without access."""
    def handle(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if request.url.path != f"/repos/{OWNER}/{REPO}":
            return httpx.Response(404, json={})
        level = reaches.get(token)
        if level is None:
            return httpx.Response(404, json={"message": "Not Found"})
        body: dict[str, Any] = {"full_name": f"{OWNER}/{REPO}", "private": True}
        if level != "app":
            body["permissions"] = {"admin": False, "push": level == "push", "pull": True}
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def _sources(monkeypatch, *, app: str | None, own: str | None, borrowed: str | None) -> None:
    """The credentials BOSS holds: the App's installation, this project's token connection, a
    token borrowed from another project (the dogfood bootstrap)."""
    monkeypatch.setattr(repo_access, "_app_candidates", lambda db, o, r: [
        Candidate("app", f"the boss-localhost App (its installation on {o})",
                  mint=lambda: app, token_kind="app")])
    monkeypatch.setattr(repo_access, "_binding_candidates", lambda *a, **k: [])
    conns = []
    if own is not None:
        conns.append(Candidate("connection", "paramg's access token (this project)",
                               project_id="fxquote", mint=lambda: own))
    if borrowed is not None:
        conns.append(Candidate("connection", "paramg's access token (from project BOSS dogfood)",
                               project_id="boss-dogfood", mint=lambda: borrowed))
    monkeypatch.setattr(repo_access, "_connection_candidates", lambda db, s, p, a=None: conns)
    monkeypatch.setattr(repo_access, "_install_candidates", lambda s: [])


SETTINGS = types.SimpleNamespace(github_token="", github_api_base_url="https://api.github.test")


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    repo_access.reset_probe_cache()
    yield
    repo_access.reset_probe_cache()


def _check(monkeypatch, reaches, *, registry=None):
    monkeypatch.setattr(local_publish, "registry_refusal", lambda c, p, f: registry)
    return local_publish.check_existing(object(), SETTINGS, "fxquote", owner=OWNER, name=REPO,
                                        client=_github(reaches))


def test_app_only_access_checks_and_names_the_app(monkeypatch):
    _sources(monkeypatch, app="app-token", own=None, borrowed="dogfood-pat")
    got = _check(monkeypatch, {"app-token": "app"})
    assert got["ok"] is True, got
    assert "boss-localhost App" in got["via"] and "boss-localhost App" in got["words"]
    got_access = repo_access.resolve(object(), SETTINGS, "fxquote", OWNER, REPO,
                                     client=_github({"app-token": "app"}))
    assert got_access.token == "app-token" and got_access.source == "app"


def test_token_only_access_prefers_this_projects_token(monkeypatch):
    _sources(monkeypatch, app=None, own="own-pat", borrowed="dogfood-pat")
    got = repo_access.resolve(object(), SETTINGS, "fxquote", OWNER, REPO,
                              client=_github({"own-pat": "push", "dogfood-pat": "push"}))
    assert got.ok and got.token == "own-pat" and "this project" in got.label
    # the App gave no token here, and that is said in the trail
    assert got.tried[0]["source"] == "app" and got.tried[0]["outcome"] == "no_token"


def test_a_token_that_can_only_read_is_not_a_push_credential(monkeypatch):
    _sources(monkeypatch, app=None, own="read-pat", borrowed=None)
    got = repo_access.resolve(object(), SETTINGS, "fxquote", OWNER, REPO,
                              client=_github({"read-pat": "read"}))
    assert not got.ok and got.reason == "read_only" and got.exists is True
    assert "may push" in got.words


def test_neither_names_every_credential_and_never_says_it_does_not_exist(monkeypatch):
    _sources(monkeypatch, app=None, own=None, borrowed="dogfood-pat")
    got = _check(monkeypatch, {})
    assert got["ok"] is False and got["reason"] == "not_visible"
    words = got["words"]
    assert "There is no" not in words and "does not exist" not in words
    assert "boss-localhost App" in words and "from project BOSS dogfood" in words
    assert "cannot see it" in words and "Setup › GitHub access" in words
    assert {t["source"] for t in got["tried"]} == {"app", "connection"}


def test_a_switched_off_registry_row_is_said_not_silent(monkeypatch):
    _sources(monkeypatch, app="app-token", own=None, borrowed=None)
    got = _check(monkeypatch, {"app-token": "app"},
                 registry="paramg/bank-fxquote is switched off in Repositories (by ceo): tick it")
    assert got["ok"] is False and got["reason"] == "registry_off" and "Repositories" in got["words"]


class _Row(dict):
    pass


class _Conn:
    """A connection whose registry row is the given one."""

    def __init__(self, row):
        self.row = row

    def begin_nested(self):
        import contextlib

        return contextlib.nullcontext()

    def execute(self, *a, **k):
        row = self.row

        class _R:
            def mappings(self):
                return self

            def first(self):
                return row
        return _R()


def test_a_registry_row_not_yet_allowed_does_not_block_publishing(monkeypatch):
    from boss_engine.multirepo import registry

    monkeypatch.setattr(registry, "org_of_project", lambda c, p: "org_boss")
    full = f"{OWNER}/{REPO}"
    # allowed=false, nobody decided: publishing IS the allow (register_binding at the switch)
    assert local_publish.registry_refusal(
        _Conn(_Row(allowed=False, allowed_by=None, archived=False)), "fxquote", full) is None
    # a person switched it off: said, with the fix
    said = local_publish.registry_refusal(
        _Conn(_Row(allowed=False, allowed_by="ceo", archived=False)), "fxquote", full)
    assert said and "switched off in Repositories" in said


def test_a_new_repository_is_created_with_the_creating_credential_and_pushed_with_the_reaching_one(
        monkeypatch):
    from boss_engine.services import repo_creation

    monkeypatch.setattr(repo_creation, "repo_creation_credential", lambda *a, **k: types.SimpleNamespace(
        token="dogfood-pat", token_kind="user", label="paramg (access token from project BOSS dogfood)",
        words="Will create as …", reason=""))
    monkeypatch.setattr(repo_access, "resolve", lambda *a, **k: repo_access.RepoAccess(
        OWNER, REPO, True, token="app-token", source="app", label="the boss-localhost App"))
    reach = local_publish.default_reach(object(), SETTINGS, "fxquote", owner=OWNER, repo=REPO,
                                        connection_id="ghconn_x")
    assert reach.create_token == "dogfood-pat", "creating uses the creating credential"
    access = reach.access_for(OWNER, REPO)
    assert access.via == "token"
    import base64

    header = " ".join(str(v) for v in access.env.values())
    assert base64.b64encode(b"x-access-token:app-token").decode() in header, \
        "the push uses the credential that reaches the repository"
    assert reach.reach_words_for(OWNER, REPO) == ""


def test_the_build_lookup_is_the_same_resolver_and_stays_bound(monkeypatch):
    """github_token_for: the env token, else this project's binding — nothing borrowed, no
    GitHub call (bound_only)."""
    from boss_engine.services import github_connections

    seen: dict[str, Any] = {}

    def fake(db, settings, project_id, owner, repo, **k):
        seen.update(k)
        return repo_access.RepoAccess(owner, repo, True, token="bound")

    monkeypatch.setattr(repo_access, "resolve", fake)
    assert github_connections.github_token_for(object(), SETTINGS, "fxquote", OWNER, REPO) == "bound"
    assert seen == {"bound_only": True, "verify": False}
    picked = repo_access.candidates(object(), types.SimpleNamespace(github_token="env-tok"),
                                    "fxquote", OWNER, REPO, bound_only=True)
    assert [c.source for c in picked] == ["env", "binding"]


@pytest.mark.parametrize("own,label", [(True, "paramg (access token, this project)"),
                                       (False, "paramg (access token from project BOSS dogfood)")])
def test_an_owner_credential_says_which_project_it_comes_from(monkeypatch, own, label):
    from boss_engine.services import github_connections, repo_creation

    monkeypatch.setattr(repo_creation, "_connection_rows", lambda db, p, a=None: [{
        "own": own, "visible": True,
        "id": "ghconn_1", "project_id": "fxquote" if own else "boss-dogfood",
        "project_name": "FXQuote" if own else "BOSS dogfood", "connection_label": "x",
        "account_login": "paramg", "api_base_url": "https://api.github.test",
        "granted_permissions": [], "missing_permissions": [], "last_test_status": None}])
    monkeypatch.setattr(github_connections, "GitHubConnectionService",
                        lambda s: types.SimpleNamespace(token_for_connection=lambda *a, **k: "pat"))
    monkeypatch.setattr(repo_creation, "pat_abilities", lambda token, **k: {
        "ok": True, "can_create": True, "words": "can create repositories under its own account (repo scope)"})
    repo_creation._ABILITIES.clear()
    got = repo_creation._connection_candidates(object(), SETTINGS, "paramg", "fxquote", probe=True)
    assert got[0].label == label
    if own:
        # this project's unchecked token is tested now, not left at "press Test all"
        assert got[0].can_create is True and "Test all" not in got[0].words
    else:
        # another project's token is never scope-tested on a probe (#179 review)
        assert got[0].can_create is None


# ── #179 review: another project's token is a person's; probes are cached ─────────────────────

class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *a, **k):
        rows = self.rows

        class _R:
            def mappings(self):
                return self

            def all(self):
                return rows
        return _R()


def _row(**k):
    base = {"id": "c", "project_id": "other", "project_name": "Teammate's app", "connection_label": "x",
            "account_login": "teammate", "api_base_url": None, "status": "active",
            "granted_permissions": [], "missing_permissions": [], "last_test_status": None,
            "created_by": "usr_teammate", "shared": False, "actor_member": False}
    return {**base, **k}


def test_another_projects_token_is_used_only_by_its_owner_or_when_shared():
    from boss_engine.services import repo_creation

    rows = [_row(id="own", project_id="fxquote", created_by="usr_teammate"),
            _row(id="private"),                                   # a teammate's, not shared
            _row(id="mine", created_by="usr_me"),                 # the acting person's own
            _row(id="shared", shared=True)]                       # shared with the org
    got = {r["id"]: r for r in repo_creation._connection_rows(_Rows(rows), "fxquote", "usr_me")}
    assert set(got) == {"own", "mine", "shared"}, "a teammate's private token is never borrowed"
    assert got["own"]["visible"] and got["mine"]["visible"]
    assert not got["shared"]["visible"]
    assert repo_creation.token_label(got["shared"]) == "a token from another project"
    assert repo_creation.token_label(got["mine"]) == "teammate (access token from project Teammate's app)"
    member = repo_creation._connection_rows(_Rows([_row(id="shared", shared=True, actor_member=True)]),
                                            "fxquote", "usr_me")
    assert member[0]["visible"], "a member of that project may read whose token it is"


def test_the_check_never_reveals_a_teammates_access(monkeypatch):
    from boss_engine.services import repo_creation

    monkeypatch.setattr(repo_access, "_app_candidates", lambda db, o, r: [])
    monkeypatch.setattr(repo_access, "_binding_candidates", lambda *a, **k: [])
    monkeypatch.setattr(repo_access, "_install_candidates", lambda s: [])
    monkeypatch.setattr(repo_creation, "_connection_rows", lambda db, p, a=None: [
        {**_row(id="shared", shared=True), "own": False, "visible": False}])
    from boss_engine.services import github_connections

    monkeypatch.setattr(github_connections, "GitHubConnectionService",
                        lambda s: types.SimpleNamespace(token_for_connection=lambda *a, **k: "t-shared"))
    got = _check(monkeypatch, {})
    assert "a token from another project" in got["words"]
    assert "teammate" not in got["words"] and "Teammate's app" not in got["words"]
    assert all(t["project_id"] is None for t in got["tried"])


def test_a_probe_is_asked_of_github_once_per_credential_and_repository(monkeypatch):
    _sources(monkeypatch, app="app-token", own=None, borrowed=None)
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"full_name": f"{OWNER}/{REPO}"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    for _ in range(3):
        assert repo_access.resolve(object(), SETTINGS, "fxquote", OWNER, REPO, client=client).ok
    assert len(calls) == 1


def test_the_publish_reach_asks_the_resolver_once(monkeypatch):
    from boss_engine.services import repo_creation

    monkeypatch.setattr(repo_creation, "repo_creation_credential", lambda *a, **k: types.SimpleNamespace(
        token=None, token_kind="app", label="", words="", reason="no_github_credential"))
    asked = []

    def fake(*a, **k):
        asked.append(k.get("actor"))
        return repo_access.RepoAccess(OWNER, REPO, False, words="None of the credentials …")

    monkeypatch.setattr(repo_access, "resolve", fake)
    from boss_engine.services import github_connections

    monkeypatch.setattr(github_connections, "github_token_for", lambda *a, **k: "")
    monkeypatch.setattr(local_publish, "github_token_for", lambda *a, **k: "", raising=False)
    reach = local_publish.default_reach(object(), SETTINGS, "fxquote", owner=OWNER, repo=REPO,
                                        connection_id="c", actor="usr_me")
    assert reach.reach_words_for(OWNER, REPO) == "None of the credentials …"
    reach.access_for(OWNER, REPO)
    reach.reach_words_for(OWNER, REPO)
    assert asked == ["usr_me"], "one resolve per repository, as the acting person"
