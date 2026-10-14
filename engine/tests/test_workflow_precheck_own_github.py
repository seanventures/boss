"""BOSSAPP-547 · the pre-check checks the starting person's OWN GitHub link, as the start door does.

Starting HelpDesk was refused "Your own GitHub account can't push to <owner>/boss-sandbox ...
Connect your GitHub in My account › Agents" while the pre-check said github.access ok: that check
reads BOSS's repository binding, not the consenting person's link. github.own_link asks the start
door's own question (consent.own_github_refusal → grants.user_repo_access_refusal), so the two
cannot disagree.

The verdict tests need no database (the check reads through `Context.memo`). The agreement tests
are DB-backed (BOSS_IDENTITY_TEST_DATABASE_URL) and drive the real grant the consent sheet posts.
"""
from __future__ import annotations

from typing import Any

import pytest

from boss_engine.identity import consent, github_app, grants, vault
from boss_engine.services import workflow_precheck as wp

REPO = "acme/boss-sandbox"
FIX = "Connect your GitHub in My account › Agents"


def _ctx(answer: Any = None, *, person: Any = ("org_boss", "usr_alice"),
         repos: tuple[str, ...] = (REPO,), offline: bool = False) -> wp.Context:
    ctx = wp.Context(connection=None, settings=object(), project_id="p1", actor="ceo",
                     spec={"steps": [{"id": "build", "kind": "capability",
                                      "binding": {"capability": "build"}}],
                           "entry_step": "build"},
                     run_input={}, offline=offline)
    ctx.memo.update({"consent": {"roles": ["forge"], "permissions": ["code_task:create"],
                                 "repos": list(repos)},
                     "starting_person": person})
    if answer is not None:
        ctx.memo["own_github"] = answer
    return ctx


def _check(ctx: wp.Context) -> Any:
    return wp.CHECKS["github.own_link"].fn(ctx, ctx.spec["steps"][0])


# ── the verdicts ──────────────────────────────────────────────────────────────────────────────

def test_every_build_checks_the_starting_persons_own_github_at_the_door():
    for cap in ("build", "retry_build", "plrr", "more_tests", "security_fix"):
        assert "github.own_link" in wp.precheck_for(cap).checks
    definition = wp.CHECKS["github.own_link"]
    assert definition.fast and definition.shared and wp.PRE_RUN in definition.checkpoints
    assert "github.own_link" in wp.CHECK_DOCS


def test_a_person_with_no_github_linked_is_told_to_connect_it_as_the_consent_says():
    found = _check(_ctx({"state": "refused", "repo": REPO, "needs": "push", "answer": False,
                         "linked": False}))
    assert found["verdict"] == "fixable"
    assert found["evidence"].startswith(f"Your own GitHub account can't push to {REPO}")
    assert "github_access_lacking" in found["evidence"]
    assert found["fix"]["action"] == f"{FIX}, then try again."
    assert grants.GITHUB_LINK_FIX == FIX
    assert found["details"] == {"repo": REPO, "needs": "push", "linked": False}


def test_a_linked_github_without_push_rights_names_the_same_fix():
    found = _check(_ctx({"state": "refused", "repo": REPO, "needs": "push", "answer": False,
                         "linked": True}))
    assert found["verdict"] == "fixable"
    assert "lacks that access" in found["evidence"]
    assert found["fix"]["action"].startswith(FIX)


def test_github_not_answering_is_unknown_never_ok():
    found = _check(_ctx({"state": "refused", "repo": REPO, "needs": "push", "answer": None,
                         "linked": True}))
    assert found["verdict"] == "unknown"
    assert "did not answer" in found["evidence"]
    assert FIX in found["fix"]["action"]


def test_offline_a_linked_github_is_unknown_because_push_was_not_asked():
    found = _check(_ctx({"state": "unprobed", "needs": "push"}, offline=True))
    assert found["verdict"] == "unknown"
    assert "offline" in found["evidence"]


def test_a_check_that_could_not_read_is_unknown_never_ok():
    found = _check(_ctx({"state": "error", "why": "GitHubRefused: outbound_client_unavailable"}))
    assert found["verdict"] == "unknown"
    assert "outbound_client_unavailable" in found["evidence"]


def test_no_known_person_is_unknown_and_says_how_to_name_one():
    found = _check(_ctx(person=None))
    assert found["verdict"] == "unknown"
    assert REPO in found["evidence"]
    assert "--as" in found["fix"]["action"]


def test_push_rights_of_the_persons_own_github_are_ready():
    found = _check(_ctx({"state": "ok", "needs": "push"}))
    assert found["verdict"] == "ready"
    assert "usr_alice's own GitHub can push" in found["evidence"]


def test_an_install_that_does_not_check_peoples_github_is_ready():
    assert _check(_ctx({"state": "not_checked"}))["verdict"] == "ready"


def test_a_grant_that_already_covers_the_run_is_ready_and_no_consent_is_asked():
    found = _check(_ctx({"state": "not_asked", "covered": True}))
    assert found["verdict"] == "ready"
    assert "covers this run" in found["evidence"]


def test_a_run_whose_consent_names_no_repository_has_nothing_to_check():
    assert _check(_ctx(repos=())) is None
    assert _check(_ctx({"state": "not_asked"})) is None


# ── one predicate: the person's own token, one read-only GET ─────────────────────────────────

@pytest.mark.parametrize("status,perms,answer", [
    (200, {"push": True}, True), (200, {"pull": True, "push": False}, False),
    (404, None, False), (401, None, False), (403, None, False),
    (503, None, None), (429, None, None)])
def test_github_says_yes_no_or_did_not_answer(monkeypatch, status, perms, answer):
    import json

    calls: list[tuple[str, str]] = []

    def transport(method, url, headers, body):
        calls.append((method, url))
        return github_app.Response(status, json.dumps({"permissions": perms or {}}).encode())

    monkeypatch.setattr(github_app, "TRANSPORT", transport)
    monkeypatch.setattr(vault, "token_for_access_check", lambda *a, **k: "gho_PERSON")
    assert github_app.user_repo_access("usr_alice", REPO, "push", db=None) is answer
    assert calls == [("GET", f"{github_app.api_base()}/repos/{REPO}")]  # one read, nothing written
    assert github_app.user_has_repo_access("usr_alice", REPO, "push", db=None) is (answer is True)


def test_no_link_is_a_no_without_asking_github(monkeypatch):
    monkeypatch.setattr(github_app, "TRANSPORT", lambda *a: pytest.fail("GitHub was asked"))
    monkeypatch.setattr(vault, "token_for_access_check", lambda *a, **k: None)
    assert github_app.user_repo_access("usr_alice", REPO, "push", db=None) is False


@pytest.mark.parametrize("answer", [True, False, None])
def test_the_start_door_refuses_exactly_what_the_shared_predicate_refuses(monkeypatch, answer):
    grants.clear_guard_cache()
    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: True)
    monkeypatch.setattr(grants, "_github_link_version", lambda *a, **k: "v1")
    monkeypatch.setattr(github_app, "user_repo_access", lambda *a, **k: answer)
    gh = {"repos": [REPO], **consent.CONSENT_GITHUB_NEEDS}
    refused = grants.user_repo_access_refusal("org_boss", "usr_alice", gh, db=None)
    if answer is True:
        assert refused is None
        grants.check_user_repo_access("org_boss", "usr_alice", gh, db=None)
    else:
        assert refused == {"repo": REPO, "needs": "push", "answer": answer}
        with pytest.raises(grants.GrantRefused) as e:
            grants.check_user_repo_access("org_boss", "usr_alice", gh, db=None)
        assert e.value.code == "github_access_lacking"
    grants.clear_guard_cache()


def test_github_not_answering_is_asked_again_rather_than_remembered(monkeypatch):
    grants.clear_guard_cache()
    answers = iter([None, True])
    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: True)
    monkeypatch.setattr(grants, "_github_link_version", lambda *a, **k: "v1")
    monkeypatch.setattr(github_app, "user_repo_access", lambda *a, **k: next(answers))
    gh = {"repos": [REPO], **consent.CONSENT_GITHUB_NEEDS}
    assert grants.user_repo_access_refusal("org_boss", "usr_alice", gh, db=None)["answer"] is None
    assert grants.user_repo_access_refusal("org_boss", "usr_alice", gh, db=None) is None
    grants.clear_guard_cache()


# ── the pre-check and the start door agree (DB-backed) ───────────────────────────────────────

@pytest.fixture()
def door(monkeypatch):
    from sqlalchemy import text

    from ._identity_agents_support import add_person, build_env, install_github

    env = build_env(monkeypatch)
    gh = install_github(monkeypatch, env)
    with env.db() as c:
        add_person(c, env.bob)                 # a second person: the guard is on (D28)
        c.execute(text("INSERT INTO roles (id, role_key, agent_id, project_id) "
                       "VALUES ('role_atlas', 'atlas', :a, :p)"), {"a": env.atlas, "p": env.project})
    return env, gh


def _precheck_says(env, *, probe: bool = True) -> dict[str, Any]:
    from ._identity_agents_support import BUILDER

    with env.db() as c:
        return consent.own_github_refusal(
            c, org_id="org_boss", person_id=env.alice, project_id=env.project,
            role_keys=["atlas"], permissions=BUILDER, repos=[REPO], probe=probe)


def _start_door_refuses(env) -> bool:
    """What the consent sheet posts at Start: the run's repositories with CONSENT_GITHUB_NEEDS."""
    try:
        env.grant(repos=(REPO,), gh=dict(consent.CONSENT_GITHUB_NEEDS), children=())
    except grants.GrantRefused as e:
        assert e.code == "github_access_lacking"
        return True
    return False


def test_with_no_github_linked_both_refuse_and_the_precheck_names_the_fix(door):
    env, gh = door
    said = _precheck_says(env)
    assert said == {"state": "refused", "repo": REPO, "needs": "push", "answer": False,
                    "linked": False}
    assert _start_door_refuses(env)
    assert not gh.urls("GET")                  # no link: nobody asked GitHub


def test_a_link_without_push_both_refuse(door):
    from ._identity_agents_support import link_github

    env, gh = door
    link_github(env, env.alice, "gho_ALICE")
    gh.user_repo_perms[("gho_ALICE", REPO)] = {"pull": True, "push": False}
    said = _precheck_says(env)
    assert said["state"] == "refused" and said["answer"] is False and said["linked"] is True
    assert _start_door_refuses(env)


def test_push_rights_both_pass_and_then_no_consent_is_asked(door):
    from ._identity_agents_support import link_github

    env, gh = door
    link_github(env, env.alice, "gho_ALICE")
    gh.user_repo_perms[("gho_ALICE", REPO)] = {"pull": True, "push": True}
    assert _precheck_says(env) == {"state": "ok", "needs": "push"}
    assert not _start_door_refuses(env)
    assert _precheck_says(env) == {"state": "not_asked", "covered": True}


def test_github_not_answering_both_refuse_and_the_precheck_says_unknown(door, monkeypatch):
    from ._identity_agents_support import link_github

    env, _ = door
    link_github(env, env.alice, "gho_ALICE")
    monkeypatch.setattr(github_app, "TRANSPORT",
                        lambda method, url, headers, body: github_app.Response(503, b"{}"))
    said = _precheck_says(env)
    assert said["state"] == "refused" and said["answer"] is None
    assert _start_door_refuses(env)


def test_offline_the_precheck_never_asks_github(door):
    from ._identity_agents_support import link_github

    env, gh = door
    link_github(env, env.alice, "gho_ALICE")
    assert _precheck_says(env, probe=False) == {"state": "unprobed", "needs": "push"}
    assert not gh.urls("GET")


def test_one_person_installs_check_nothing_and_neither_refuses(door):
    from sqlalchemy import text

    env, _ = door
    with env.db() as c:
        c.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :b"),
                  {"b": env.bob})
    assert _precheck_says(env) == {"state": "not_checked"}
    assert not _start_door_refuses(env)
