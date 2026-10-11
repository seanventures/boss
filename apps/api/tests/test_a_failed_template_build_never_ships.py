"""A template build that FAILS its gates never ends "completed" and is never offered for release
(tester round 2, N1).

Seen: login-oauth's build failed template_tests / written_tests_pass / nothing_broke, sat about
20 min in `repairing` (repair_max=0) while the console said "Reviewing · Working 10m", then the
shepherd's abandoned-run sweep flipped it to `completed` because its phase was `done`. Apps read
"Done · no PR"; the workflow's Build step went green and its Deploy step asked to pick a host and
approve a release for a build with four failed checks. Held here as classes:

  * the repair budget decides: repair_max=0 blocks a failed template gate at once (no
    `repairing`); a non-zero budget repairs, and blocks once it is spent;
  * a run at its last phase is not a passed build: the sweep ends a `repairing` or template-held
    run BLOCKED with why and what next, never `completed`, and leaves an ended-blocked run alone;
  * the template application follows the build to `blocked`;
  * a workflow step waiting on a `repairing` build follows the repair; one waiting on a finished
    but not-passed build FAILS with why, so nothing after it (deploy) runs;
  * deploy_build refuses a build that did not pass, whatever its status says;
  * a repair attempt keeps its workflow run, so the template gates still run on it.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import governed_code_task_service as gov


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _run(engine, project_id: str, *, status: str, fctx: dict, phase: str = "done",
         age_minutes: int = 30, title: str = "Member portal: add Login") -> str:
    run_id = _uid("crun")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx, "
            "created_at, updated_at) VALUES (:id, :p, :st, :ph, :t, '{}'::jsonb, '{}'::jsonb, "
            "CAST(:f AS JSONB), now() - make_interval(mins => :age), now() - make_interval(mins => :age))"),
            {"id": run_id, "p": project_id, "st": status, "ph": phase, "t": title,
             "f": json.dumps(fctx), "age": age_minutes})
    return run_id


def _failed_gates(engine, project_id: str, run_id: str, gates: list[str]) -> None:
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, status, gates_passed, gates_failed, "
            "evidence) VALUES (:i, :p, :r, 'fail', '[]'::jsonb, CAST(:g AS JSONB), '{}'::jsonb)"),
            {"i": _uid("qg"), "p": project_id, "r": run_id, "g": json.dumps(gates)})


def _row(engine, run_id: str) -> dict:
    with engine.begin() as db:
        return dict(db.execute(text(
            "SELECT status, phase, error, finalize_ctx FROM code_task_runs WHERE id = :i"),
            {"i": run_id}).mappings().one())


# ---- the disposition: the build's repair budget decides --------------------------------------

@pytest.mark.parametrize("gate", ["template_tests", "no_committed_secrets"])
def test_repair_max_zero_blocks_a_failed_template_gate_at_once(gate):
    failing = [gate, "written_tests_pass", "nothing_broke"]
    action = gov.resolve_gate_failure_action(
        failing=failing, advisory_gate=True, rounds_used=0, max_repair_rounds=2,
        never_block=True, faults=gov.classify_gate_faults(failing=failing, evidence={}),
        template_repair_max=0)
    assert action == "block"


def test_a_non_zero_budget_repairs_then_blocks_when_spent():
    kw = dict(failing=["template_tests"], advisory_gate=True, max_repair_rounds=2,
              never_block=True, faults={"template_tests": "WORK"}, template_repair_max=1)
    assert gov.resolve_gate_failure_action(rounds_used=0, **kw) == "repair"
    assert gov.resolve_gate_failure_action(rounds_used=1, **kw) == "block"
    # the review-repair cap still bounds it from above
    kw["template_repair_max"] = 5
    assert gov.resolve_gate_failure_action(rounds_used=2, **kw) == "block"


def test_the_budget_is_only_for_template_gates():
    """An ordinary build's judgement gates keep the review-repair loop they had."""
    assert gov.resolve_gate_failure_action(
        failing=["code_review_passed"], advisory_gate=False, rounds_used=0, max_repair_rounds=2,
        never_block=True, faults={"code_review_passed": "WORK"}, template_repair_max=0) == "repair"


def test_an_explicit_zero_review_repair_rounds_is_honoured():
    assert gov.review_repair_rounds(SimpleNamespace(code_review_repair_max_rounds=0)) == 0
    assert gov.review_repair_rounds(SimpleNamespace(code_review_repair_max_rounds=None)) == 2
    assert gov.review_repair_rounds(SimpleNamespace()) == 2
    assert gov.review_repair_rounds(SimpleNamespace(code_review_repair_max_rounds=3)) == 3


def test_the_pipeline_passes_the_runs_repair_budget():
    import inspect

    src = inspect.getsource(gov)
    assert src.count("template_repair_max=max_repair") == 2
    assert "max_repair_rounds = review_repair_rounds(settings)" in src


# ---- the sweep: a finished phase is not a passed build ----------------------------------------

def test_the_sweep_ends_a_template_held_repairing_run_blocked_never_completed(world, engine):
    from boss_engine.config import Settings
    from boss_engine.services import shepherd_service
    from boss_engine.templates import apply as apply_service

    p = world.project_a
    wfr = _uid("wfr")
    run_id = _run(engine, p, status="repairing", fctx={
        "workflow_run_id": wfr, "failing_gates": ["template_tests", "nothing_broke"],
        "review_repair_round": 1})
    _failed_gates(engine, p, run_id, ["template_tests", "written_tests_pass", "nothing_broke"])
    with engine.begin() as db:
        tid = _uid("tpl")
        db.execute(text("INSERT INTO app_templates (id, org_id, template_key, version, source, status) "
                        "VALUES (:i, :o, :k, '1.0.0', 'builtin', 'active')"),
                   {"i": tid, "o": world.org, "k": f"login-{tid}"})
        db.execute(text(
            "INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, stack, "
            "workflow_run_id, status) VALUES (:i, :p, 'member portal', :t, '1.0.0', 'node', :w, 'building')"),
            {"i": _uid("tap"), "p": p, "t": tid, "w": wfr})

    shepherd_service.sweep_abandoned_runs(engine, Settings(), p)

    row = _row(engine, run_id)
    assert row["status"] == "blocked" and row["phase"] == "done"
    assert row["error"].startswith("Blocked: the template's tests did not pass.")
    assert "retry the build" in row["error"]                       # what to do next
    assert row["finalize_ctx"]["template_hold"]["gates"] == ["template_tests"]
    with engine.begin() as db:
        apps = apply_service.applications(db, p, "member portal")
    assert [a["status"] for a in apps if a.get("workflow_run_id") == wfr] == ["blocked"]

    # and a second sweep leaves a run that ENDED blocked alone (it used to become completed)
    shepherd_service.sweep_abandoned_runs(engine, Settings(), p)
    assert _row(engine, run_id)["status"] == "blocked"


def test_a_repairing_run_whose_repair_never_started_ends_blocked_with_words(world, engine):
    from boss_engine.config import Settings
    from boss_engine.services import shepherd_service

    p = world.project_a
    run_id = _run(engine, p, status="repairing", fctx={"failing_gates": ["code_review_passed"],
                                                       "review_repair_next_run": "crun_gone"})
    shepherd_service.sweep_abandoned_runs(engine, Settings(), p)
    row = _row(engine, run_id)
    assert row["status"] == "blocked"
    assert "repair round never started" in row["error"]


def test_an_ordinary_finished_run_still_completes(world, engine):
    from boss_engine.config import Settings
    from boss_engine.services import shepherd_service

    run_id = _run(engine, world.project_a, status="running", fctx={})
    shepherd_service.sweep_abandoned_runs(engine, Settings(), world.project_a)
    assert _row(engine, run_id)["status"] == "completed"


# ---- nothing downstream treats it as passed ----------------------------------------------------

def test_deploy_build_refuses_a_build_that_did_not_pass(world, engine):
    from boss_engine.config import Settings
    from boss_engine.services.delivery_pipeline import deploy_build

    p = world.project_a
    # completed by an old path, but its template gate failed
    held = _run(engine, p, status="completed", fctx={})
    _failed_gates(engine, p, held, ["template_tests"])
    out = deploy_build(engine, Settings(), project_id=p, run_id=held, actor="ceo")
    assert out["status"] == "failed"
    assert out["reason"].startswith("Blocked: the template's tests did not pass.")
    # an attempt whose work went to a repair round is not the passed build either
    superseded = _run(engine, p, status="completed", fctx={
        "review_repair_next_run": "crun_child", "failing_gates": ["nothing_broke"]})
    out = deploy_build(engine, Settings(), project_id=p, run_id=superseded, actor="ceo")
    assert out["status"] == "failed" and "failed its checks" in out["reason"]


def _workflow_step_waiting_on(engine, project_id: str, run_id: str) -> tuple[str, str]:
    wfr, step = _uid("wfr"), _uid("wsr")
    with engine.begin() as db:
        wf = {"d": _uid("wfd"), "v": _uid("wfv")}
        db.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
                        "VALUES (:d, :p, :k, 'Member portal', 'ceo')"),
                   {"d": wf["d"], "p": project_id, "k": wf["d"]})
        db.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, spec, "
                        "content_hash, created_by) VALUES (:v, :p, :d, 1, CAST(:spec AS JSONB), 'h', 'ceo')"),
                   {"v": wf["v"], "p": project_id, "d": wf["d"], "spec": json.dumps({
                       "entry_step": "build", "input_schema": {"directive": "string", "title": "string"},
                       "steps": [{"id": "build", "kind": "capability", "next": [],
                                  "binding": {"capability": "build", "inputs": {
                                      "title": {"from_input": "title"},
                                      "instructions": {"from_input": "directive"}}}}]})})
        db.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, actor, "
            "status, input) VALUES (:i, :p, :d, :v, 'ceo', 'waiting', '{}'::jsonb)"),
            {"i": wfr, "p": project_id, "d": wf["d"], "v": wf["v"]})
        db.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, status, "
            "output) VALUES (:i, :p, :r, 'build', 'capability', 'waiting', CAST(:o AS JSONB))"),
            {"i": step, "p": project_id, "r": wfr,
             "o": json.dumps({"await": {"kind": "code_task", "id": run_id}})})
    return wfr, step


def _step(engine, step_id: str) -> dict:
    with engine.begin() as db:
        return dict(db.execute(text("SELECT status, last_error AS error, output FROM workflow_step_runs WHERE id = :i"),
                               {"i": step_id}).mappings().one())


def test_a_step_waiting_on_a_repairing_build_follows_the_repair(world, engine):
    from boss_engine.services.workflow_capability_runner import resume_awaited

    p = world.project_a
    parent = _run(engine, p, status="repairing", fctx={}, age_minutes=10)
    wfr, step = _workflow_step_waiting_on(engine, p, parent)
    child = _run(engine, p, status="running", phase="implement", age_minutes=5,
                 fctx={"root_run_id": parent, "parent_run_id": parent, "workflow_run_id": wfr})
    with engine.begin() as db:
        out = resume_awaited(db, project_id=p, run_id=wfr, worker_id="t")
    assert [o["outcome"] for o in out] == ["following_retry"]
    s = _step(engine, step)
    assert s["status"] == "waiting" and s["output"]["await"]["id"] == child


def test_a_step_waiting_on_a_finished_but_held_build_fails_with_why(world, engine):
    from boss_engine.services.workflow_capability_runner import resume_awaited

    p = world.project_a
    run_id = _run(engine, p, status="completed", fctx={})
    _failed_gates(engine, p, run_id, ["template_tests", "nothing_broke"])
    wfr, step = _workflow_step_waiting_on(engine, p, run_id)
    with engine.begin() as db:
        out = resume_awaited(db, project_id=p, run_id=wfr, worker_id="t")
    assert [o["outcome"] for o in out] == ["failed"]
    s = _step(engine, step)
    assert s["status"] == "failed"
    assert "template's tests did not pass" in (s["error"] or "")


def test_a_repair_attempt_keeps_its_workflow_run():
    """The template gates read the template from the workflow run: a repair child without the
    link ran with no template gates at all, and the application never saw it."""
    import inspect

    from boss_engine.services import code_task_service as cts

    src = inspect.getsource(cts.retry_code_task)
    assert '{"workflow_run_id": fctx_prev["workflow_run_id"]}' in src
