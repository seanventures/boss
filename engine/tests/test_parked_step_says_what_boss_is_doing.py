"""THE PARKED STEP SAYS WHAT BOSS IS DOING (P0; BOSSAPP-571 spec §10.1): every self-fix status
has a sentence on the step, never "waiting" without why; the strip's stages say where it is; the
facts line names the files, the tests added, the cost of the budget and the minutes; and every
step showing a self-fix shows it as it is NOW (a verified fix never still says "Install")."""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import self_fix as sf

from ._self_fix_upstream_support import insert_verified
from ._self_fix_support import clean_project
from .test_workflow_capability_runner import eng, project  # noqa: F401


@pytest.mark.parametrize("status", sf.STATUSES)
def test_every_status_has_a_line_that_says_why(status):
    line = sf.step_line({"id": "sfx_1", "status": status, "progress": {}})
    assert line and len(line) > 12
    assert line.strip().lower() not in ("waiting", "waiting…", "waiting...")
    view = sf.step_view({"id": "sfx_1", "status": status, "progress": {}})
    assert view["label"] == "Fixing BOSS" and len(view["stages"]) == len(sf.STAGES)
    assert view["chip"] in ("Building", "Needs you", "Installed", "Verified", "Rolled back",
                            "Refused", "Sent upstream", "In release")


def test_the_strip_says_where_it_is():
    view = sf.step_view({"id": "sfx_1", "status": sf.FIXING, "progress": {
        "sentence": "Mender: the new test fails on BOSS as it is (as expected). Making it pass."}})
    states = {s["stage"]: s["state"] for s in view["stages"]}
    assert states["found"] == states["reproduced"] == "done" and states["fixing"] == "now"
    assert states["you"] == "next" and "Making it pass" in view["line"]
    done = sf.step_view({"id": "sfx_1", "status": sf.VERIFIED})
    assert all(s["state"] == "done" for s in done["stages"])
    needs = sf.step_view({"id": "sfx_1", "status": sf.AWAITING_APPROVAL, "decision_id": "dec_1"})
    assert needs["needs_you"] and "needs your approval" in needs["line"]


def test_the_facts_line():
    facts = sf.step_facts({
        "diff_stat": {"files": ["engine/boss_engine/services/stack_deploy.py",
                                "engine/tests/test_x.py"]},
        "red_green": {"tests": ["engine/tests/test_x.py"]}, "cost_micros": 1_840_000,
        "budget_usd_micros": 10_000_000, "started_at": "2026-10-17T08:00:00+00:00",
        "verified_at": "2026-10-17T08:06:30+00:00"})
    assert facts == {"files": ["stack_deploy.py"], "tests_added": 1, "cost_usd": 1.84,
                     "budget_usd": 10.0, "minutes": 6}


def test_every_step_showing_a_fix_shows_it_as_it_is_now(eng, project):  # noqa: F811
    sfx_id = insert_verified(eng, project, status="installed")
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workflow_definitions LIMIT 1")).scalar()
    if wid is None:
        pytest.skip("no workflow definition to hang a step on")
    try:
        with eng.begin() as c:
            ver = c.execute(text("SELECT id FROM workflow_versions WHERE "
                                 "workflow_definition_id = :w LIMIT 1"), {"w": wid}).scalar()
            if ver is None:
                pytest.skip("no workflow version")
            run_id, step_row = f"wfr_{uuid.uuid4().hex[:16]}", f"wsr_{uuid.uuid4().hex[:16]}"
            c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                           "workflow_version_id, actor, status) VALUES (:i, :p, :w, :v, 'ceo', "
                           "'waiting')"), {"i": run_id, "p": project, "w": wid, "v": ver})
            stale = sf.step_view({"id": sfx_id, "status": sf.HEALTH_WINDOW})
            c.execute(text(
                "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                "step_kind, status, output) VALUES (:i, :p, :r, 'deploy', 'deploy', 'waiting', "
                "CAST(:o AS JSONB))"),
                {"i": step_row, "p": project, "r": run_id,
                 "o": json.dumps({"self_fix": {**stale, "waiting_on": True}})})
            sf.transition(c, sfx_id, sf.VERIFYING, actor="sys_worker", sentence="resuming")
            sf.transition(c, sfx_id, sf.VERIFIED, actor="sys_worker", sentence="verified")
            out = c.execute(text("SELECT output FROM workflow_step_runs WHERE id = :i"),
                            {"i": step_row}).scalar()
        view = out["self_fix"]
        assert view["status"] == sf.VERIFIED and view["chip"] == "Verified"
        assert all(s["state"] == "done" for s in view["stages"])
        assert view["waiting_on"] is True, "a waiting step stays marked as waiting on it"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM workflow_step_runs WHERE project_id = :p"),
                      {"p": project})
            c.execute(text("DELETE FROM workflow_runs WHERE project_id = :p"), {"p": project})
        clean_project(eng, project)
