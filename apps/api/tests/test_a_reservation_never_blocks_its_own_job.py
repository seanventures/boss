"""A RESERVATION NEVER BLOCKS THE JOB IT WAS MADE FOR, AND NO MODEL IS SAID AS NO MODEL.

THE CASE (stranger test r3, rc10.28, 2026-11-18; fresh install, no model, $10 daily cap): Easy
Mode consented, launched (201) and made a local repository, then:

  - the launch reserved $10.00 — the whole cap (build_budget.reservation_estimate takes
    min(estimate, headroom), and an unknown route is money);
  - status_for_project counted that active hold as committed spend, so the state went red;
  - jobs._budget_state_policy_for_job then deferred the build's OWN job: "Waiting for budget:
    the project's cap is reached" at $0.00 spent — when the real need was a model.

Now:
  - a job is admitted against the money OTHER work holds: its own reservation (made under its id,
    or the reservation of the build it carries) is left out; every other job still counts it;
  - Easy Mode's launch hold is recorded as its build's reservation, so the build is admitted;
  - with no usable model, the launch makes, reserves and starts nothing and says "No model is
    connected …" with a link to Setup › Models; a build or workflow run waiting in such a
    project says "Waiting for a model …" (stuck_work), never a budget sentence.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import model_readiness

CAP = 10_000_000  # $10.00


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    from apps.api.app.routers import factory_launch as route

    monkeypatch.setattr(route._settings, "local_repo_root", str(tmp_path / "repos"))
    monkeypatch.setattr("boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")
    model_readiness.forget()
    yield
    model_readiness.forget()


def _models(monkeypatch, connected: bool) -> None:
    from boss_engine.services import setup_view

    rows = [{"label": "Claude", "pill": {"state": "working"}, "kind": "subscription"}] if connected else []
    monkeypatch.setattr(setup_view, "build_basics", lambda c, s, p, **kw: {"models": rows})
    monkeypatch.setattr(setup_view, "model_is_local", lambda m: False)
    model_readiness.forget()


def _cap(c, project_id: str, daily: int = CAP) -> None:
    c.execute(text("DELETE FROM budget_policies WHERE scope_type='project' AND scope_id=:p"),
              {"p": project_id})
    c.execute(text("INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                   "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
                   "VALUES (:i, 'project', :p, :d, :m, :b)"),
              {"i": f"bp_{uuid.uuid4().hex[:10]}", "p": project_id, "d": daily,
               "m": daily * 30, "b": daily})


def _estimate(monkeypatch, share: float) -> None:
    """The launch's reservation: `share` of the project's daily cap (1.0 = the whole cap, what an
    unknown or pay-per-use route reserved on the stranger's install)."""
    def estimate(connection, settings, project_id, amount_usd_micros=None):
        _cap(connection, project_id)
        return int(CAP * share)

    monkeypatch.setattr("boss_engine.services.build_budget.reservation_estimate", estimate)


def _launch(client, prompt: str):
    return client.post("/v1/factory/launch", json={"prompt": prompt, "mode": "build"})


def _job_of(engine, run_id: str) -> str:
    with engine.connect() as c:
        return str(c.execute(text("SELECT id FROM jobs WHERE kind = 'code.task' AND "
                                  "payload->>'run_id' = :r"), {"r": run_id}).scalar())


def _policy(engine, project_id: str, job_id: str):
    from apps.api.app import main as api_main

    with engine.begin() as c:
        return api_main.jobs._budget_state_policy_for_job(c, project_id, None, job_id, "code.task")


def test_a_job_holding_the_whole_cap_is_admitted_and_others_still_see_it(
        client, engine, monkeypatch):
    _models(monkeypatch, connected=True)
    _estimate(monkeypatch, 1.0)

    launched = _launch(client, f"A CSV quality desk {uuid.uuid4().hex[:6]}.")

    assert launched.status_code == 201, launched.text
    body = launched.json()
    project_id, run_id = body["project_id"], body["run_id"]
    with engine.connect() as c:
        held = c.execute(text("SELECT reservation_id FROM code_task_runs WHERE id = :r"),
                         {"r": run_id}).scalar()
        amount = c.execute(text("SELECT amount_usd_micros FROM budget_reservations "
                                "WHERE id = :i AND status = 'active'"), {"i": held}).scalar()
    assert held == body["reservation_id"] and amount == CAP     # the launch's hold is the run's

    # Everyone else sees the cap used up ...
    from apps.api.app import main as api_main

    with engine.begin() as c:
        seen = api_main.jobs.budget.status_for_project(c, project_id)
    assert seen["budget_state"] == "red"
    # ... but the build's own job is admitted against it.
    assert _policy(engine, project_id, _job_of(engine, run_id)) is None

    # Another job in the same project still counts that hold and is deferred.
    from boss_engine.services.code_task_service import start_code_task

    with engine.begin() as c:
        other = start_code_task(
            c, project_id=project_id, task_id=None, title="Another build",
            instructions=f"do {uuid.uuid4().hex}", acceptance_criteria=[], test_commands=[],
            owner="acme", repo="sandbox", base_branch="main", finalize_ctx={"actor": "ceo"},
            jobs=api_main.jobs)
    held_back = _policy(engine, project_id, _job_of(engine, other))
    assert held_back is not None and held_back["budget_state"] == "red"
    assert held_back["reason"] == "red_halts_non_p0_work"


def test_a_pay_per_use_build_under_the_cap_runs(client, engine, monkeypatch):
    _models(monkeypatch, connected=True)
    _estimate(monkeypatch, 0.4)                  # a known estimate, $4 of the $10 cap

    launched = _launch(client, f"A pantry list {uuid.uuid4().hex[:6]}.")

    assert launched.status_code == 201, launched.text
    body = launched.json()
    assert _policy(engine, body["project_id"], _job_of(engine, body["run_id"])) is None


def test_no_model_makes_reserves_and_starts_nothing_and_says_so(client, engine, monkeypatch):
    _models(monkeypatch, connected=False)
    _estimate(monkeypatch, 1.0)
    with engine.connect() as c:
        projects = c.execute(text("SELECT count(*) FROM projects")).scalar()
        holds = c.execute(text("SELECT count(*) FROM budget_reservations")).scalar()
        jobs = c.execute(text("SELECT count(*) FROM jobs")).scalar()

    refused = _launch(client, f"A flood gauge map {uuid.uuid4().hex[:6]}.")

    assert refused.status_code == 409, refused.text
    said = refused.json()
    assert said["error"] == "needs_model"
    assert said["message"].startswith("No model is connected")
    assert "Setup › Models" in said["message"] and "budget" not in said["message"].lower()
    assert said["fix_href"] == model_readiness.SETUP_MODELS_HREF
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM projects")).scalar() == projects
        assert c.execute(text("SELECT count(*) FROM budget_reservations")).scalar() == holds
        assert c.execute(text("SELECT count(*) FROM jobs")).scalar() == jobs


def test_a_build_waiting_with_no_model_says_it_waits_for_a_model(engine, monkeypatch):
    from boss_engine.services import stuck_work

    _models(monkeypatch, connected=False)
    # Queued and held by the budget, as the stranger's build was.
    held = {"id": "job_x", "status": "queued", "last_error": {"kind": "budget_hold",
            "message": "The project's budget cap is reached."}, "run_after": None,
            "created_at": None, "updated_at": None, "lease_expires_at": None}
    with engine.connect() as c:
        project_id = c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar()
        why = stuck_work.wait_reason(
            c, {"id": "crun_nomodel", "project_id": project_id, "status": "running",
                "phase": "queued", "finalize_ctx": {}}, job=held)
    assert why["code"] == "needs_model" and why["who"] == "you"
    assert why["words"] == model_readiness.WAITING_FOR_MODEL
    assert "budget" not in why["words"].lower()
    assert why["href"] == model_readiness.SETUP_MODELS_HREF
    hint = stuck_work.triage({"run": {}, "job": {}, "wait_reason": why})
    assert hint["cause_code"] == "needs_model" and hint["who"] == "you"

    # With a model it is whatever it was before (here: the budget hold).
    _models(monkeypatch, connected=True)
    with engine.connect() as c:
        why = stuck_work.wait_reason(
            c, {"id": "crun_nomodel", "project_id": project_id, "status": "running",
                "phase": "queued", "finalize_ctx": {}}, job=held)
    assert why["code"] == "budget"

    # A job's own verdict still outranks it: a crash loop is a crash loop, model or not.
    _models(monkeypatch, connected=False)
    with engine.connect() as c:
        why = stuck_work.wait_reason(
            c, {"id": "crun_nomodel", "project_id": project_id, "status": "running",
                "phase": "queued", "finalize_ctx": {"last_crash": {"count": 9}}}, job=held)
    assert why["code"] == "crash_loop"


def test_a_workflow_run_with_no_model_is_not_waiting_at_meeting(engine, monkeypatch):
    """Try a sample and Start a new app start workflow runs: theirs said "Waiting at meeting"."""
    from boss_engine.services import stuck_work, workflows

    _models(monkeypatch, connected=False)
    with engine.begin() as c:
        project_id = c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar()
        definition = workflows.create_definition(
            c, project_id=project_id, workflow_key=f"wf_{uuid.uuid4().hex[:6]}", name="Sample",
            description=None, actor="test", namespace="product")
        version = workflows.publish_version(c, project_id=project_id,
                                            definition_id=definition["id"], actor="test", spec={
            "entry_step": "meeting", "steps": [{"id": "meeting", "kind": "task", "title": "Meet",
                                                "binding": {}, "next": []}]})
        run = workflows.start_run(c, project_id=project_id, actor="ceo", input_data={},
                                  dedupe_key=f"dk_{uuid.uuid4().hex[:8]}",
                                  workflow_version_id=version["id"])
        c.execute(text("UPDATE workflow_step_runs SET status = 'waiting', lease_expires_at = NULL "
                       "WHERE workflow_run_id = :w"), {"w": run["id"]})
        why = stuck_work.workflow_wait_reason(c, run["id"])
    assert why is not None and why["code"] == "needs_model"
    assert why["words"].startswith("Waiting for a model")
