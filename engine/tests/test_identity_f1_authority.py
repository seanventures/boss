"""F1 · NO GRANT IS AN ERROR, children of runs, and the revoke hard stop.

Findings #2, #5, #6, #7, #8 and the revoke hard stop. Each test fails without the fix it names:
  #5  a build started with a PAT/API key needs a grant of the caller's own (409 under enforce);
  #6  a retry/resume/failover re-enqueued by the system or the worker keeps the authority the build
      was STARTED under, never sys_worker (and so never the shared token);
  #7  a build inside a delegated run is a CHILD hop with every spawn check, never a fresh first hop;
  #8  a job that acts on someone's behalf with no principal fails closed at enqueue (enforce);
      no principal, a system principal, no Passport or no egress proxy -> NoAuthority (enforce);
  #2  a child workflow run inherits its parent's authority only for that authority's own person;
  HS  revoking a grant stops a running build MID-STAGE: its processes die at once (same process,
      after commit) or within seconds (another process, by polling) — not at the next boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import text

from boss_engine.identity import cascade, consent, grants, lineage, runs, tokens
from boss_engine.identity.delegation import AUD_API, AUD_EGRESS, CascadeTrigger
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

from ._identity_agents_support import BASE, build_env, install_github, session_ctx, start


@pytest.fixture()
def env(monkeypatch):
    e = build_env(monkeypatch)
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    with e.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id, project_id) VALUES ('r-forge', 'forge', :a, :p)"),
                  {"a": e.forge, "p": e.project})
    return e


def _mode(monkeypatch, mode: str) -> None:
    """The mode lives in the ONE settings object the guard and lineage read (F2), not os.environ."""
    from boss_engine.config import get_settings

    monkeypatch.setenv("BOSS_AUTHZ_MODE", mode)
    monkeypatch.setattr(get_settings(), "authz_mode", mode)


def _enforce(monkeypatch):
    _mode(monkeypatch, "enforce")


def _key_ctx(pid: str, kind=PrincipalKind.USER) -> AuthContext:
    return AuthContext(Principal(pid, kind, "org_boss", PrincipalStatus.ACTIVE, pid), AuthMethod.API_KEY)


def _job(env, job_id, run_id, who):
    with env.db() as c:
        payload, cols = lineage.stamp_job_row({"run_id": run_id}, who, kind="code.task")
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload) VALUES (:j, :p, 'code.task', CAST(:pl AS JSONB))"),
                  {"j": job_id, "p": env.project, "pl": json.dumps(payload)})


# ---- #5 PAT / API key / MCP-started builds ---------------------------------------------------------------

def test_5_a_key_started_build_needs_a_grant_of_the_callers_own(env, monkeypatch):
    _enforce(monkeypatch)
    with env.db() as c, lineage.acting(_key_ctx(env.alice)):
        with pytest.raises(consent.ConsentRequired) as e:
            consent.require_authority(org_id=None, project_id=env.project, role_keys=["forge"],
                                      permissions=consent.CODE_TASK_PERMISSIONS, repos=["acme/web"],
                                      task="via MCP", run_id="run_ct:crun_k1", db=c)
    assert e.value.body["error"] == "consent_required" and e.value.body["task_run_id"] is None
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    with env.db() as c, lineage.acting(_key_ctx(env.alice)):
        acting = consent.require_authority(org_id=None, project_id=env.project, role_keys=["forge"],
                                           permissions=consent.CODE_TASK_PERMISSIONS, repos=["acme/web"],
                                           task="via MCP", run_id="run_ct:crun_k1", db=c)
    assert acting is not None and acting.grant_id == g.id and acting.principal_id == env.alice


def test_5_in_shadow_a_key_without_a_grant_is_logged_loudly_not_silently(env, monkeypatch, caplog):
    _mode(monkeypatch, "shadow")
    with env.db() as c, lineage.acting(_key_ctx(env.alice)):
        assert consent.require_authority(org_id=None, project_id=env.project, role_keys=["forge"],
                                         permissions=consent.CODE_TASK_PERMISSIONS, repos=["acme/web"],
                                         task="via MCP", run_id="run_ct:crun_k2", db=c) is None
    assert any("SHADOW" in r.getMessage() and "enforce" in r.getMessage() for r in caplog.records)


# ---- #6 retry / resume / failover keep the grant -----------------------------------------------------------

def test_6_a_build_re_enqueued_by_the_worker_keeps_the_authority_it_was_started_under(env, monkeypatch):
    install_github(monkeypatch, env)
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    alice = lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id=g.id)
    _job(env, "job_orig", "crun_r1", alice)                          # the start
    _job(env, "job_retry", "crun_r1", lineage.SYSTEM_ACTING)         # a system retry
    with lineage.acting(lineage.SYSTEM_ACTING):                      # the worker calling in
        started = runs.start_code_task_run(env.engine, task_run_id="crun_r1", project_id=env.project,
                                           owner="acme", repo="web", role_key="forge", job_id="job_retry")
    assert started is not None
    shim, _ = started
    with env.db() as c:
        row = c.execute(text("SELECT grant_id, subject_principal_id FROM identity_run_credentials WHERE jti = :j"),
                        {"j": shim.tokens[AUD_API].jti}).first()
    assert tuple(row) == (g.id, env.alice)
    runs.finish_code_task_run(env.engine, shim, org_id="org_boss")


def test_6_another_person_retrying_does_not_borrow_the_original_grant(env, monkeypatch):
    _enforce(monkeypatch)
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    _job(env, "job_o2", "crun_r2", lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id=g.id))
    with env.db() as c:
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES ('usr_bob', 'org_boss', 'user', 'active', 'Bob')"))
    with lineage.acting(lineage.Acting(principal_id="usr_bob", org_id="org_boss")):
        with pytest.raises(runs.NoAuthority):
            runs.start_code_task_run(env.engine, task_run_id="crun_r2", project_id=env.project, owner="acme",
                                     repo="web", role_key="forge")


# ---- #8 no principal / system / no passport / no egress ----------------------------------------------------

@pytest.mark.parametrize("case", ["no_principal", "system", "no_passport", "no_egress"])
def test_8_no_grant_is_an_error_under_enforcement_never_the_shared_token(env, monkeypatch, case):
    _enforce(monkeypatch)
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    who = {"no_principal": None, "system": lineage.SYSTEM_ACTING}.get(
        case, lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id=g.id))
    role = "nobody" if case == "no_passport" else "forge"
    if case == "no_egress":
        monkeypatch.delenv("BOSS_EGRESS_URL", raising=False)
    with pytest.raises(runs.NoAuthority):
        runs.start_code_task_run(env.engine, task_run_id=f"crun_n_{case}", project_id=env.project, owner="acme",
                                 repo="web", role_key=role, acting=who)


def test_8_a_delegated_job_with_no_principal_fails_closed_at_enqueue(monkeypatch):
    _mode(monkeypatch, "enforce")
    with pytest.raises(lineage.PrincipalRequired):
        lineage.stamp_job_row({"run_id": "crun_x"}, None, kind="code.task")
    # BOSS's own housekeeping stays BOSS's own work
    payload, cols = lineage.stamp_job_row({}, None, kind="deployment.health_sweep")
    assert cols["on_behalf_system"] is True
    _mode(monkeypatch, "shadow")
    payload, cols = lineage.stamp_job_row({"run_id": "crun_x"}, None, kind="code.task")
    assert cols["on_behalf_system"] is True and cols["grant_id"] is None   # holds nothing


# ---- #7 a build inside a delegated run is a child hop ---------------------------------------------------

def _parent_run(env, *, children):
    g = env.grant(agents=(env.atlas,), children=children, repos=("acme/web",))
    shim = start(env, g.id, agent=env.atlas, run="run_ag:loop1", repos=("acme/web",),
                 github_needs={"contents": "write", "pull_requests": "write"})
    with env.db() as c:
        url = BASE + "/internal/run"
        claims = tokens.verify_run_token(shim.tokens[AUD_API].access_token, audience=AUD_API,
                                         dpop_proof=shim.key.proof("POST", url, access_token=shim.tokens[AUD_API].access_token),
                                         htm="POST", htu=url, db=c)
        ctx = tokens.context_for(claims, db=c)
    return g, shim, lineage.Acting.from_context(ctx)


def test_7_a_build_in_a_delegated_run_is_minted_as_a_child_with_the_spawn_checks(env, monkeypatch):
    install_github(monkeypatch, env)
    g, parent, acting = _parent_run(env, children=(env.forge,))
    shim, access = runs.start_code_task_run(env.engine, task_run_id="crun_c1", project_id=env.project,
                                            owner="acme", repo="web", role_key="forge", acting=acting)
    with env.db() as c:
        row = c.execute(text("SELECT chain, depth, parent_jti, run_id FROM identity_run_credentials WHERE jti = :j"),
                        {"j": shim.tokens[AUD_EGRESS].jti}).first()
    assert [h.split("#")[0] for h in row[0]] == [env.atlas, env.forge] and row[1] == 2
    assert row[2] == parent.tokens[AUD_API].jti and row[3] == "run_ag:loop1/ct:crun_c1"
    assert shim.egress_credential and access.clone_url.endswith("/github/acme/web.git")


def test_7_a_child_the_parent_may_not_spawn_is_refused(env, monkeypatch):
    install_github(monkeypatch, env)
    _enforce(monkeypatch)
    g, parent, acting = _parent_run(env, children=(env.vera,))          # forge is not an allowed child
    with pytest.raises(tokens.TokenError):
        runs.start_code_task_run(env.engine, task_run_id="crun_c2", project_id=env.project, owner="acme",
                                 repo="web", role_key="forge", acting=acting)


def test_7_a_child_of_a_run_that_is_not_live_is_refused(env, monkeypatch):
    install_github(monkeypatch, env)
    _enforce(monkeypatch)
    g, parent, acting = _parent_run(env, children=(env.forge,))
    with env.db() as c:
        runs.end_run(parent.run_id, db=c, org_id="org_boss")
    with pytest.raises(runs.NoAuthority):
        runs.start_code_task_run(env.engine, task_run_id="crun_c3", project_id=env.project, owner="acme",
                                 repo="web", role_key="forge", acting=acting)


# ---- the revoke hard stop ---------------------------------------------------------------------------------

def _running_build(crun: str):
    from boss_engine.services import owned_process

    with owned_process.owner_scope("code_run", crun):
        # a stage that is past its last boundary: one long command, nothing checks a stop request
        return owned_process.popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _gone(proc, seconds=5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def test_revoking_a_grant_kills_a_running_build_mid_stage_in_this_process(env, monkeypatch):
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    shim = start(env, g.id, agent=env.forge, run="run_ct:crun_hs1")
    runs.remember_engine(env.engine)                 # the process running builds installs the hook
    proc = _running_build("crun_hs1")
    try:
        assert proc.poll() is None
        with env.db() as c:
            cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(env.alice),
                                   reason="stop now", db=c)
        assert _gone(proc), "the build's process survived the revoke"
    finally:
        proc.kill()
    del shim


def test_a_revoke_from_another_process_stops_the_build_within_seconds(env, monkeypatch):
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    shim = start(env, g.id, agent=env.forge, run="run_ct:crun_hs2")
    monkeypatch.setattr(cascade, "AFTER_COMMIT", [])  # "another process": no hook here
    proc = _running_build("crun_hs2")
    try:
        with env.db() as c:
            cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(env.alice),
                                   reason="stop now", db=c)
        assert proc.poll() is None
        stopped = runs.enforce_revocations(env.engine)   # the supervisor's 2-second poll
        assert "run_ct:crun_hs2" in stopped and _gone(proc)
    finally:
        proc.kill()
    del shim


def test_a_legacy_path_build_with_no_token_is_stopped_too(env, monkeypatch):
    monkeypatch.setattr(cascade, "AFTER_COMMIT", [])
    with env.db() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id) VALUES ('crun_hs3', :p)"), {"p": env.project})
    proc = _running_build("crun_hs3")
    try:
        with env.db() as c:
            cascade._halt_code_task("crun_hs3", "authority revoked: test", c)
        assert "run_ct:crun_hs3" in runs.enforce_revocations(env.engine) and _gone(proc)
    finally:
        proc.kill()


# ---- #2 a child workflow run inherits only for its own person ----------------------------------------------

def test_2_a_child_run_cannot_borrow_someone_elses_parent_authority(env, monkeypatch):
    from boss_engine.identity import consent as cs

    alice_acting = lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id="dlg_whatever",
                                  run_id="run_wf:wfr_parent")
    monkeypatch.setattr(cs, "recorded_authority", lambda db, rid: alice_acting)
    monkeypatch.setattr(cs, "acting_for_workflow_run", lambda db, rid: alice_acting)
    with env.db() as c:
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES ('usr_mallory2', 'org_boss', 'user', 'active', 'M')"))
    kw = dict(project_id=env.project, spec={}, input_data={}, run_id="wfr_child", parent_run_id="wfr_parent")
    with env.db() as c, lineage.acting(session_ctx("usr_mallory2")):
        with pytest.raises(cs.ParentNotYours):
            cs.require_for_workflow(c, **kw)
    with env.db() as c, lineage.acting(session_ctx(env.alice)):
        assert cs.require_for_workflow(c, **kw).run_id == "run_wf:wfr_parent/wf:wfr_child"
    with env.db() as c:                                                   # the worker continuing it
        assert cs.require_for_workflow(c, **kw).principal_id == env.alice


def test_2_the_http_door_runs_the_readiness_preflight_for_child_runs_too():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "apps/api/app/main.py").read_text(encoding="utf-8")
    door = src[src.index("def start_workflow_run(payload: WorkflowRunStartRequest)"):]
    door = door[:door.index("workflow_runtime.start_run(")]
    assert "_delivery_readiness_or_409(" in door and "if not payload.parent_run_id" not in door
