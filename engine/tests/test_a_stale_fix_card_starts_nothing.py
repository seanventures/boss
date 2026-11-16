"""A re-check's "Fix these" card is about one PR, at one moment (BOSSAPP-1328).

The #168 review: Fix cards carried no pull_request_url and were never withdrawn, so pressing an
old one after a newer build started a paid follow-up from a stale snapshot — and handing that
snapshot to the ledger reopened rows fixed since as regressions. And outside an enforced request
anyone could press it. Now the card names its PR and is withdrawn by a newer re-check, build or
merge card of that PR; a press rebuilds the work from the ledger's rows open NOW, or refuses
("nothing open", "a newer build exists"); and only a person with steer may press it.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import acceptance_gate as ag
from boss_engine.services import change_recheck as cr
from boss_engine.services import checks_failing as cf
from boss_engine.services import decisions as dmod
from boss_engine.services import run_decisions
from boss_engine.services.acceptance import ledger as al

from .test_a_change_can_be_rechecked import PR, _cleanup, _run
from .test_a_resumed_build_runs_as_the_person import _Jobs
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name


def _member(c, project_id, member, kind="human"):
    c.execute(text(
        "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
        "role_keys, permissions) VALUES (:i, :p, :m, :m, :k, '[\"ceo\"]'::jsonb, "
        "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
        {"i": f"pm_{project_id}_{member}", "p": project_id, "m": member, "k": kind})


def _lineage_with_card(eng, project, *, status="completed"):  # noqa: F811
    record = cf.collect([ag.GATE])
    with eng.begin() as c:
        _member(c, project, "ceo")
        root = _run(c, project)
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = jsonb_build_object("
                       "'root_run_id', id) WHERE id = :i"), {"i": root})
        recheck = _run(c, project, status=status, fctx={"root_run_id": root, cr.KEY: True})
    card = cr.raise_fix_card(eng, run_id=recheck, project_id=project, task_id=None,
                             title="Northgate gateway", spec={"owner": "acme", "repo": "gw"},
                             base_branch=f"boss/code/{root}", parent_root=root,
                             parent_origin=None, workflow_run_id=None,
                             findings=cf.followup_findings(record), checks_failing=record)
    return root, recheck, card


def _status(eng, decision_id):  # noqa: F811
    with eng.begin() as c:
        return c.execute(text("SELECT status, resolution_note FROM decisions WHERE id = :d"),
                         {"d": decision_id}).mappings().one()


def _runs(eng, project):  # noqa: F811
    with eng.begin() as c:
        return c.execute(text("SELECT count(*) FROM code_task_runs WHERE project_id = :p"),
                         {"p": project}).scalar()


def test_the_card_names_its_pr_and_a_newer_builds_card_withdraws_it(eng, project):  # noqa: F811
    try:
        root, recheck, card = _lineage_with_card(eng, project)
        with eng.begin() as c:
            rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                            {"d": card}).scalar()
            assert rec["pull_request_url"] == PR
            newer = _run(c, project, fctx={"root_run_id": root})
            merge = f"dec_{uuid.uuid4().hex[:10]}"
            c.execute(text("INSERT INTO decisions (id, project_id, question, recommendation, "
                           "status, created_at) VALUES (:i, :p, 'Approve code change', "
                           "CAST(:r AS JSONB), 'pending', clock_timestamp())"),
                      {"i": merge, "p": project, "r": json.dumps(
                          {"action": "review_pull_request", "run_id": newer,
                           "pull_request_url": PR.upper().replace("HTTP", "http") + "/"})})
            gone = run_decisions.withdraw_superseded_merge_cards(c, project, PR, merge,
                                                                 run_id=newer)
        assert card in gone and _status(eng, card)["status"] == "withdrawn"
    finally:
        _cleanup(eng, project)


def test_a_newer_attempt_of_the_change_withdraws_the_card(eng, project):  # noqa: F811
    from boss_engine.services.code_task_service import retry_code_task

    try:
        _root, recheck, card = _lineage_with_card(eng, project, status="failed")
        out = retry_code_task(eng, get_settings(), _Jobs(), recheck, fix_note="try again")
        got = _status(eng, card)
        assert got["status"] == "withdrawn" and out["run_id"] in got["resolution_note"]
    finally:
        _cleanup(eng, project)


def test_pressing_a_card_after_the_fix_starts_nothing(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dmod, "_spawn_as_this_request", lambda fn: fn())
    try:
        _root, _recheck, card = _lineage_with_card(eng, project)
        with eng.begin() as c:
            c.execute(text("UPDATE findings SET status = 'fixed' WHERE project_id = :p"),
                      {"p": project})
        before = _runs(eng, project)
        with pytest.raises(dmod.InvalidDecisionTransitionError, match="Nothing is open"), \
                eng.begin() as c:
            dmod.DecisionService(get_settings()).transition(c, card, action="approve",
                                                            actor="ceo", note="go")
        assert _runs(eng, project) == before
        assert _status(eng, card)["status"] == "pending"
        with eng.begin() as c:
            assert {r["status"] for r in c.execute(text(
                "SELECT status FROM findings WHERE project_id = :p"),
                {"p": project}).mappings()} == {"fixed"}, "no row reopened as a regression"
            rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                            {"d": card}).scalar()
        with pytest.raises(cr.RecheckRefused, match="Nothing is open"):
            cr.start_fix(eng, get_settings(), _Jobs(), rec, project_id=project, task_id=None)
    finally:
        _cleanup(eng, project)


def test_a_press_builds_from_what_is_open_now_not_the_snapshot(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dmod, "_spawn_as_this_request", lambda fn: fn())
    monkeypatch.setattr("boss_engine.db.get_engine", lambda: eng)
    try:
        _root, recheck, card = _lineage_with_card(eng, project)
        with eng.begin() as c:
            fixed_id = c.execute(text("SELECT id FROM findings WHERE project_id = :p LIMIT 1"),
                                 {"p": project}).scalar()
            al.observe_findings(c, project_id=project, lineage=_root, run_id=recheck,
                                findings=[{"severity": "major",
                                           "text": "(review_code) gw/handler.go:3 — new gap"}],
                                stage="review")
            c.execute(text("UPDATE findings SET status = 'fixed' WHERE id = :i"), {"i": fixed_id})
        svc = dmod.DecisionService(get_settings())
        svc.jobs = _Jobs()
        with eng.begin() as c:
            svc.transition(c, card, action="approve", actor="ceo", note="go")
        with eng.begin() as c:
            fix = c.execute(text("SELECT spec FROM code_task_runs WHERE project_id = :p AND "
                                 "finalize_ctx->>'deferred_from_run' = :r"),
                            {"p": project, "r": recheck}).mappings().all()
            reopened = c.execute(text("SELECT status FROM findings WHERE id = :i"),
                                 {"i": fixed_id}).scalar()
        assert len(fix) == 1 and "new gap" in fix[0]["spec"]["instructions"]
        assert reopened == "fixed", "the snapshot never reopens a fixed row"
    finally:
        _cleanup(eng, project)


@pytest.mark.parametrize("actor,kind", [("agent:coo", None), ("boss", None), ("coo", "agent")])
def test_an_agent_or_worker_can_never_press_it(eng, project, actor, kind):  # noqa: F811
    try:
        _root, _recheck, card = _lineage_with_card(eng, project)
        with eng.begin() as c:
            if kind:
                _member(c, project, actor, kind)
            assert cr.may_start_fix(c, project, actor) is False
            assert cr.may_start_fix(c, project, "ceo") is True
    finally:
        _cleanup(eng, project)
