"""A continuation BOSS starts for a lineage acts under the lineage's ORIGINAL person, or blocks
naming the missing grant.

Build E (wfr_122f8fa8, 2026-10-27): the Owner consented "For 30 days" (standing grant
dlg_rukox7ds, naming the builder). Its repair rounds (retry_code_task(automatic=True)) carried
that grant; then PR #5 opened with checks failing and BOSS started "Follow-up: checks failing"
crun_97784316 — whose job was stamped `sys_legacy_system`, no grant, and the build blocked "No
one's authority covers this build". crun_309ff6d1 (the follow-up of PR #4, a "for this run only"
workflow grant) was the same.

ROOT CAUSE: the follow-up is created inside the worker's own request (its key). It passed
`parent_run_id`, so start_code_task called carry_retry_authority — but as a PERSON's call
(by_boss=False), and the caller check refuses any caller who is not the build's person. The
grant was never consulted. Now the follow-up says it is BOSS continuing the lineage
(start_code_task(continuation=True)): the same carry an automatic retry gets — the original
person and grant, bound to the same run, never wider — or a blocked build that names the gap.
"""
from __future__ import annotations

import inspect
import re

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage, runs
from boss_engine.services import code_task_service as cts
from boss_engine.services import governed_code_task_service as governed

from .test_a_resumed_build_runs_as_the_person import _Jobs, _ports  # noqa: F401
from .test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all import (
    WORKER_KEY, _job_of, _workflow_build)
from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import staffed  # noqa: F401

FINDINGS = [{"severity": "high", "text": "the failing check `api tests` must pass"}]


def _shipped(env, crun):
    with env.engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status='completed', phase='done' WHERE id=:r"), {"r": crun})


def _follow_up(env, crun) -> str:
    """BOSS spawning the follow-up from the worker, as in production (the worker's key)."""
    with lineage.acting(WORKER_KEY):
        new = governed._spawn_deferred_findings_task(
            env.engine, get_settings(), _Jobs(), project_id=env.project, task_id=None,
            spec={"owner": "acme", "repo": "web"}, base_branch="boss/feature",
            findings=FINDINGS, parent_run_id=crun, parent_title="HelpDesk", parent_root=crun)
    assert new
    return new


def _fctx(env, run_id) -> dict:
    with env.engine.begin() as c:
        return c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"), {"i": run_id}).scalar() or {}


def _runs_as_person(env, new, grant):
    job = _job_of(env, new)
    assert job["on_behalf_of"] == ALICE and job["grant_id"] == grant and not job["on_behalf_system"], dict(job)
    builder = runs.code_task_builder({}, _fctx(env, new))
    started = runs.start_code_task_run(env.engine, task_run_id=new, project_id=env.project, owner="acme",
                                       repo="web", role_key=builder, job_id=job["id"])
    assert started is not None, "the follow-up's build is authorised"
    runs.finish_code_task_run(env.engine, started[0], org_id="org_boss")
    return job


def _blocked(env, new, monkeypatch, *, says: str):
    job = _job_of(env, new)
    assert job["on_behalf_of"] != ALICE and not job["grant_id"], "no person's authority was gained"
    assert says in str(_fctx(env, new).get("authority_gap") or ""), _fctx(env, new)
    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    with pytest.raises(runs.NoAuthority):
        runs.start_code_task_run(env.engine, task_run_id=new, project_id=env.project, owner="acme",
                                 repo="web", role_key=runs.code_task_builder({}, _fctx(env, new)),
                                 job_id=job["id"])


def test_a_standing_grant_carries_to_the_follow_up_boss_starts(env, staffed):  # noqa: F811
    wf_run, crun, grant = _workflow_build(env)
    with env.engine.begin() as c:                      # "For 30 days": not bound to any run
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id=NULL WHERE id=:g"), {"g": grant})
    _shipped(env, crun)
    new = _follow_up(env, crun)
    job = _runs_as_person(env, new, grant)
    assert str(job["payload"]["_identity"]["run_id"]).startswith(f"run_wf:{wf_run}")
    assert "authority_gap" not in _fctx(env, new)


def test_a_run_scoped_grant_carries_to_the_follow_up_in_the_same_lineage(env, staffed):  # noqa: F811
    wf_run, crun, grant = _workflow_build(env)         # "For this run only": bound to run_wf:<run>
    with env.engine.begin() as c:
        bound = c.execute(text("SELECT task_run_id FROM identity_delegation_grants WHERE id=:g"),
                          {"g": grant}).scalar()
    assert str(bound).startswith("run_wf:")
    _shipped(env, crun)
    job = _runs_as_person(env, _follow_up(env, crun), grant)
    assert str(job["payload"]["_identity"]["run_id"]).startswith(f"run_wf:{wf_run}/")


def test_a_key_started_build_s_follow_up_stays_blocked(env, staffed, monkeypatch):  # noqa: F811
    _, crun, _ = _workflow_build(env)
    with env.engine.begin() as c:                      # the build was started by a key, no grant
        c.execute(text("UPDATE jobs SET on_behalf_of='sys_legacy_system', grant_id=NULL, "
                       "on_behalf_system=false, payload = payload - '_identity' "
                       "WHERE payload->>'run_id'=:r"), {"r": crun})
    _shipped(env, crun)
    _blocked(env, _follow_up(env, crun), monkeypatch, says="started by BOSS itself or a key")


def test_a_revoked_grant_leaves_the_follow_up_blocked_and_says_which(env, staffed, monkeypatch):  # noqa: F811
    _, crun, grant = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id=NULL, status='revoked', "
                       "revoked_at=now() WHERE id=:g"), {"g": grant})
    _shipped(env, crun)
    _blocked(env, _follow_up(env, crun), monkeypatch, says=f"grant {grant}")


def test_a_this_build_only_grant_does_not_extend_to_a_follow_up(env, staffed, monkeypatch):  # noqa: F811
    _, crun, grant = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id=:t WHERE id=:g"),
                  {"t": f"run_ct:{crun}", "g": grant})
    _shipped(env, crun)
    _blocked(env, _follow_up(env, crun), monkeypatch, says="only")


def test_the_blocked_follow_up_names_the_missing_grant(env, staffed, monkeypatch):  # noqa: F811
    _, crun, grant = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET status='revoked', revoked_at=now() "
                       "WHERE id=:g"), {"g": grant})
    _shipped(env, crun)
    new = _follow_up(env, crun)

    def refused(*_a, **_k):
        raise cts.AuthorityRefused("no_grant")

    monkeypatch.setattr(cts, "_run_code_task", refused)
    out = cts._run_code_task_once(env.engine, get_settings(), _Jobs(), new)
    assert out["reason"] == "no_authority" and grant in out["message"], out
    with env.engine.begin() as c:
        row = c.execute(text("SELECT status, error FROM code_task_runs WHERE id=:i"), {"i": new}).one()
    assert row[0] == "blocked" and grant in row[1]


def test_a_caller_cannot_supply_the_gap(env, staffed):  # noqa: F811
    _, crun, _ = _workflow_build(env)
    with env.engine.begin() as c, lineage.acting(WORKER_KEY):
        new = cts.start_code_task(
            c, project_id=env.project, task_id=None, title="x", instructions="y",
            acceptance_criteria=[], test_commands=[], owner="acme", repo="web", base_branch="main",
            finalize_ctx={"authority_gap": "forged"}, jobs=_Jobs())
    assert "authority_gap" not in _fctx(env, new)


def test_every_follow_up_boss_spawns_says_it_continues_the_lineage():
    """The class: each start_code_task call in the follow-up spawner says continuation=True."""
    source = inspect.getsource(governed._spawn_deferred_findings_task)
    calls = [m for m in re.finditer(r"\bstart_code_task\(", source)]
    assert calls
    for match in calls:
        depth = 0
        for end in range(match.end() - 1, len(source)):
            depth += {"(": 1, ")": -1}.get(source[end], 0)
            if depth == 0:
                break
        assert "continuation=True" in source[match.start():end + 1]


def test_a_follow_up_belongs_to_its_parents_workflow_run(env, staffed):  # noqa: F811
    """helpdesk-ai 2026-11-06: the "checks failing" follow-up carried no workflow_run_id, so its
    merge approval never showed on the run ("how does the user know?"). It carries the parent's
    run, and still acts as the lineage's person under the run's grant."""
    wf_run, crun, grant = _workflow_build(env)
    _shipped(env, crun)
    with lineage.acting(WORKER_KEY):
        new = governed._spawn_deferred_findings_task(
            env.engine, get_settings(), _Jobs(), project_id=env.project, task_id=None,
            spec={"owner": "acme", "repo": "web"}, base_branch="boss/feature",
            findings=FINDINGS, parent_run_id=crun, parent_title="HelpDesk", parent_root=crun,
            checks_failing={"failed": ["written_tests_pass"]}, parent_workflow_run_id=wf_run)
    assert _fctx(env, new).get("workflow_run_id") == wf_run
    _runs_as_person(env, new, grant)
