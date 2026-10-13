"""BOSS's own retries of a build run under the person's grant for that run, or are not started.

crun_2f76585e (a HelpDesk build of a workflow run the person consented to "for this run only")
failed on a GitHub 502; BOSS's one-shot infra retry started crun_da5f6f72 at once, and identity
refused it: "No one's authority covers this build: it was started by BOSS itself (or a key)".
ROOT CAUSE: the retry was started inside the worker's own request (its key, `sys_legacy_system`),
so carry_retry_authority saw a caller who was not the build's person and carried nothing; the
attempt was stamped with the worker's key and refused. crun_18eaa450 (the shepherd, an hour after
an upgrade, of a build that had died before it) was the same, with a grant that had already ended.

Every automatic caller (infra retry, shepherd, autonomous review-repair) now says it is BOSS
(retry_code_task(automatic=True)): the attempt carries the person's grant for the same run, never
wider, or BOSS does not start it and the build stays failed with Retry for a person. A build that
failed before this BOSS started is never retried automatically.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.identity import lineage, runs
from boss_engine.services import code_task_service as cts
from boss_engine.services import governed_code_task_service as governed
from boss_engine.services import jobs as jobs_service

from .test_a_resumed_build_runs_as_the_person import SPEC, _Jobs, _ports  # noqa: F401
from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import (  # noqa: F401
    RUN_INPUT, _publish, _start_consented, staffed)

#: The worker calling in with its own key: what the API sees while it runs a leased build.
WORKER_KEY = AuthContext(Principal("sys_legacy_system", PrincipalKind.SYSTEM, "org_boss",
                                   PrincipalStatus.ACTIVE, "worker"), AuthMethod.LEGACY_API_KEY)


def _workflow_build(env, *, error="git -c failed: Cloning into '.'...\nerror: RPC failed; HTTP 502"):
    """A build a consented workflow run started, failed `infra`, its job stamped as the person."""
    version = _publish(env, SPEC, f"auto_{uuid.uuid4().hex[:6]}")
    run = _start_consented(env, version, RUN_INPUT)
    with env.engine.begin() as c:
        wf = c.execute(text("SELECT grant_id, authority_run_id FROM workflow_runs WHERE id=:r"),
                       {"r": run["id"]}).mappings().one()
    crun = f"crun_{uuid.uuid4().hex[:12]}"
    person = lineage.Acting(principal_id=ALICE, org_id="org_boss", grant_id=wf["grant_id"],
                            run_id=wf["authority_run_id"])
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, state, "
            "finalize_ctx, error) VALUES (:id, :p, NULL, 'running', 'queued', 'HelpDesk', CAST(:spec AS JSONB), "
            "'{}'::jsonb, CAST(:fc AS JSONB), :e)"),
            {"id": crun, "p": env.project, "e": error,
             "spec": json.dumps({"instructions": "build it", "acceptance_criteria": [], "test_commands": [],
                                 "owner": "acme", "repo": "web", "base_branch": "main"}),
             "fc": json.dumps({"workflow_run_id": run["id"], "failure_class": "infra"})})
        with lineage.acting(person):                       # started by the person's consent
            assert jobs_service.enqueue_job(c, env.project, "code.task", {"run_id": crun})["id"]
        c.execute(text("UPDATE code_task_runs SET status='failed', phase='done' WHERE id=:r"), {"r": crun})
    return run["id"], crun, wf["grant_id"]


def _infra_retry(env, crun):
    with lineage.acting(WORKER_KEY):                       # as in production: the worker's key
        return governed._retry_after_infra_failure(env.engine, get_settings(), _Jobs(), crun,
                                                   env.project, {}, {"status": "failed"})


def _job_of(env, run_id):
    with env.engine.begin() as c:
        return c.execute(text("SELECT id, on_behalf_of, grant_id, on_behalf_system, payload FROM jobs "
                              "WHERE kind='code.task' AND payload->>'run_id'=:r"), {"r": run_id}).mappings().one()


def _children(env, crun) -> int:
    with env.engine.begin() as c:
        return c.execute(text("SELECT count(*) FROM code_task_runs WHERE finalize_ctx->>'parent_run_id'=:r"),
                         {"r": crun}).scalar()


def test_the_infra_retry_of_a_consented_workflow_build_runs_under_that_runs_grant(env, staffed):  # noqa: F811
    wf_run, crun, grant = _workflow_build(env)
    out = _infra_retry(env, crun)
    new = out.get("auto_retry_run_id")
    assert new, out
    job = _job_of(env, new)
    # The person, under the very grant of the run — never the worker's key, never BOSS itself.
    assert job["on_behalf_of"] == ALICE and job["grant_id"] == grant and not job["on_behalf_system"]
    # Scoped to the same run: a child of it, nothing wider.
    assert str(job["payload"]["_identity"]["run_id"]).startswith(f"run_wf:{wf_run}")
    # And the build that job runs is authorised (crun_da5f6f72 was refused `no_grant` here).
    with env.engine.begin() as c:
        builder = runs.code_task_builder({}, c.execute(text(
            "SELECT finalize_ctx FROM code_task_runs WHERE id=:i"), {"i": new}).scalar())
    started = runs.start_code_task_run(env.engine, task_run_id=new, project_id=env.project, owner="acme",
                                       repo="web", role_key=builder, job_id=job["id"])
    assert started is not None
    runs.finish_code_task_run(env.engine, started[0], org_id="org_boss")


def test_with_no_live_grant_boss_does_not_start_the_retry_and_leaves_it_to_a_person(env, staffed):  # noqa: F811
    _, crun, grant = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET status='expired' WHERE id=:g"), {"g": grant})
    out = _infra_retry(env, crun)
    assert out.get("auto_retry") == "needs_person" and "Retry" in out["next_step"]
    assert _children(env, crun) == 0, "nothing is queued for a build identity would refuse"
    with env.engine.begin() as c:
        said = c.execute(text("SELECT count(*) FROM events WHERE entity_id=:r "
                              "AND event_type='code_task.auto_retry_withheld'"), {"r": crun}).scalar()
        status = c.execute(text("SELECT status FROM code_task_runs WHERE id=:r"), {"r": crun}).scalar()
    assert said == 1 and status == "failed"


def test_a_build_stamped_as_boss_itself_carries_nothing(env, staffed):  # noqa: F811
    _, crun, _ = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE jobs SET on_behalf_of='sys_worker', grant_id=NULL, on_behalf_system=true, "
                       "payload = payload - '_identity' WHERE payload->>'run_id'=:r"), {"r": crun})
    with pytest.raises(cts.RetryNeedsPerson):
        with lineage.acting(WORKER_KEY):
            cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun, automatic=True)
    assert _children(env, crun) == 0


def test_a_this_build_only_grant_is_not_carried_past_its_build(env, staffed):  # noqa: F811
    _, crun, grant = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id=:t WHERE id=:g"),
                  {"t": f"run_ct:{crun}", "g": grant})
    with pytest.raises(cts.RetryNeedsPerson):
        with lineage.acting(WORKER_KEY):
            cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun, automatic=True)
    assert _children(env, crun) == 0


def test_a_build_that_failed_before_boss_started_is_not_retried_automatically(env, staffed, monkeypatch):  # noqa: F811
    """crun_566f8767 died (dead-lettered, 05:52); after the upgrade the shepherd retried it (06:57)."""
    _, crun, _ = _workflow_build(env)
    monkeypatch.setattr(cts, "PROCESS_STARTED_AT", cts.PROCESS_STARTED_AT.__class__.now(
        cts.PROCESS_STARTED_AT.tzinfo) + timedelta(minutes=5))     # BOSS restarted after the failure
    with pytest.raises(cts.RetryNeedsPerson, match="before BOSS last started"):
        with lineage.acting(WORKER_KEY):
            cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun, automatic=True)
    assert _children(env, crun) == 0
    # The shepherd's retry of it hands it to a person, once, and starts nothing.
    from boss_engine.services import shepherd_service

    assert shepherd_service._retry_run(env.engine, get_settings(), _Jobs(), crun, "timed out") == 0
    assert shepherd_service._retry_run(env.engine, get_settings(), _Jobs(), crun, "timed out") == 0
    with env.engine.begin() as c:
        asked = c.execute(text("SELECT count(*) FROM shepherd_escalations WHERE task_ref=:r AND status='open'"),
                          {"r": crun}).scalar()
    assert asked == 1 and _children(env, crun) == 0
    # A person's Retry is not BOSS's: it is still theirs to start.
    with lineage.acting(WORKER_KEY):
        assert cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun)["run_id"]


def test_every_automatic_retry_caller_says_it_is_boss():
    """The class, enforced: no path where BOSS decides to retry calls retry_code_task as a person
    would (the infra retry and the review-repair in the pipeline, both shepherd remedies)."""
    import inspect
    import re

    from boss_engine.services import shepherd_service

    found = 0
    for module in (governed, shepherd_service):
        source = inspect.getsource(module)
        for match in re.finditer(r"\bretry_code_task\(", source):
            depth = 0
            for end in range(match.end() - 1, len(source)):
                depth += {"(": 1, ")": -1}.get(source[end], 0)
                if depth == 0:
                    break
            call = source[match.start():end + 1]
            assert "automatic=True" in call, f"{module.__name__}: {call[:160]}"
            found += 1
    assert found >= 4
