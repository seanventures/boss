"""A BOSS-side failure starts a self-fix, linked to its recommendation, run, step and cause
(BOSSAPP-571 R1, spec §5.2 Stages 0–2), through the real recovery loop and the runner's sweep.

And the other side of R1: when self-fix is off (BOSS_SELF_FIX=off, the org switch), BOSS is
not sure enough, the install cannot change itself, or the cause had its attempts, today's
behaviour runs unchanged — recorded for BOSS's developers and parked for a BOSS update — and the
step says why.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from boss_engine.services import self_fix as sf
from boss_engine.services import step_recovery as sr

from ._self_fix_support import failing_build_step, harness, self_fix_on  # noqa: F401
from .test_a_failed_step_stops_the_run import _steps
from .test_workflow_capability_runner import eng, project  # noqa: F401


def _rows(h):
    with h.eng.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT * FROM self_fixes WHERE project_id = :p ORDER BY created_at"),
            {"p": h.project}).mappings().all()]


def test_a_boss_side_build_failure_starts_a_self_fix_and_parks_on_it(harness, monkeypatch):
    with harness.eng.begin() as c:
        run, awaited = failing_build_step(harness, c, monkeypatch)
        step = _steps(c, run["id"])["s"]
        jobs = c.execute(text("SELECT kind, payload FROM jobs WHERE project_id = :p AND kind = "
                              "'self_fix.build'"), {"p": harness.project}).mappings().all()
        rec = c.execute(text("SELECT id, status, self_fix_id, class_signature FROM "
                             "boss_recommendations WHERE project_id = :p"),
                        {"p": harness.project}).mappings().first()
        events = [r[0] for r in c.execute(text(
            "SELECT event_type FROM events WHERE entity_id = :r AND event_type LIKE 'self_fix.%' "
            "ORDER BY created_at"), {"r": run["id"]}).all()]
    rows = _rows(harness)
    assert len(rows) == 1
    sfx = rows[0]
    assert sfx["status"] == sf.CLAIMED
    assert (sfx["run_id"], sfx["step_id"], sfx["step_kind"]) == (run["id"], "s", "build")
    assert sfx["signature"] == "build:boss:repository_root"
    assert sfx["class_signature"] == "build:boss:repository_root"
    assert sfx["recommendation_id"] == rec["id"]
    assert rec["status"] == "fixing" and rec["self_fix_id"] == sfx["id"]
    assert sfx["confidence"] in ("medium", "high")
    handle = step["output"]["await"]
    assert step["status"] == "waiting"
    assert handle["kind"] == sf.AWAIT_KIND and handle["id"] == sfx["id"]
    assert handle["build_run_id"] == awaited and handle["step_kind"] == "build"
    assert step["output"]["self_fix_id"] == sfx["id"]
    assert "fixing itself" in step["output"]["note"] or sfx["id"] in step["output"]["note"]
    assert len(jobs) == 1 and jobs[0]["payload"]["self_fix_id"] == sfx["id"]
    assert events[:2] == ["self_fix.proposed", "self_fix.claimed"]


def test_while_it_builds_the_step_keeps_waiting_and_says_what_boss_is_doing(harness,
                                                                            monkeypatch):
    from boss_engine.services import workflow_capability_runner as runner

    from ._self_fix_support import Worker

    with harness.eng.begin() as c:
        run, _ = failing_build_step(harness, c, monkeypatch)
        sfx_id = _steps(c, run["id"])["s"]["output"]["self_fix_id"]
        sf.transition(c, sfx_id, sf.DIAGNOSING, actor="test",
                      sentence="Mender: reading BOSS's own code to find the bug.")
        runner.tick(c, Worker(harness.eng, harness.settings), project_id=harness.project)
        step = _steps(c, run["id"])["s"]
    assert step["status"] == "waiting" and step["output"]["await"]["kind"] == sf.AWAIT_KIND
    assert step["output"]["note"] == "Mender: reading BOSS's own code to find the bug."


def test_a_self_fix_that_ends_without_holding_falls_back_to_a_boss_update_wait(harness,
                                                                               monkeypatch):
    from boss_engine.services import workflow_capability_runner as runner

    from ._self_fix_support import Worker

    with harness.eng.begin() as c:
        run, _ = failing_build_step(harness, c, monkeypatch)
        sfx_id = _steps(c, run["id"])["s"]["output"]["self_fix_id"]
        sf.refuse(c, sfx_id, "no_reproduction", "no failing test", settings=harness.settings)
        runner.tick(c, Worker(harness.eng, harness.settings), project_id=harness.project)
        step = _steps(c, run["id"])["s"]
    handle = step["output"]["await"]
    assert step["status"] == "waiting"
    assert handle["kind"] == sr.AWAIT_KIND and handle["then"] == sr.THEN_BOSS_UPDATE
    assert "did not fix itself" in step["output"]["note"]


def test_off_by_environment_means_todays_behaviour(harness, monkeypatch):
    monkeypatch.setenv("BOSS_SELF_FIX", "off")
    with harness.eng.begin() as c:
        run, _ = failing_build_step(harness, c, monkeypatch)
        step = _steps(c, run["id"])["s"]
        jobs = c.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p AND kind = "
                              "'self_fix.build'"), {"p": harness.project}).scalar()
    assert _rows(harness) == [] and jobs == 0
    assert step["output"]["await"]["then"] == sr.THEN_BOSS_UPDATE
    assert "BOSS_SELF_FIX" in step["output"]["self_fix_skipped"]
    assert step["output"]["boss_recommendation_id"]


def test_off_by_the_org_switch_means_todays_behaviour(harness, monkeypatch):
    with harness.eng.begin() as c:
        sf.set_switch(c, on=False, actor="test", why="not today")
        run, _ = failing_build_step(harness, c, monkeypatch)
        step = _steps(c, run["id"])["s"]
    assert _rows(harness) == []
    assert step["output"]["await"]["then"] == sr.THEN_BOSS_UPDATE
    assert "switched off" in step["output"]["self_fix_skipped"]


def test_an_install_that_cannot_change_itself_says_so(harness, monkeypatch):
    monkeypatch.setenv("BOSS_SOURCE_SHA", "unknown")
    with harness.eng.begin() as c:
        run, _ = failing_build_step(harness, c, monkeypatch)
        step = _steps(c, run["id"])["s"]
    assert _rows(harness) == []
    assert "git checkout" in step["output"]["self_fix_skipped"]


def test_switching_off_cancels_what_is_building_and_keeps_what_is_installed(harness):
    first = harness.propose(run_id="", signature="build:boss:repository_root")["self_fix"]
    second = harness.propose(run_id="", signature="deploy:port_held:8000", kind="deploy",
                             files=[])
    with harness.eng.begin() as c:
        if second.get("outcome") == "started":
            c.execute(text("UPDATE self_fixes SET status = 'installed' WHERE id = :i"),
                      {"i": second["self_fix"]["id"]})
        sf.set_switch(c, on=False, actor="test", why="stop")
        statuses = {r["id"]: r["status"] for r in c.execute(text(
            "SELECT id, status FROM self_fixes WHERE project_id = :p"),
            {"p": harness.project}).mappings()}
        audit_types = [r[0] for r in c.execute(text(
            "SELECT event_type FROM events WHERE project_id = :p AND event_type = "
            "'self_fix.cancelled'"), {"p": harness.project}).all()]
    assert statuses[first["id"]] == sf.CANCELLED and audit_types
    if second.get("outcome") == "started":
        assert statuses[second["self_fix"]["id"]] == "installed"


def test_the_class_signature_strips_this_installs_parameters():
    assert sf.class_signature("deploy:port_held:8000") == "deploy:port_held"
    assert sf.class_signature("deploy:unwired:res-db") == "deploy:unwired:res-db"
    assert sf.class_signature("deploy:unwired:helpdesk-db") == "deploy:unwired"
    assert sf.class_signature("build:boss:repository_root") == "build:boss:repository_root"
    assert sf.class_signature("deploy:install_1:0a1b2c3d4e") == "deploy:install_1:0a1b2c3d4e"


def test_the_evidence_a_self_fix_keeps_holds_no_host_address(harness):
    out = harness.propose(evidence_extra={"log_tail": "ssh deploy@203.0.113.10: refused",
                                          "headline": "Port 8000 on 203.0.113.10 is held"})
    assert "203.0.113.10" not in json.dumps(out["self_fix"]["evidence"])
