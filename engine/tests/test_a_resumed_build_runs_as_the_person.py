"""A step retry that resumes a saved workspace starts its build AS THE PERSON, under the run's grant.

ac3eeac3 made a retried build step resume the failed attempt (code_task_service.retry_code_task)
— called bare, from the worker, so the resumed code task's job was stamped with BOSS's own
identity and the build ended `blocked` at once: "No one's authority covers this build: it was
started by BOSS itself". A build step's own create_code_task runs inside the run's recorded
authority (workflow_capability_runner._step_authority); the resume now does exactly the same.
No check is relaxed: a run whose authority has ended is refused before anything is queued.
"""
from __future__ import annotations

import json
import types
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage, runs
from boss_engine.services import jobs as jobs_service
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows

from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import (  # noqa: F401
    RUN_INPUT, _publish, _start_consented, staffed)

SPEC = {"entry_step": "build", "steps": [
    {"id": "build", "kind": "capability", "next": [],
     "binding": {"capability": "build", "inputs": {"title": "HelpDesk", "instructions": "build it", "repository": "acme/web"}}}]}


class _Jobs:
    """Enqueues the code.task job the real way, so it carries the ambient authority."""
    settings = None

    def create_job(self, db, *, project_id, task_id, kind, priority, payload):
        return jobs_service.enqueue_job(db, project_id, kind, payload, task_id=task_id,
                                        priority=priority)


def _failed_build(env, run_id: str) -> str:
    crun = f"crun_{uuid.uuid4().hex[:12]}"
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
            "state, finalize_ctx, error) VALUES (:id, :p, NULL, 'failed', 'done', 'HelpDesk', "
            "CAST(:spec AS JSONB), '{}'::jsonb, CAST(:fc AS JSONB), 'tests failed')"),
            {"id": crun, "p": env.project,
             "spec": json.dumps({"instructions": "build it", "acceptance_criteria": [],
                                 "test_commands": [], "owner": "acme", "repo": "web",
                                 "base_branch": "main"}),
             "fc": json.dumps({"attempt": 1, "root_run_id": crun, "workflow_run_id": run_id,
                               "saved_workspace": {"files": [{"path": "a.ts", "content": "x"}],
                                                   "file_count": 1}})})
        c.execute(text(
            "UPDATE workflow_step_runs SET status='failed', last_error='tests failed', "
            "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='build'"),
            {"r": run_id, "o": json.dumps({"capability": "build", "code_task_run_id": crun})})
        c.execute(text("UPDATE workflow_runs SET status='failed' WHERE id=:r"), {"r": run_id})
        workflows.retry_failed_step(c, project_id=env.project, run_id=run_id, step_id="build",
                                    actor="ceo", note="redirect to /dashboard")
    return crun


@pytest.fixture(autouse=True)
def _ports(monkeypatch):
    from boss_engine.services import code_task_service as cts

    monkeypatch.setattr(cts, "_engine_ports", lambda: types.SimpleNamespace(
        persistence=types.SimpleNamespace(transaction=lambda e: e.begin(),
                                          list_runs=lambda *a, **k: [])))


def _resume(env, run_id):
    ctx = types.SimpleNamespace(engine=env.engine, settings=get_settings(), jobs=_Jobs())
    # The worker advances the run: BOSS itself is the ambient identity, as in production.
    with lineage.acting(lineage.SYSTEM_ACTING), env.engine.begin() as c:
        return runner._retried_build(c, ctx, env.project, {"id": "build"}, {"run_id": run_id},
                                     {"title": "HelpDesk", "instructions": "build it"})


def _started(env):
    version = _publish(env, SPEC, "resume")
    run = _start_consented(env, version, RUN_INPUT)
    return run["id"], _failed_build(env, run["id"])


def test_a_persons_step_retry_that_resumes_a_saved_workspace_is_authorised_under_the_runs_grant(
        env, staffed):  # noqa: F811
    run_id, crun = _started(env)
    resumed, _, note = _resume(env, run_id)
    assert resumed is not None and note == "redirect to /dashboard"
    assert resumed["resumed_from_code_task_run_id"] == crun
    assert resumed["continues_from_workspace"] == 1
    new_run = resumed["run_id"]
    with env.engine.begin() as c:
        job = c.execute(text("SELECT id, on_behalf_of, grant_id, on_behalf_system FROM jobs "
                             "WHERE kind='code.task' AND payload->>'run_id' = :r"),
                        {"r": new_run}).mappings().one()
        run_grant = c.execute(text("SELECT grant_id FROM workflow_runs WHERE id=:r"),
                              {"r": run_id}).scalar()
        builder = runs.code_task_builder(
            {}, c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"),
                          {"i": new_run}).scalar())
    # Stamped as the person, under the grant the run was consented with — never BOSS itself.
    assert job["on_behalf_of"] == ALICE and not job["on_behalf_system"]
    assert run_grant and job["grant_id"] == run_grant
    # And the worker's start of that build is authorised (it was refused `no_grant`).
    started = runs.start_code_task_run(env.engine, task_run_id=new_run, project_id=env.project,
                                       owner="acme", repo="web", role_key=builder,
                                       job_id=job["id"])
    assert started is not None
    runs.finish_code_task_run(env.engine, started[0], org_id="org_boss")


def test_a_resume_with_no_valid_grant_still_asks_for_consent(env, staffed):  # noqa: F811
    run_id, _ = _started(env)
    with env.engine.begin() as c:
        grant_id = c.execute(text("SELECT grant_id FROM workflow_runs WHERE id=:r"),
                             {"r": run_id}).scalar()
        c.execute(text("UPDATE identity_delegation_grants SET status='revoked' WHERE id=:g"),
                  {"g": grant_id})
    with pytest.raises(runner.CapabilityExecutionError, match="renew"):
        _resume(env, run_id)
    with env.engine.begin() as c:
        queued = c.execute(text("SELECT count(*) FROM code_task_runs WHERE project_id=:p "
                                "AND finalize_ctx->>'parent_run_id' IS NOT NULL"),
                           {"p": env.project}).scalar()
    assert queued == 0, "nothing is queued for a run whose authority has ended"
