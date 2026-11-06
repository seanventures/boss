"""A person acting on what an ambient agent is working on takes it over at once (design §4 L4).

The fix for "keeps firing after I took over" (openai/codex #57009) is wired where people act: a
GitHub push, review or comment on the PR an agent is fixing, and Stop on the build or run it
started. The agent's lease becomes the person's, and its next lease check fails, so it stops.
BOSS's own App, GitHub bots and BOSS's own actors are not a person: their events change nothing.
A webhook or a Stop never fails because of the ambient board.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services.ambient import board, collab, person_actions

from ._ambient_support import eng, open_item, project  # noqa: F401

PR_REF = "pr:acme/shop#41"


def _held(c, pid, *, subject_ref=PR_REF, fingerprint="fp:ci-41"):
    item = open_item(c, pid, fingerprint=fingerprint, task_class="ci_fix_boss_pr",
                     subject_ref=subject_ref)["item"]
    held = board.claim(c, item["id"], owner="agent:software_engineer")
    return item["id"], held["lease_token"]


def _event(login="octocat", kind="User", **extra):
    return {"sender": {"login": login, "type": kind},
            "repository": {"full_name": "acme/shop"}, **extra}


def test_a_person_pushing_to_the_pr_takes_the_item_and_the_agent_loses_its_lease(eng, project):
    with eng.begin() as c:
        item_id, token = _held(c, project)
        taken = person_actions.on_github_event(
            c, project_id=project, event="pull_request",
            payload=_event(action="synchronize", pull_request={"number": 41}))
        assert taken == [item_id]
        item = board.get(c, item_id)
        assert item["lease_owner"] == "person:github:octocat" and item["lease_kind"] == "person"
        with pytest.raises(board.LeaseLost):
            board.assert_lease(c, item_id, owner="agent:software_engineer", token=token)


def test_a_comment_on_the_pr_takes_it_over_too(eng, project):
    with eng.begin() as c:
        item_id, _ = _held(c, project)
        taken = person_actions.on_github_event(
            c, project_id=project, event="issue_comment",
            payload=_event(issue={"number": 41, "pull_request": {"url": "x"}}))
        assert taken == [item_id]


@pytest.mark.parametrize("login,kind", [("boss-app[bot]", "Bot"), ("dependabot[bot]", "User"),
                                        ("boss-bot", "User")])
def test_bots_and_boss_itself_never_take_over(eng, project, login, kind):
    with eng.begin() as c:
        item_id, _ = _held(c, project)
        taken = person_actions.on_github_event(
            c, project_id=project, event="pull_request", bot_login="boss-bot",
            payload=_event(login=login, kind=kind, pull_request={"number": 41}))
        assert taken == []
        assert board.get(c, item_id)["lease_owner"] == "agent:software_engineer"


def test_a_comment_on_another_pr_changes_nothing(eng, project):
    with eng.begin() as c:
        item_id, _ = _held(c, project)
        assert person_actions.on_github_event(
            c, project_id=project, event="pull_request",
            payload=_event(pull_request={"number": 99})) == []


def test_stopping_the_build_an_agent_started_takes_its_item_over(eng, project):
    from boss_engine.services.code_task_service import cancel_run

    run = f"crun_{uuid4().hex[:10]}"
    with eng.begin() as c:
        item_id, token = _held(c, project, subject_ref=None, fingerprint="fp:build")
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec) VALUES "
                       "(:i, :p, 'fix CI', 'running', '{}'::jsonb)"), {"i": run, "p": project})
        board.link(c, item_id, "code_task", run, actor="agent:software_engineer")
    with eng.begin() as c:
        cancel_run(c, run, project, actor="ceo", reason="I will do it")
        item = board.get(c, item_id)
        assert item["lease_owner"] == "person:ceo"
        assert [m["kind"] for m in collab.messages(c, item_id)][-1] == "handoff"


def test_boss_stopping_its_own_run_is_not_a_person_taking_over(eng, project):
    with eng.begin() as c:
        item_id, _ = _held(c, project, subject_ref="run:wfr_x", fingerprint="fp:run")
        assert person_actions.on_run_cancelled(c, project_id=project, run_id="wfr_x",
                                               actor="boss") == []
        assert person_actions.on_run_cancelled(c, project_id=project, run_id="wfr_x",
                                               actor="agent:sre") == []
        assert person_actions.on_run_cancelled(c, project_id=project, run_id="wfr_x",
                                               actor="ceo") == [item_id]


def test_a_failure_on_the_board_never_fails_the_caller(eng, project, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("board down")

    monkeypatch.setattr(board, "on_person_action", broken)
    with eng.begin() as c:
        assert person_actions.on_github_event(
            c, project_id=project, event="pull_request",
            payload=_event(pull_request={"number": 41})) == []
        c.execute(text("SELECT 1"))   # the transaction is still usable
