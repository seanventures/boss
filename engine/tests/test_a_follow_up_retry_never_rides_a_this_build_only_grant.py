"""A follow-up's automatic retry never rides its parent's "this build only" grant (review of #222).

BOSS's retry of a build carries the person's "this build only" grant for THAT build
(consent.carry_retry_authority, same_build). A follow-up BOSS started itself (its own spec,
stamped as BOSS) is another build: the lineage walk reaches the parent's grant, and carrying it
would let BOSS run different work under a consent given for one build. Its retry is refused
(RetryNeedsPerson) and waits for the person's OK, or runs under a standing grant they gave.
"""
import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage
from boss_engine.services import code_task_service as cts
from boss_engine.services import jobs as jobs_service

from .test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all import (  # noqa: F401
    ALICE, WORKER_KEY, _Jobs, _ports, _standalone_build, env, staffed)


def _follow_up(env, crun):
    """A follow-up BOSS started itself for `crun` (its own spec), failed on a platform error."""
    fup = f"crun_{uuid.uuid4().hex[:12]}"
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, state, "
            "finalize_ctx, error) VALUES (:id, :p, NULL, 'running', 'queued', 'Follow-up', "
            "CAST(:spec AS JSONB), '{}'::jsonb, CAST(:fc AS JSONB), 'git failed: HTTP 502')"),
            {"id": fup, "p": env.project,
             "spec": json.dumps({"instructions": "SOMETHING ELSE ENTIRELY", "acceptance_criteria": [],
                                 "test_commands": [], "owner": "acme", "repo": "web",
                                 "base_branch": "main"}),
             "fc": json.dumps({"parent_run_id": crun, "root_run_id": crun, "failure_class": "infra"})})
        with lineage.acting(WORKER_KEY):
            jobs_service.enqueue_job(c, env.project, "code.task", {"run_id": fup})
        c.execute(text("UPDATE jobs SET on_behalf_of='sys_worker', grant_id=NULL, on_behalf_system=true, "
                       "payload = payload - '_identity' WHERE payload->>'run_id'=:r"), {"r": fup})
        c.execute(text("UPDATE code_task_runs SET status='failed', phase='done' WHERE id=:r"), {"r": fup})
    return fup


def _children(env, run_id):
    with env.engine.begin() as c:
        return c.execute(text("SELECT count(*) FROM code_task_runs WHERE "
                              "finalize_ctx->>'parent_run_id' = :r"), {"r": run_id}).scalar()


def test_a_boss_stamped_follow_up_retry_does_not_ride_the_parents_this_build_only_grant(env, staffed):  # noqa: F811
    """With the org default OFF (agents_fix_failures = 0): refused, as before."""
    from boss_engine.services import project_settings

    crun, _grant = _standalone_build(env)                   # person's build, "this build only"
    fup = _follow_up(env, crun)
    with env.engine.begin() as c:
        project_settings.set_one(c, env.project, "agents_fix_failures", 0)
    try:
        with pytest.raises(cts.RetryNeedsPerson):
            with lineage.acting(WORKER_KEY):
                cts.retry_code_task(env.engine, get_settings(), _Jobs(), fup, automatic=True)
        assert _children(env, fup) == 0
    finally:
        with env.engine.begin() as c:
            project_settings.clear_one(c, env.project, "agents_fix_failures")


def test_by_default_the_follow_up_retry_runs_on_the_persons_fixer_authority_not_the_one_build_grant(
        env, staffed):  # noqa: F811
    """THE ORG DEFAULT (identity/fix_authority, operator 2026-11-21): BOSS's retry of the person's
    lineage runs with that person's own authority — on their FIXER grant (org default, audited),
    never on the consent they gave for one other build."""
    crun, grant = _standalone_build(env)
    fup = _follow_up(env, crun)
    with lineage.acting(WORKER_KEY):
        new = cts.retry_code_task(env.engine, get_settings(), _Jobs(), fup, automatic=True)["run_id"]
    with env.engine.begin() as c:
        job = c.execute(text("SELECT on_behalf_of, grant_id FROM jobs WHERE kind='code.task' "
                             "AND payload->>'run_id'=:r"), {"r": new}).mappings().one()
        kind, purpose = c.execute(text("SELECT authority_kind, purpose FROM "
                                       "identity_delegation_grants WHERE id = :g"),
                                  {"g": job["grant_id"]}).one()
    audited = sum(1 for e in env.world.events          # the audit log (faked in this world)
                  if e.event_type == "delegation.created" and e.grant_id == job["grant_id"]
                  and (e.details or {}).get("org_default") == "agents_fix_failures")
    assert job["on_behalf_of"] == ALICE and job["grant_id"] != grant
    assert kind == "fixer" and "org default" in purpose and audited == 1


def test_under_a_standing_grant_boss_retries_the_follow_up_by_itself(env, staffed):  # noqa: F811
    crun, grant = _standalone_build(env)
    with env.engine.begin() as c:                           # the person gave it 30 days instead
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id = NULL WHERE id = :g"),
                  {"g": grant})
    fup = _follow_up(env, crun)
    with lineage.acting(WORKER_KEY):
        new = cts.retry_code_task(env.engine, get_settings(), _Jobs(), fup, automatic=True)["run_id"]
    with env.engine.begin() as c:
        job = c.execute(text("SELECT on_behalf_of, grant_id FROM jobs WHERE kind='code.task' "
                             "AND payload->>'run_id'=:r"), {"r": new}).mappings().one()
    assert job["on_behalf_of"] == ALICE and job["grant_id"] == grant
