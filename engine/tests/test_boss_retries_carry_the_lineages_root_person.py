"""BOSS's retry of a run BOSS itself stamped carries the lineage's person, or names the gap.

crun_ef2252c5 (2026-10-27): the Engineering Captain (a BOSS agent, under the install key
`sys_legacy_console`) retried crun_97784316, a follow-up stamped `sys_legacy_system` before
0e74dffb. Its lineage root crun_53e4ae12 ran as the Owner under standing grant dlg_rukox7ds, yet the
retry blocked "No one's authority covers this build": carry_retry_authority treated the key as a
person who was not the build's (the caller check), and only ever looked at the direct parent,
which carries nothing. Now a SYSTEM caller is BOSS deciding, and a BOSS-stamped parent resolves
to the lineage's nearest person-started build, under the same rules (never wider; a "this build
only" grant ends with its build; a lineage with no person behind it carries nothing).
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.identity import lineage, runs
from boss_engine.services import code_task_service as cts
from boss_engine.services import jobs as jobs_service

from .test_a_resumed_build_runs_as_the_person import _Jobs, _ports  # noqa: F401
from .test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all import (
    WORKER_KEY, _job_of, _workflow_build)
from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import staffed  # noqa: F401

#: A BOSS agent over MCP under the install key: what the API sees for the Captain's retry.
INSTALL_KEY = AuthContext(Principal("sys_legacy_console", PrincipalKind.SYSTEM, "org_boss",
                                    PrincipalStatus.ACTIVE, "console"), AuthMethod.LEGACY_API_KEY)


def _system_follow_up(env, root: str) -> str:
    """A follow-up of `root` stamped as the worker's key (as crun_97784316 was), now failed."""
    run = f"crun_{uuid.uuid4().hex[:12]}"
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, state, "
            "finalize_ctx, error) VALUES (:id, :p, NULL, 'running', 'queued', 'Follow-up: checks failing', "
            "CAST(:spec AS JSONB), '{}'::jsonb, CAST(:fc AS JSONB), 'blocked')"),
            {"id": run, "p": env.project,
             "spec": json.dumps({"instructions": "fix the checks", "acceptance_criteria": [], "test_commands": [],
                                 "owner": "acme", "repo": "web", "base_branch": "boss/feature"}),
             "fc": json.dumps({"is_followup": True, "parent_run_id": root, "root_run_id": root})})
        with lineage.acting(WORKER_KEY):
            assert jobs_service.enqueue_job(c, env.project, "code.task", {"run_id": run})["id"]
        c.execute(text("UPDATE code_task_runs SET status='failed', phase='done' WHERE id=:r"), {"r": run})
    assert _job_of(env, run)["on_behalf_of"] == "sys_legacy_system"
    return run


def _captain_retry(env, run) -> str:
    with lineage.acting(INSTALL_KEY):
        return cts.retry_code_task(env.engine, get_settings(), _Jobs(), run)["run_id"]


def _fctx(env, run_id) -> dict:
    with env.engine.begin() as c:
        return c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"), {"i": run_id}).scalar() or {}


def _standing(env, grant):
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id=NULL WHERE id=:g"), {"g": grant})


def test_an_agent_retry_of_a_system_stamped_follow_up_carries_the_root_persons_standing_grant(env, staffed):  # noqa: F811
    wf_run, root, grant = _workflow_build(env)
    _standing(env, grant)
    new = _captain_retry(env, _system_follow_up(env, root))
    job = _job_of(env, new)
    assert job["on_behalf_of"] == ALICE and job["grant_id"] == grant and not job["on_behalf_system"], dict(job)
    assert str(job["payload"]["_identity"]["run_id"]).startswith(f"run_wf:{wf_run}/retry:")
    started = runs.start_code_task_run(env.engine, task_run_id=new, project_id=env.project, owner="acme",
                                       repo="web", role_key=runs.code_task_builder({}, _fctx(env, new)),
                                       job_id=job["id"])
    assert started is not None
    runs.finish_code_task_run(env.engine, started[0], org_id="org_boss")


def test_a_key_rooted_lineage_stays_blocked(env, staffed, monkeypatch):  # noqa: F811
    _, root, grant = _workflow_build(env)
    _standing(env, grant)                              # the person HAS a grant; the lineage is not theirs
    with env.engine.begin() as c:
        c.execute(text("UPDATE jobs SET on_behalf_of='sys_legacy_system', grant_id=NULL, "
                       "on_behalf_system=false, payload = payload - '_identity' "
                       "WHERE payload->>'run_id'=:r"), {"r": root})
    new = _captain_retry(env, _system_follow_up(env, root))
    job = _job_of(env, new)
    assert job["on_behalf_of"] != ALICE and not job["grant_id"]
    assert "every build before it in its lineage" in str(_fctx(env, new).get("authority_gap"))
    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    import pytest

    with pytest.raises(runs.NoAuthority):
        runs.start_code_task_run(env.engine, task_run_id=new, project_id=env.project, owner="acme",
                                 repo="web", role_key=runs.code_task_builder({}, _fctx(env, new)),
                                 job_id=job["id"])


def test_a_revoked_root_grant_blocks_with_the_gap_named(env, staffed):  # noqa: F811
    _, root, grant = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id=NULL, status='revoked', "
                       "revoked_at=now() WHERE id=:g"), {"g": grant})
    new = _captain_retry(env, _system_follow_up(env, root))
    job = _job_of(env, new)
    assert job["on_behalf_of"] != ALICE and not job["grant_id"]
    gap = str(_fctx(env, new).get("authority_gap"))
    assert grant in gap and root in gap, gap


def test_a_this_build_only_root_grant_does_not_reach_a_retry_of_its_follow_up(env, staffed):  # noqa: F811
    _, root, grant = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id=:t WHERE id=:g"),
                  {"t": f"run_ct:{root}", "g": grant})
    new = _captain_retry(env, _system_follow_up(env, root))
    assert not _job_of(env, new)["grant_id"]
    assert "only" in str(_fctx(env, new).get("authority_gap"))
