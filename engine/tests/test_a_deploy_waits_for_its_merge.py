"""A deploy of a build whose pull request is not merged WAITS on the merge, on the step.

helpdesk-ai wfr_c8dbccd76a92488c99dc (2026-11-06): the build step finished when PR #3 opened, the
deploy step planned a release of the UNMERGED head, and the merge question sat outside the run
(withdrawn and re-raised by each repair) — the run never said Needs you. The rule: wait on the
merge, show the PR's current merge question on the step, run again once GitHub shows it merged.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.core import now_utc

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


def _seen(monkeypatch, state, number=3):
    monkeypatch.setattr(runner, "_unmerged_build",
                        lambda *a, **k: None if state == "merged" else
                        {"state": state, "number": number})


def test_an_unmerged_build_holds_its_deploy_on_the_merge(monkeypatch):
    _seen(monkeypatch, "open")
    monkeypatch.setattr(runner, "_merge_question", lambda *a: {"id": "dec_q", "status": "pending"})
    out = runner._merge_wait(None, "p", "crun_1")
    assert out["status"] == "awaiting"
    assert out["await"]["kind"] == runner.MERGE_AWAIT_KIND and out["await"]["id"] == "crun_1"
    assert out["await"]["decision_id"] == "dec_q"
    assert "approve PR #3" in out["note"] and "Nothing is deployed" in out["note"]


def test_an_approved_but_unmerged_pr_says_to_merge_it_in_github(monkeypatch):
    _seen(monkeypatch, "merge_pending")
    monkeypatch.setattr(runner, "_merge_question", lambda *a: {"id": "dec_q", "status": "approved"})
    assert "Merge it in GitHub" in runner._merge_wait(None, "p", "crun_1")["note"]


def test_a_merged_build_deploys_without_waiting(monkeypatch):
    _seen(monkeypatch, "merged")
    assert runner._merge_wait(None, "p", "crun_1") is None


def test_a_closed_pr_is_refused_in_words(monkeypatch):
    _seen(monkeypatch, "closed")
    with pytest.raises(runner.SettledCapabilityError, match="closed without merging"):
        runner._merge_wait(None, "p", "crun_1")


class _Conn:
    def __init__(self):
        self.sql: list[str] = []

    def execute(self, stmt, params=None):
        self.sql.append(str(stmt))


def _held(looked_ago=60):
    at = (now_utc() - timedelta(seconds=looked_ago)).isoformat()
    handle = {"kind": "merge", "id": "crun_1", "looked_at": at}
    return {"id": "wfs_1", "step_id": "deploy", "decision_id": None}, {"await": handle}, handle


def test_the_held_step_shows_the_current_question_and_never_releases_on_the_approval(monkeypatch):
    _seen(monkeypatch, "open")
    monkeypatch.setattr(runner, "_merge_question", lambda *a: {"id": "dec_new", "status": "pending"})
    requeued = []
    monkeypatch.setattr(runner.workflows, "requeue_waiting_step", lambda *a, **k: requeued.append(k))
    c = _Conn()
    step, output, handle = _held()
    assert runner._follow_merge(c, project_id="p", run_id="r", step=step, output=output,
                                handle=handle) is None
    assert any("decision_id = :d" in q and "released_for" in q for q in c.sql)
    assert not requeued


def test_a_merge_on_github_runs_the_deploy_again(monkeypatch):
    _seen(monkeypatch, "merged")
    monkeypatch.setattr(runner, "_merge_question", lambda *a: {"id": "dec_q", "status": "approved"})
    requeued = []
    monkeypatch.setattr(runner.workflows, "requeue_waiting_step", lambda *a, **k: requeued.append(k))
    step, output, handle = _held()
    out = runner._follow_merge(_Conn(), project_id="p", run_id="r", step=step, output=output,
                               handle=handle)
    assert out["outcome"] == "merged_deploying" and len(requeued) == 1


def test_github_is_asked_at_most_every_thirty_seconds(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GitHub was asked too soon")
    monkeypatch.setattr(runner, "_unmerged_build", boom)
    monkeypatch.setattr(runner, "_merge_question", lambda *a: None)
    step, output, handle = _held(looked_ago=5)
    assert runner._follow_merge(_Conn(), project_id="p", run_id="r", step=step, output=output,
                                handle=handle) is None


def test_the_current_question_is_the_newest_one_not_withdrawn():
    try:
        engine = create_engine(TEST_DB, future=True)
        with engine.begin() as c:
            c.execute(text("SELECT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    pid, crun = f"p_{uuid.uuid4().hex[:8]}", f"crun_{uuid.uuid4().hex[:10]}"
    url = f"https://github.com/acme/app/pull/{uuid.uuid4().int % 9000}"
    with engine.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, 'm', :i)"),
                  {"i": pid, "w": wid})
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, state, "
                       "finalize_ctx, pull_request_url, pull_request_number) VALUES (:i, :p, 't', "
                       "'completed', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, :u, 3)"),
                  {"i": crun, "p": pid, "u": url})
        for n, (did, st) in enumerate([("dec_old", "withdrawn"), ("dec_cur", "pending"),
                                       ("dec_retracted", "withdrawn")]):
            c.execute(text("INSERT INTO decisions (id, project_id, question, recommendation, "
                           "options, risk_tier, status, created_at) VALUES (:i, :p, 'merge?', "
                           "CAST(:r AS JSONB), '[]'::jsonb, 1, :s, now() + make_interval(secs => :n))"),
                      {"i": f"{did}_{pid}", "p": pid, "s": st, "n": n,
                       "r": json.dumps({"action": "review_pull_request", "pull_request_url": url})})
        got = runner._merge_question(c, pid, crun)
        c.execute(text("DELETE FROM decisions WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
    assert got and got["id"] == f"dec_cur_{pid}"
