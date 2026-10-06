"""Run scorecards: every terminal transition of every unit kind writes exactly one scorecard, a
rerun is idempotent, late outcomes rescore the same row, weights are versioned, history is
backfilled. docs/product/RUN_SCORECARDS.md, "Tests that must exist"."""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.tests.improve_fixtures import (cards, project, reset_state, seed_build,
                                             seed_meeting, seed_research, seed_workflow_run)

_VOLATILE = {"computed_at", "late_outcomes_at"}


def _stable(rows):
    return [{k: v for k, v in r.items() if k not in _VOLATILE} for r in rows]


def test_a_build_settles_into_exactly_one_scorecard_and_a_rerun_is_idempotent(engine):
    from boss_engine.services.agent_acts import settle_code_task

    reset_state(engine)
    with engine.begin() as c:
        rid = seed_build(c, status="running")
    settle_code_task(engine, rid)
    with engine.begin() as c:
        assert cards(c, rid) == []  # running is not terminal: nothing is written
        c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :id"), {"id": rid})
    settle_code_task(engine, rid)
    with engine.begin() as c:
        first = cards(c, rid)
    settle_code_task(engine, rid)
    with engine.begin() as c:
        again = cards(c, rid)
    assert len(first) == 1 and len(again) == 1
    assert _stable(first) == _stable(again)
    row = first[0]
    assert row["unit_kind"] == "build" and row["terminal_state"] == "completed"
    assert row["harness"] == "claude_code" and row["model"] == "claude-sonnet-5"
    assert row["provider"] == "anthropic" and row["role_key"] == "software_engineer"
    assert (row["tests_passed"], row["tests_total"]) == (1, 1)
    assert row["review_approved"] is True and row["gates_failed"] == 0
    assert float(row["cost_usd"]) == 1.2 and row["score_version"] == "1"
    assert 0 <= float(row["score"]) <= 1


def test_every_unit_kind_reaches_its_scorecard_through_its_real_transition(engine):
    """Builds through settle_code_task, runs through cancel_run's event, steps, panels and
    research through the events their writers emit — and every one exactly once."""
    from boss_engine.services.core import emit_event
    from boss_engine.services.workflows import cancel_run

    reset_state(engine)
    with engine.begin() as c:
        qc = seed_build(c, status="failed", finalize={"is_followup": True,
                                                       "failure_class": "tests_failed"})
        failed = seed_build(c, status="cancelled")
        wf, steps = seed_workflow_run(c, status="running")
        c.execute(text("UPDATE workflow_step_runs SET status = 'succeeded', completed_at = now() "
                       "WHERE id = :id"), {"id": steps[0]})
        emit_event(c, project(), "workflow.step_succeeded", "ceo", "workflow_run", wf,
                   {"workflow_step_run_id": steps[0], "step_id": "research"})
        cancel_run(c, project(), wf, "ceo", reason="changed my mind", compensate=False)
        mrun, mid = seed_meeting(c, decision_status="approved")
        emit_event(c, project(), "meeting.completed", "boss", "meeting", mid, {})
        disc = seed_research(c)
        emit_event(c, project(), "discovery.completed", "boss", "discovery_run", disc, {})
        # The same terminal events again: still one row each.
        emit_event(c, project(), "meeting.completed", "boss", "meeting", mid, {})
        emit_event(c, project(), "discovery.completed", "boss", "discovery_run", disc, {})
    from boss_engine.services.agent_acts import settle_code_task

    settle_code_task(engine, qc)
    settle_code_task(engine, failed)
    with engine.begin() as c:
        got = {u: cards(c, u) for u in (qc, failed, wf, steps[0], mrun, disc)}
    assert all(len(v) == 1 for v in got.values()), {k: len(v) for k, v in got.items()}
    assert got[qc][0]["unit_kind"] == "quick_change"
    assert got[qc][0]["error_class"] == "tests_failed" and float(got[qc][0]["score"]) == 0.0
    assert got[failed][0]["terminal_state"] == "cancelled"
    assert got[wf][0]["unit_kind"] == "workflow_run"
    assert got[wf][0]["terminal_state"] == "cancelled"
    assert got[steps[0]][0]["unit_kind"] == "step" and got[steps[0]][0]["step_key"] == "research"
    assert got[mrun][0]["unit_kind"] == "panel" and got[mrun][0]["prd_approved"] is True
    assert got[disc][0]["unit_kind"] == "research"
    assert got[disc][0]["research_verdict"] == "pursue" and float(got[disc][0]["score"]) == 0.8


def test_the_sweep_records_what_no_event_announced_and_skipped_steps_get_none(engine):
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        wf, steps = seed_workflow_run(c, status="completed")
        c.execute(text("UPDATE workflow_step_runs SET status = 'succeeded' WHERE id = :id"),
                  {"id": steps[0]})
        c.execute(text("UPDATE workflow_step_runs SET status = 'skipped' WHERE id = :id"),
                  {"id": steps[1]})
        quiet = seed_build(c, status="completed")
        scorecards.sweep(c)
        scorecards.sweep(c)  # twice: still one each
        assert len(cards(c, wf)) == 1 and len(cards(c, steps[0])) == 1
        assert cards(c, steps[1]) == []
        assert len(cards(c, quiet)) == 1 and cards(c, quiet)[0]["source"] == "backfill"


def test_a_late_merge_updates_the_same_row_and_raises_the_score(engine):
    from boss_engine.services.agent_acts import settle_code_task
    from boss_engine.services.core import emit_event

    reset_state(engine)
    with engine.begin() as c:
        rid = seed_build(c, pr=42, decision_status="pending")
    settle_code_task(engine, rid)
    with engine.begin() as c:
        before = cards(c, rid)[0]
        assert before["pr_opened"] is True and before["pr_merged"] is False
        did = c.execute(text("SELECT decision_id FROM code_task_runs WHERE id = :id"),
                        {"id": rid}).scalar()
        c.execute(text("UPDATE decisions SET status = 'executed' WHERE id = :d"), {"d": did})
        emit_event(c, project(), "decision.executed", "ceo", "decision", did, {})
        after = cards(c, rid)
    assert len(after) == 1
    assert after[0]["pr_merged"] is True and after[0]["late_outcomes_at"] is not None
    assert round(float(after[0]["score"]) - float(before["score"]), 3) == 0.15 or \
        float(after[0]["score"]) == 1.0


def test_a_revert_and_a_rollback_take_the_score_down(engine):
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        rid = seed_build(c, pr=77, decision_status="executed")
        merged = scorecards.record_scorecard(c, "build", rid)
        seed_build(c, title="Revert \"Add a thing\" (#77)")
        reverted = scorecards.record_scorecard(c, "build", rid, late=True)
    assert merged["pr_merged"] and not merged["pr_reverted"]
    assert reverted["pr_reverted"] is True
    assert reverted["score"] < merged["score"]


def test_weights_are_versioned_shown_and_every_row_rescored(engine, client):
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        rid = seed_build(c)
        scorecards.record_scorecard(c, "build", rid)
    r = client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                                   "weights": {"tests": 0.7}})
    assert r.status_code == 200, r.text
    assert r.json()["score"]["version"] == "1.1"
    weights = {w["key"]: w["value"] for w in r.json()["score"]["weights"]}
    assert weights["tests"] == 0.7
    with engine.begin() as c:
        # Rescored with the save itself: nothing is left for the tick, the view is never blank.
        assert cards(c, rid)[0]["score_version"] == "1.1"
        assert scorecards.rescore(c) == 0
        logged = c.execute(text("SELECT data FROM events WHERE event_type = 'settings.changed' "
                                "AND entity_id = 'score_weights'")).scalar()
    assert logged and logged["to"] == "1.1"
    bad = client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                                     "weights": {"tests": 3}})
    assert bad.status_code == 422


def test_backfill_and_the_scorecards_view(engine, client):
    reset_state(engine)
    with engine.begin() as c:
        for _ in range(3):
            seed_build(c)
        seed_build(c, status="failed", gates_failed=["tests_pass"])
    r = client.post("/v1/scorecards/backfill", json={"project_id": project(), "actor": "ceo"})
    assert r.status_code == 200, r.text
    assert r.json()["recorded"]["build"] == 4
    v = client.get(f"/v1/scorecards?project_id={project()}&group=harness&win=30d")
    assert v.status_code == 200, v.text
    body = v.json()
    assert body["total"]["n"] == 4 and body["total"]["backfilled"] == 4
    assert body["rows"][0]["key"] == "claude_code" and body["rows"][0]["n"] == 4
    assert body["costing"] and body["costing"][0]["state"] == "failed"
    assert body["costing"][0]["href"].startswith("/?area=code_tasks&build=")
    assert {w["key"] for w in body["weights"]} >= {"tests", "merged_bonus", "cost_penalty"}
    assert client.get(f"/v1/scorecards?project_id={project()}&group=nope").status_code == 422


def test_a_scorecard_holds_no_content(engine):
    """Names, numbers and verdicts only: the build's title and instructions never land here."""
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        rid = seed_build(c, title="Zqx secret brief about acme payroll",
                         spec={"role_key": "software_engineer",
                               "instructions": "Zqx prompt: refactor /srv/acme/payroll.py"})
        scorecards.record_scorecard(c, "build", rid)
        row = cards(c, rid)[0]
    dumped = json.dumps(row, default=str)
    assert "Zqx" not in dumped and "payroll" not in dumped and "/srv" not in dumped
    assert len(dumped) < 2048 + 512  # the doc's ≤ 2 KB target, with JSON quoting overhead
