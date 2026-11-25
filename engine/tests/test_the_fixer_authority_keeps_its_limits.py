"""THE FIXER AUTHORITY KEEPS ITS LIMITS (review of #264, identity/fix_authority.py).

Agents fix failures with the starter's authority by default — and never past these:
  * turning `agents_fix_failures` off stops work ALREADY standing on a fixer grant (read at
    every use, grants.usable), not only new grants;
  * a person who revokes the grant a run was started under ends that run's fixer authority;
  * a key the person took off their fixer grant is never given back;
  * a key no agent may hold (secret:lease …) is refused, never quietly granted;
  * a project in another org is never acted in.
Full-schema identity world (test_identity_agents_workflow_consent.env).
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from boss_engine.identity import consent, fix_authority, grants
from boss_engine.services import project_settings
from boss_engine.services import workflow_capability_runner as runner

from .test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all import (  # noqa: F401
    _workflow_build)
from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401
from .test_no_step_is_blocked_by_consent import staffed  # noqa: F401


def _moved_onto_fixer(env):
    wf_run, _crun, grant = _workflow_build(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET status='expired' WHERE id=:g"),
                  {"g": grant})
    with env.engine.begin() as c:
        acting = runner._live_run_authority(c, env.project, wf_run)
    assert acting is not None
    return wf_run, grant, acting


def test_turning_the_default_off_stops_a_run_already_on_its_fixer_grant(env, staffed):  # noqa: F811
    wf_run, _grant, acting = _moved_onto_fixer(env)
    with env.engine.begin() as c:
        project_settings.set_one(c, env.project, "agents_fix_failures", 0)
    with env.engine.begin() as c:
        assert consent.acting_for_workflow_run(c, wf_run) is None, "read at every use"
        assert not grants.usable(grants.load(acting.grant_id, db=c), db=c)
        assert runner._live_run_authority(c, env.project, wf_run) is None
    with env.engine.begin() as c:
        project_settings.set_one(c, env.project, "agents_fix_failures", 1)
    with env.engine.begin() as c:
        assert consent.acting_for_workflow_run(c, wf_run) is not None, "back on, it acts again"


def test_revoking_the_runs_original_grant_ends_its_fixer_authority_mid_run(env, staffed):  # noqa: F811
    wf_run, grant, _acting = _moved_onto_fixer(env)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET status='revoked' WHERE id=:g"),
                  {"g": grant})
    with env.engine.begin() as c:
        assert consent.acting_for_workflow_run(c, wf_run) is None
        assert runner._live_run_authority(c, env.project, wf_run) is None, "not re-created"


def test_a_key_the_person_removed_is_never_given_back(env, staffed):  # noqa: F811
    wf_run, _grant, acting = _moved_onto_fixer(env)
    with env.engine.begin() as c:
        fixer = grants.load(acting.grant_id, db=c)
        removed = sorted(fixer.permissions)[0]
        c.execute(text("DELETE FROM identity_delegation_grant_permissions WHERE grant_id = :g "
                       "AND permission_key = :k"), {"g": fixer.id, "k": removed})
        c.execute(text("UPDATE identity_delegation_grants SET resources = resources || "
                       "jsonb_build_object('fixer', jsonb_build_object('withheld', "
                       "jsonb_build_array(CAST(:k AS text)))) WHERE id = :g"),
                  {"g": fixer.id, "k": removed})
    gap: list[str] = []
    with env.engine.begin() as c:
        again = fix_authority.ensure(c, person_id=ALICE, project_id=env.project,
                                     agent_ids=list(fixer.agent_ids), permissions=[removed],
                                     gap=gap)
    assert again is None and "does not give it back" in gap[-1]


def test_a_key_no_agent_may_hold_is_refused_not_granted(env, staffed):  # noqa: F811
    env.world.grant(ALICE, {"secret:lease"}, env.project)
    _wf, _g, acting = _moved_onto_fixer(env)
    gap: list[str] = []
    with env.engine.begin() as c:
        fixer = grants.load(acting.grant_id, db=c)
        got = fix_authority.ensure(c, person_id=ALICE, project_id=env.project,
                                   agent_ids=list(fixer.agent_ids), permissions=["secret:lease"],
                                   gap=gap)
        kept = grants.load(acting.grant_id, db=c)
    assert got is None and "secret:lease" in gap[-1]
    assert "secret:lease" not in kept.permissions


def test_a_project_in_another_org_is_never_acted_in(env, staffed):  # noqa: F811
    other = f"p_{uuid.uuid4().hex[:10]}"
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_other', 'Other') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_other', "
                       "'org_other', 'ws') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_other', 'o', :p)"), {"p": other})
    gap: list[str] = []
    try:
        with env.engine.begin() as c:
            got = fix_authority.ensure(c, person_id=ALICE, project_id=other,
                                       agent_ids=["agt_forgewf0001"],
                                       permissions=["code_task:read"], gap=gap)
        assert got is None and "another org" in gap[-1]
    finally:
        with env.engine.begin() as c:
            c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": other})
