"""The New project cost footer prices the work an operator starts — all of it, and only it.

GET /v1/delivery-workflows/costs summed only the code tasks' model calls of every workflow run
in the project, whatever state it was in. So the footer under-priced a workflow by its panel and
its research (their spend lives on meeting_runs / discovery_runs, not in llm_calls), counted runs
that never finished, mixed in the system's own captain and swarm passes, and priced a "quick
change" with the shepherd's automatic gate retries in it. "Spec it first" had no price at all.

Every case seeds real rows on the real database and reads the HTTP route.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.tests.test_a_feature_builds_onto_its_app import (
    _call, _insert_run, _project, _workflow_run,
)


def _install(client):
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": _project(), "actor": "ceo"})


def _meeting(engine, *, usd, constraints=None, origin=None, status="completed",
             directive="Spec the scorecard change."):
    meeting_id = f"mrun_{uuid4().hex[:10]}"
    fctx = {"origin": origin} if origin else {}
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO meeting_runs (id, project_id, status, phase, directive, constraints, "
            "usage, finalize_ctx, created_at) VALUES (:id, :p, :s, 'done', :d, "
            "CAST(:c AS JSONB), CAST(:u AS JSONB), CAST(:f AS JSONB), now() - interval '1 hour')"
        ), {"id": meeting_id, "p": _project(), "s": status, "d": directive,
            "c": json.dumps({"meeting_type": "prd_panel", **(constraints or {})}),
            "u": json.dumps({"cost_usd_micros": int(usd * 1_000_000), "model": "m"}),
            "f": json.dumps(fctx)})
    return meeting_id


def _discovery(engine, *, usd):
    run_id = f"disc_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO discovery_runs (id, project_id, status, phase, title, usage) "
            "VALUES (:id, :p, 'completed', 'done', 'Research', CAST(:u AS JSONB))"
        ), {"id": run_id, "p": _project(),
            "u": json.dumps({"cost_usd_micros": int(usd * 1_000_000)})})
    return run_id


def _step(engine, workflow_run_id, step_id, kind, output):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind,"
            " iteration, status, output) VALUES (:id, :p, :r, :s, :k, 0, 'succeeded',"
            " CAST(:o AS JSONB))"
        ), {"id": f"wfs_{uuid4().hex[:10]}", "p": _project(), "r": workflow_run_id,
            "s": f"{step_id}_{uuid4().hex[:4]}", "k": kind, "o": json.dumps(output)})


def _job(engine, run_id, priority):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO jobs (id, project_id, kind, priority, status, payload) "
            "VALUES (:id, :p, 'code.task', :pr, 'succeeded', CAST(:pl AS JSONB))"
        ), {"id": f"job_{uuid4().hex[:10]}", "p": _project(), "pr": priority,
            "pl": json.dumps({"run_id": run_id})})


def _costs(client):
    response = client.get("/v1/delivery-workflows/costs", params={"project_id": _project()})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_run_costs_its_panel_and_research_and_only_finished_runs_count(client, engine):
    _install(client)
    # Run A: $1 of build + a $2 panel convened by the run + $0.5 of research its step awaited.
    a = _workflow_run(client, engine, "Feature A")
    _insert_run(engine, "crun_wfa", fctx={"workflow_run_id": a})
    _call(engine, "crun_wfa", 1.0, included=True)
    _meeting(engine, usd=2.0, origin={"workflow_run_id": a, "step_run_id": "x"})
    disc = _discovery(engine, usd=0.5)
    _step(engine, a, "research", "research", {"discovery_id": disc})
    # Run B: its panel is found through the step that waited on it (request_meeting's shape).
    b = _workflow_run(client, engine, "Feature B")
    _insert_run(engine, "crun_wfb", fctx={"workflow_run_id": b})
    _call(engine, "crun_wfb", 1.0, included=True)
    panel_b = _meeting(engine, usd=3.0)
    _step(engine, b, "meeting", "meeting", {"meeting": {"run_id": panel_b}})
    # Run C failed half way, with an expensive build: not the price of the workflow.
    c = _workflow_run(client, engine, "Feature C", status="failed")
    _insert_run(engine, "crun_wfc", fctx={"workflow_run_id": c})
    _call(engine, "crun_wfc", 50.0)

    minor = _costs(client)["costs"]["minor_feature"]
    assert minor["runs"] == 2                       # C is not counted
    assert minor["low_usd"] == pytest.approx(3.5 + (4.0 - 3.5) * 0.2)
    assert minor["high_usd"] == pytest.approx(3.5 + (4.0 - 3.5) * 0.8)
    assert minor["metered"] is False


def test_the_systems_own_passes_are_reported_apart_not_priced_in(client, engine):
    _install(client)
    run = _workflow_run(client, engine, "Feature")
    _insert_run(engine, "crun_op", fctx={"workflow_run_id": run})
    _call(engine, "crun_op", 2.0, included=True)
    # A captain pass that happened to name the run, and a swarm build queued at BACKGROUND.
    _insert_run(engine, "crun_captain", fctx={"workflow_run_id": run,
                                              "source": "metatron_captain"})
    _call(engine, "crun_captain", 40.0)
    _insert_run(engine, "crun_swarm", fctx={"is_followup": True, "parent_run_id": "crun_op"})
    _job(engine, "crun_swarm", 90)
    _call(engine, "crun_swarm", 7.0)

    body = _costs(client)
    assert body["costs"]["minor_feature"]["low_usd"] == pytest.approx(2.0)
    assert "@change" not in body["costs"]            # the BACKGROUND follow-up is not a change
    assert body["autonomous"]["runs"] == 2
    assert body["autonomous"]["usd"] == pytest.approx(47.0)
    assert body["autonomous"]["metered"] is True


def test_a_quick_change_is_a_persons_feedback_not_an_automatic_retry(client, engine):
    _install(client)
    _insert_run(engine, "crun_fb", fctx={"attempt": 2, "feedback_history": [
        {"fix_note": "make the button blue"}]})
    _call(engine, "crun_fb", 1.0, included=True)
    for n, note in enumerate((
            "Automatic retry by the task shepherd: died on lease expiry",
            "Task Shepherd: infrastructure. Applying remedy 'substrate_fallback'",
            "Autonomous review-repair: the quality gate blocked on tests")):
        _insert_run(engine, f"crun_auto{n}", fctx={"attempt": 3, "feedback_history": [
            {"fix_note": note}]})
        _call(engine, f"crun_auto{n}", 9.0)
    change = _costs(client)["costs"]["@change"]
    assert change["runs"] == 1
    assert change["low_usd"] == pytest.approx(1.0)


def test_spec_it_first_is_priced_from_panels_and_the_builds_they_produced(client, engine):
    _install(client)
    _insert_run(engine, "crun_app", fctx={"attempt": 1})
    # Two spec-first panels about the app; each produced one follow-up build.
    for n, (panel_usd, build_usd) in enumerate(((1.0, 2.0), (2.0, 4.0))):
        meeting = _meeting(engine, usd=panel_usd, constraints={"extends_run_id": "crun_app"})
        _insert_run(engine, f"crun_spec{n}",
                    fctx={"is_followup": True, "parent_run_id": "crun_app",
                          "origin": {"kind": "meeting", "meeting_run_id": meeting}})
        _call(engine, f"crun_spec{n}", build_usd, included=True)
    # A later quick change on the same app inherits the panel's origin: it is a change, not
    # the panel's build.
    _insert_run(engine, "crun_later", fctx={"is_followup": True, "parent_run_id": "crun_spec1",
                                            "origin": {"meeting_run_id": "irrelevant"}})
    _call(engine, "crun_later", 0.5, included=True)

    costs = _costs(client)["costs"]
    spec = costs["@spec"]
    assert spec["runs"] == 2 and spec["basis"] == "spec_first"
    assert spec["low_usd"] == pytest.approx(3.0 + 3.0 * 0.2)
    assert spec["high_usd"] == pytest.approx(3.0 + 3.0 * 0.8)
    assert costs["@change"]["runs"] == 1             # only crun_later
    assert costs["@change"]["low_usd"] == pytest.approx(0.5)


def test_spec_it_first_falls_back_to_prd_panels_and_their_follow_up(client, engine):
    _install(client)
    meeting = _meeting(engine, usd=1.5)
    _insert_run(engine, "crun_fu", fctx={"is_followup": True, "parent_run_id": "crun_x",
                                         "origin": {"kind": "meeting",
                                                    "meeting_run_id": meeting}})
    _call(engine, "crun_fu", 1.0, included=True)
    spec = _costs(client)["costs"]["@spec"]
    assert spec["basis"] == "prd_panel"
    assert spec["runs"] == 1 and spec["low_usd"] == pytest.approx(2.5)
