"""Retry with a note: a workflow step's Try again carries what should change to the builder.

A build step's failure says "retry the build with a note on what to fix", and POST
.../retry-step took no note — the builder re-ran blind. Now the note is stored on the step run
(`output.retry_note`) and in `workflow.step_retried`, and a retried build resumes the failed
attempt's lineage with the note as the operator's feedback, plus what failed last time: the
gates and the tail of the verify output, redacted.
"""
import json
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.db import begin
from apps.api.app.services.core import make_id
from apps.api.app.services.jobs import JobLeaseService
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows

PROJECT = "boss-dogfood"
JOBS = JobLeaseService(get_settings())
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
NOTE = "The login test fails because /login redirects to /home; redirect to /dashboard."


def _failed_code_task(c) -> str:
    run_id = make_id("crun")
    verify = "\n".join(f"noise line {i}" for i in range(100)) + (
        f"\nFAIL tests/login.test.ts: expected /dashboard\nGITHUB_TOKEN={SECRET}\n")
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
        "state, finalize_ctx, error) VALUES (:id, :p, NULL, 'failed', 'done', 'Helpdesk', "
        "CAST(:spec AS JSONB), CAST(:state AS JSONB), CAST(:fc AS JSONB), 'tests failed')"),
        {"id": run_id, "p": PROJECT,
         "spec": json.dumps({"instructions": "build the helpdesk", "acceptance_criteria": [],
                             "test_commands": [], "owner": "o", "repo": "r",
                             "base_branch": "main"}),
         "state": json.dumps({"last_verify_output": verify}),
         # Its work was pushed to its branch: a retry repairs only work a build KEPT
         # (code_task_service.lineage_kept_work; engine test_a_retry_builds_onto_the_kept_work).
         "fc": json.dumps({"attempt": 1, "root_run_id": run_id,
                           "work_branch": f"boss/code/{run_id}"})})
    c.execute(text(
        "INSERT INTO quality_gates (id, project_id, code_task_run_id, status, gates_failed) "
        "VALUES (:id, :p, :r, 'fail', CAST(:gf AS JSONB))"),
        {"id": make_id("qg"), "p": PROJECT, "r": run_id,
         "gf": json.dumps(["tests_passed", "code_review_passed"])})
    return run_id


def _failed_build_run(engine, *, code_task: bool = True) -> tuple[str, str | None]:
    key = f"retrynote_{uuid4().hex[:10]}"
    with begin(engine) as c:
        definition = workflows.create_definition(
            c, project_id=PROJECT, workflow_key=key, name="Retry note", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        version = workflows.publish_version(
            c, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "build", "steps": [
                {"id": "build", "kind": "capability",
                 "binding": {"capability": "notify", "inputs": {"message": "x"}}, "next": []},
            ]})
        run = workflows.start_run(
            c, project_id=PROJECT, actor="ceo", input_data={},
            dedupe_key=f"dk_{uuid4().hex[:10]}", workflow_version_id=version["id"])
        crun = _failed_code_task(c) if code_task else None
        output = {"capability": "build", "status": "failed",
                  **({"code_task_run_id": crun, "await": {"kind": "code_task", "id": crun}}
                     if crun else {})}
        c.execute(text(
            "UPDATE workflow_step_runs SET status='failed', last_error='tests failed', "
            "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='build'"),
            {"r": run["id"], "o": json.dumps(output)})
        c.execute(text("UPDATE workflow_runs SET status='failed', error='build failed' "
                       "WHERE id=:r"), {"r": run["id"]})
    return run["id"], crun


def _retry(client, run_id, **extra):
    return client.post(f"/v1/delivery-workflows/runs/{run_id}/retry-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "build", **extra})


def _output(engine, run_id):
    with begin(engine) as c:
        return c.execute(text("SELECT output FROM workflow_step_runs WHERE workflow_run_id=:r "
                              "AND step_id='build'"), {"r": run_id}).scalar_one()


def _rebuild(engine, run_id, inputs=None):
    ctx = SimpleNamespace(engine=engine, settings=get_settings(), jobs=JOBS)
    with begin(engine) as c:
        return runner._retried_build(
            c, ctx, PROJECT, {"id": "build"}, {"run_id": run_id},
            inputs or {"title": "Helpdesk", "instructions": "build the helpdesk"})


def _instructions(engine, crun):
    with begin(engine) as c:
        return c.execute(text("SELECT spec->>'instructions' FROM code_task_runs WHERE id=:i"),
                         {"i": crun}).scalar_one()


def test_the_note_is_kept_on_the_step_and_in_the_event(client, engine):
    run_id, crun = _failed_build_run(engine)
    response = _retry(client, run_id, note=NOTE)
    assert response.status_code == 200, response.text
    out = _output(engine, run_id)
    assert out["retry_note"] == NOTE
    assert out["retried_from_code_task_run_id"] == crun
    with begin(engine) as c:
        data = c.execute(text("SELECT data FROM events WHERE entity_id=:r "
                              "AND event_type='workflow.step_retried'"), {"r": run_id}).scalar_one()
    assert data["note"] == NOTE


def test_a_note_over_the_limit_is_refused(client, engine):
    run_id, _ = _failed_build_run(engine)
    response = _retry(client, run_id, note="x" * 4001)
    assert response.status_code in (400, 422), response.text


def test_the_note_and_the_previous_failure_reach_the_resumed_builds_instructions(client, engine):
    run_id, crun = _failed_build_run(engine)
    assert _retry(client, run_id, note=NOTE).status_code == 200
    resumed, _, note = _rebuild(engine, run_id)
    assert resumed is not None and note == NOTE
    assert resumed["resumed_from_code_task_run_id"] == crun
    assert resumed["retry_note"] == NOTE
    instructions = _instructions(engine, resumed["run_id"])
    # The note, verbatim, under a heading of its own.
    assert f"## Feedback to address (from the operator)\n{NOTE}" in instructions
    # What failed last time: the gates and the verify tail, last 60 lines, redacted.
    assert "## What failed last time (previous attempt)" in instructions
    assert "Gates failed: tests_passed, code_review_passed" in instructions
    assert "FAIL tests/login.test.ts: expected /dashboard" in instructions
    assert "noise line 99" in instructions and "noise line 10\n" not in instructions
    assert SECRET not in instructions
    # The same lineage: the new attempt is the failed one's child.
    with begin(engine) as c:
        fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"),
                         {"i": resumed["run_id"]}).scalar_one()
    assert fctx["parent_run_id"] == crun and fctx["root_run_id"] == crun


def test_a_retry_without_a_note_still_resumes_with_the_failure(client, engine):
    run_id, crun = _failed_build_run(engine)
    response = _retry(client, run_id)
    assert response.status_code == 200, response.text
    assert _output(engine, run_id)["retry_note"] == ""
    resumed, _, note = _rebuild(engine, run_id)
    assert note == "" and "retry_note" not in resumed
    instructions = _instructions(engine, resumed["run_id"])
    assert "Feedback to address (from the operator)" not in instructions
    assert "Gates failed: tests_passed, code_review_passed" in instructions
    assert SECRET not in instructions


def test_a_build_with_nothing_to_resume_carries_the_note_into_its_instructions(client, engine):
    run_id, _ = _failed_build_run(engine, code_task=False)
    assert _retry(client, run_id, note=NOTE).status_code == 200
    resumed, inputs, note = _rebuild(engine, run_id)
    assert resumed is None and note == NOTE
    assert inputs["instructions"].endswith(f"{runner.RETRY_NOTE_HEADING}\n{NOTE}")


def test_a_build_that_was_never_retried_is_untouched(engine):
    run_id, _ = _failed_build_run(engine)
    inputs = {"title": "Helpdesk", "instructions": "build the helpdesk"}
    assert _rebuild(engine, run_id, inputs) == (None, inputs, "")


# --- a retry continues from the failed attempt's saved workspace ---------------------------------

def _save_workspace_on(engine, crun, key="saved_workspace"):
    files = [{"path": "helpdesk-ai/src/App.tsx", "content": "export {}"},
             {"path": "helpdesk-ai/e2e/login.spec.ts", "content": "test()"}]
    value = {"files": files, "after": "exit", "file_count": 2} if key == "saved_workspace" else files
    with begin(engine) as c:
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                       "jsonb_build_object(CAST(:k AS text), CAST(:v AS JSONB)) WHERE id=:i"),
                  {"k": key, "v": json.dumps(value), "i": crun})


def _fctx(engine, crun):
    with begin(engine) as c:
        return c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"),
                         {"i": crun}).scalar_one()


def test_a_retried_build_continues_from_the_saved_workspace(client, engine):
    run_id, crun = _failed_build_run(engine)
    _save_workspace_on(engine, crun)
    assert _retry(client, run_id, note=NOTE).status_code == 200
    resumed, _, _ = _rebuild(engine, run_id)
    assert resumed["continues_from_workspace"] == 2
    assert "saved workspace (2 files)" in resumed["resume"]
    assert _fctx(engine, resumed["run_id"])["seed_workspace_from"] == crun


def test_a_gate_blocked_builds_preserved_diff_is_a_workspace_too(client, engine):
    """Build 7's case: blocked by its template gate, its tree preserved as pending_diff."""
    run_id, crun = _failed_build_run(engine)
    _save_workspace_on(engine, crun, key="pending_diff")
    assert _retry(client, run_id).status_code == 200
    resumed, _, _ = _rebuild(engine, run_id)
    assert resumed["continues_from_workspace"] == 2
    assert _fctx(engine, resumed["run_id"])["seed_workspace_from"] == crun


def test_with_no_saved_workspace_the_retry_says_it_runs_the_full_pipeline(client, engine):
    run_id, crun = _failed_build_run(engine)
    assert _retry(client, run_id).status_code == 200
    resumed, _, _ = _rebuild(engine, run_id)
    assert resumed["continues_from_workspace"] == 0
    assert resumed["resume"].startswith("full pipeline")
    assert "seed_workspace_from" not in _fctx(engine, resumed["run_id"])
