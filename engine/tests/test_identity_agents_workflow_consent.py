"""S3 · Consent at WORKFLOW-RUN start, the run's authority on worker-started work, and schedules.

Against the FULL schema (workflow_runs, workflow_schedules, jobs, roles are real), in a throwaway
database named by BOSS_IDENTITY_FULL_TEST_DATABASE_URL. The other streams are faked through their
frozen signatures (as in the other S3 suites).

  - a person starting a workflow run without a covering grant gets 409 consent_required;
  - with a grant, the run records it, and the jobs and builds the worker starts for it carry it;
  - revoking the grant mid-run halts the run (and its build, and cancels its queued work);
  - a schedule without a standing grant is refused; a schedule whose grant ends pauses.
"""
from __future__ import annotations


import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.identity import cascade, consent, grants, lineage, passports, runs, signing_keys, tokens, vault
from boss_engine.identity.delegation import CascadeTrigger, GrantRequest

from ._identity_agents_support import MASTER_KEY, ISSUER, World, install_fakes, session_ctx

URL_ENV = "BOSS_IDENTITY_FULL_TEST_DATABASE_URL"
ALICE = "usr_alice_wf"
FORGE = "agt_forgewf0001"
ATLAS = "agt_atlaswf0001"
NEEDS = consent.CODE_TASK_PERMISSIONS | consent.WORKFLOW_BASE_PERMISSIONS | {"code_task:create"}


@pytest.fixture()
def env(monkeypatch):
    url = os.environ.get(URL_ENV)
    if not url:
        pytest.skip(f"{URL_ENV} not set (a throwaway Postgres with the full schema)")
    assert "test" in url.rsplit("/", 1)[-1]
    monkeypatch.setenv("BOSS_MASTER_KEY", MASTER_KEY)
    monkeypatch.setenv("BOSS_PUBLIC_URL", ISSUER)
    monkeypatch.delenv("BOSS_AUTHZ_MODE", raising=False)
    vault.reset_master_key_cache()
    signing_keys.clear_cache()
    monkeypatch.setattr(cascade, "AFTER_COMMIT", [])
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://"), pool_size=4)
    world = World()
    install_fakes(monkeypatch, world)
    project = f"p_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("TRUNCATE identity_security_events, identity_egress_credentials, identity_run_credentials, "
                       "identity_delegation_grant_permissions, identity_delegation_grants, identity_agent_ceilings, "
                       "identity_agent_passports, identity_signing_keys, identity_vault_items, identity_vault_keks, "
                       "identity_dpop_replay, identity_principals CASCADE"))
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_boss', 'BOSS') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_wf', 'org_boss', 'ws') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_wf', 'wf', :p)"), {"p": project})
    with engine.begin() as c:
        vault.check_master_key(db=c)
        signing_keys.provision(org_id="org_boss", db=c)
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:u, 'org_boss', 'user', 'active', 'Alice')"), {"u": ALICE})
        for aid, rk in ((FORGE, "forge"), (ATLAS, "atlas")):
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) "
                           "VALUES (:i, :p, :r, :r, '{}'::jsonb, :a)"),
                      {"i": f"role_{uuid.uuid4().hex[:8]}", "p": project, "r": rk, "a": aid})
            passports.ensure_passport(org_id="org_boss", agent_id=aid, role_key=rk, display_name=rk.capitalize(),
                                      sponsor_principal_id=ALICE, db=c)
    world.grant(ALICE, set(NEEDS) | {"delegation:grant"}, project)
    world.grant(ALICE, {"delegation:grant"})

    class E:
        pass

    e = E()
    e.engine, e.world, e.project = engine, world, project
    yield e
    with engine.begin() as c:
        for table in ("jobs", "workflow_step_runs", "workflow_runs", "workflow_schedules", "workflow_versions",
                      "workflow_definitions", "events", "roles"):
            try:
                c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project})
            except Exception:  # noqa: BLE001
                pass
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project})
    engine.dispose()


def _workflow(c, project):
    from boss_engine.services import workflows

    definition = workflows.create_definition(c, project_id=project, workflow_key=f"wf_{uuid.uuid4().hex[:6]}",
                                             name="Ship it", description=None, actor="test", namespace="product")
    version = workflows.publish_version(c, project_id=project, definition_id=definition["id"], actor="test", spec={
        "entry_step": "b", "steps": [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}}, "next": []}]})
    return definition, version


def _start(c, project, version_id, *, as_person=True, **kw):
    from boss_engine.services import workflows

    call = lambda: workflows.start_run(c, project_id=project, actor="ceo",  # noqa: E731
                                       input_data={"title": "Interview Scorecard", "repository": "acme/web"},
                                       dedupe_key=f"dk_{uuid.uuid4().hex[:8]}", workflow_version_id=version_id, **kw)
    if as_person:
        with lineage.acting(session_ctx(ALICE)):
            return call()
    return call()


def _grant(env, *, kind="standing", task_run_id=None):
    req = GrantRequest(agent_ids=(FORGE,), permissions=frozenset(NEEDS), project_ids=(env.project,),
                       resources={"github": {"repos": ["acme/web"], "contents": "write", "pull_requests": "write"}},
                       task_run_id=task_run_id, purpose="Interview Scorecard")
    with env.engine.begin() as c:
        return grants.create_grant(session_ctx(ALICE), req, db=c)


def test_a_person_starting_a_workflow_run_without_a_grant_gets_409_and_nothing_is_written(env):
    with env.engine.begin() as c:
        _, version = _workflow(c, env.project)
    with env.engine.begin() as c, pytest.raises(consent.ConsentRequired) as e:
        _start(c, env.project, version["id"])
    body = e.value.body
    assert body["error"] == "consent_required" and body["scope"] == "workflow_run"
    assert [a["id"] for a in body["agents"]] == [FORGE] and set(body["permissions"]) == set(NEEDS)
    assert body["github"]["repos"] == ["acme/web"] and body["durations"][0]["label"] == "For this run only"
    assert body["task_run_id"].startswith("pending:")
    with env.engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"), {"p": env.project}).scalar() == 0
    # Caller-supplied identity in the input or the policy snapshot is never kept.
    g = _grant(env)
    with env.engine.begin() as c:
        run = _start(c, env.project, version["id"], policy_snapshot={"_identity": {"on_behalf_of": "usr_root",
                                                                                 "grant_id": "dlg_forged"}})
        snap, obo, gid, rid = c.execute(text("SELECT policy_snapshot, on_behalf_of, grant_id, authority_run_id "
                                             "FROM workflow_runs WHERE id = :r"), {"r": run["id"]}).one()
    # The authority is in the run's columns, never in the caller-shaped snapshot.
    assert gid == g.id and obo == ALICE and rid == f"run_wf:{run['id']}" and "_identity" not in snap


def test_system_starts_are_not_asked_and_record_no_authority(env):
    with env.engine.begin() as c:
        _, version = _workflow(c, env.project)
        run = _start(c, env.project, version["id"], as_person=False)
        snap, obo, gid = c.execute(text("SELECT policy_snapshot, on_behalf_of, grant_id FROM workflow_runs "
                                        "WHERE id = :r"), {"r": run["id"]}).one()
    assert "_identity" not in (snap or {}) and obo is None and gid is None


def test_for_this_run_only_attaches_to_the_run_and_its_builds_carry_it(env, monkeypatch):
    from boss_engine.services import jobs

    with env.engine.begin() as c:
        _, version = _workflow(c, env.project)
    pending = _grant(env, task_run_id=grants.PENDING_PREFIX + "abc")
    with env.engine.begin() as c:
        run = _start(c, env.project, version["id"])
    with env.engine.begin() as c:
        claimed = grants.load(pending.id, db=c)
        assert claimed.task_run_id == f"run_wf:{run['id']}"                       # bound to this run only
        acting = consent.acting_for_workflow_run(c, run["id"])
        assert acting.principal_id == ALICE and acting.grant_id == pending.id
        # The worker queues the run's build: no session, no ambient context — the job carries it.
        job = jobs.enqueue_job(c, env.project, "code.task", {"run_id": "crun_x1", "workflow_run_id": run["id"]})
        obo, gid, chain = c.execute(text("SELECT on_behalf_of, grant_id, chain FROM jobs WHERE id = :j"),
                                    {"j": job["id"]}).one()
    assert obo == ALICE and gid == pending.id and chain == []
    # ...and the build mints its run token from that job: Forge for Alice, nested under the run.
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    import tempfile

    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    shim, _ = runs.start_code_task_run(env.engine, task_run_id="crun_x1", project_id=env.project, owner="acme",
                                       repo="web", role_key="forge", job_id=job["id"])
    url = ISSUER + "/v1/x"
    tok = shim.tokens["boss:api"].access_token
    with env.engine.begin() as c:
        claims = tokens.verify_run_token(tok, audience="boss:api", dpop_proof=shim.key.proof("GET", url, access_token=tok),
                                         htm="GET", htu=url, db=c)
    assert claims.sub == ALICE and claims.grant_id == pending.id
    assert claims.task.run_id == f"run_wf:{run['id']}/ct:crun_x1" and claims.chain.agent_ids == (FORGE,)
    runs.finish_code_task_run(env.engine, shim, org_id="org_boss")
    # A second run cannot use the task grant: it was claimed by the first.
    with env.engine.begin() as c, pytest.raises(consent.ConsentRequired):
        _start(c, env.project, version["id"])
    assert "start_code_task" in open(consent.__file__.replace("identity/consent.py", "services/code_task_service.py")).read()


def test_revoking_the_grant_mid_run_halts_the_run_its_build_and_its_queued_work(env, monkeypatch):
    from boss_engine.services import jobs

    with env.engine.begin() as c:
        _, version = _workflow(c, env.project)
    g = _grant(env)
    with env.engine.begin() as c:
        run = _start(c, env.project, version["id"])
        job = jobs.enqueue_job(c, env.project, "code.task", {"run_id": "crun_y1", "workflow_run_id": run["id"]})
    with env.engine.begin() as c:
        report = cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(ALICE),
                                        reason="changed my mind", db=c)
    assert f"run_wf:{run['id']}" in report.runs_halted
    with env.engine.begin() as c:
        status, why = c.execute(text("SELECT status, blocked_reason FROM workflow_runs WHERE id = :r"),
                                {"r": run["id"]}).one()
        jstatus = c.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": job["id"]}).scalar()
        assert status == "paused" and "authority revoked" in why and jstatus == "cancelled"
        assert consent.acting_for_workflow_run(c, run["id"]) is None              # nothing more acts for it


def test_a_schedule_needs_a_standing_grant_and_pauses_when_it_ends(env):
    from boss_engine.services import workflow_schedules

    with env.engine.begin() as c:
        definition, _ = _workflow(c, env.project)
        key = c.execute(text("SELECT workflow_key FROM workflow_definitions WHERE id = :d"),
                        {"d": definition["id"]}).scalar()

    def create():
        with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
            return workflow_schedules.create(c, project_id=env.project, workflow_key=key, name=f"nightly {uuid.uuid4().hex[:4]}",
                                             cadence="daily", minute=0, actor="ceo",
                                             input_data={"title": "x", "repository": "acme/web",
                                                         "_identity": {"grant_id": "dlg_forged"}})

    with pytest.raises(consent.ConsentRequired) as e:
        create()
    assert e.value.body["scope"] == "schedule" and [d["kind"] for d in e.value.body["durations"]] == ["standing"]
    _grant(env, task_run_id=grants.PENDING_PREFIX + "t")                         # a task grant is not enough
    with pytest.raises(consent.ConsentRequired):
        create()
    g = _grant(env)
    sched = create()
    with env.engine.begin() as c:
        stored, obo, gid = c.execute(text("SELECT input, on_behalf_of, grant_id FROM workflow_schedules "
                                          "WHERE id = :s"), {"s": sched["id"]}).one()
    assert obo == ALICE and gid == g.id and "_identity" not in stored          # the forged link was dropped
    # The grant ends -> the cascade pauses the schedule; a firing under a dead grant pauses too.
    with env.engine.begin() as c:
        report = cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(ALICE), reason="x", db=c)
        assert sched["id"] in report.schedules_paused
        assert c.execute(text("SELECT enabled FROM workflow_schedules WHERE id = :s"), {"s": sched["id"]}).scalar() is False
        c.execute(text("UPDATE workflow_schedules SET enabled = true, next_run_at = now() - interval '1 minute' "
                       "WHERE id = :s"), {"s": sched["id"]})
        out = workflow_schedules.run_due(c, project_id=env.project)
        row = c.execute(text("SELECT enabled, last_error FROM workflow_schedules WHERE id = :s"),
                        {"s": sched["id"]}).one()
    assert out and out[0]["error"] == "authority ended" and row[0] is False and row[1].startswith("Renew authority")


def test_a_live_schedule_fires_under_its_grant(env, monkeypatch):
    from boss_engine.services import workflow_schedules

    with env.engine.begin() as c:
        definition, _ = _workflow(c, env.project)
        key = c.execute(text("SELECT workflow_key FROM workflow_definitions WHERE id = :d"),
                        {"d": definition["id"]}).scalar()
    g = _grant(env)
    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
        sched = workflow_schedules.create(c, project_id=env.project, workflow_key=key, name="nightly",
                                          cadence="daily", minute=0, actor="ceo",
                                          input_data={"title": "x", "repository": "acme/web"})
    import boss_engine.services.workflow_readiness as readiness

    monkeypatch.setattr(readiness, "refuse_unless_ready", lambda *a, **k: {"missing": [], "started_anyway": []})
    monkeypatch.setattr(readiness, "record_started_anyway", lambda *a, **k: None)
    with env.engine.begin() as c:
        c.execute(text("UPDATE workflow_schedules SET next_run_at = now() - interval '1 minute' WHERE id = :s"),
                  {"s": sched["id"]})
        out = workflow_schedules.run_due(c, project_id=env.project)
        assert out and out[0].get("run_id"), out
        snap = c.execute(text("SELECT grant_id, input, on_behalf_of FROM workflow_runs WHERE id = :r"),
                         {"r": out[0]["run_id"]}).one()
    assert snap[0] == g.id and "_identity" not in snap[1] and snap[2] == ALICE



def test_an_agent_without_a_passport_gets_one_lazily_so_a_person_is_still_asked(env, monkeypatch):
    """Integration: no install ran the Passport upgrade, so builds silently ran outside delegation
    (consent returned None for an un-passported agent). The start now upgrades the roles first —
    the person is asked for consent — and under enforce an agent that still cannot have one is
    refused, never run as the worker."""
    from boss_engine.identity import consent, passports

    calls = []
    real = passports.upgrade_from_roles
    monkeypatch.setattr(passports, "upgrade_from_roles",
                        lambda **kw: calls.append(kw) or real(**kw))
    monkeypatch.setattr(passports, "get", lambda aid, db=None: None)   # nobody can get one
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    from boss_engine.identity import runs

    monkeypatch.setattr(runs, "agent_for_role", lambda db, project_id, rk: "agt_nopass000001")
    with env.engine.begin() as db:
        with pytest.raises(consent.AgentWithoutPassport):
            consent._agents(db, env.project, ["builder"])
    assert calls and calls[0]["org_id"] == "org_boss"
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "shadow")
    with env.engine.begin() as db:
        assert consent._agents(db, env.project, ["builder"]) is None


def test_consent_asks_for_what_each_steps_tool_is_checked_against(env):
    """Integration walk: a Fast Fix run started under a grant without code_task:create, then its
    build step was refused under enforce ("'create_code_task' needs code_task:create"). The needs
    now come from each step's capability tool declaration."""
    with env.engine.begin() as db:
        roles, perms, _ = consent.workflow_needs(db, env.project, {"steps": [
            {"id": "build", "binding": {"capability": "build"}}]}, {})
    assert "code_task:create" in perms and roles


def test_revoking_a_grant_stops_a_build_whose_job_never_started(env):
    """Integration walk: revoking the grant cancelled the queued code.task job, but its code-task run
    row stayed `running` for ever. The cascade now stops that run too."""
    import json as _json

    from boss_engine.identity import cascade, delegation, lineage
    from boss_engine.identity.delegation import CascadeTrigger, GrantRequest
    from ._identity_agents_support import session_ctx

    with env.engine.begin() as c:
        g = delegation.create_grant(session_ctx(ALICE), GrantRequest(
            agent_ids=(FORGE,), permissions=frozenset(NEEDS), project_ids=(env.project,), purpose="walk"), db=c)
        crun = f"crun_{uuid.uuid4().hex[:12]}"
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase) VALUES "
                       "(:i, :p, 'walk', 'running', 'queued')"), {"i": crun, "p": env.project})
        payload = lineage.stamp_job({"code_task_run_id": crun},
                                    lineage.Acting(principal_id=ALICE, org_id="org_boss", grant_id=g.id))
        c.execute(text("INSERT INTO jobs (id, project_id, kind, status, payload, grant_id, on_behalf_of) VALUES "
                       "(:j, :p, 'code.task', 'queued', CAST(:pl AS JSONB), :g, :u)"),
                  {"j": f"job_{uuid.uuid4().hex[:12]}", "p": env.project, "pl": _json.dumps(payload), "g": g.id, "u": ALICE})
    with env.engine.begin() as c:
        delegation.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(ALICE), reason="walk", db=c)
    with env.engine.begin() as c:
        assert c.execute(text("SELECT status FROM code_task_runs WHERE id = :i"), {"i": crun}).scalar() == "cancelled"


def test_revoking_a_grant_stops_a_build_already_running_under_it(env):
    """Integration walk (J9): a build that took the legacy repo path minted no run token of its
    own, so the revoke paused the workflow but the code task ran on to completion. A leased
    code.task job under the grant now gets its build stopped (a stop request the stop watch
    honours within seconds)."""
    import json as _json

    from boss_engine.identity import delegation, lineage
    from boss_engine.identity.delegation import CascadeTrigger, GrantRequest
    from ._identity_agents_support import session_ctx

    with env.engine.begin() as c:
        g = delegation.create_grant(session_ctx(ALICE), GrantRequest(
            agent_ids=(FORGE,), permissions=frozenset(NEEDS), project_ids=(env.project,), purpose="walk"), db=c)
        crun = f"crun_{uuid.uuid4().hex[:12]}"
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase) VALUES "
                       "(:i, :p, 'walk', 'running', 'serve_check')"), {"i": crun, "p": env.project})
        payload = lineage.stamp_job({"run_id": crun},
                                    lineage.Acting(principal_id=ALICE, org_id="org_boss", grant_id=g.id))
        c.execute(text("INSERT INTO jobs (id, project_id, kind, status, payload, grant_id, on_behalf_of) VALUES "
                       "(:j, :p, 'code.task', 'running', CAST(:pl AS JSONB), :g, :u)"),
                  {"j": f"job_{uuid.uuid4().hex[:12]}", "p": env.project, "pl": _json.dumps(payload), "g": g.id, "u": ALICE})
    with env.engine.begin() as c:
        report = delegation.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(ALICE),
                                           reason="walk", db=c)
    assert f"run_ct:{crun}" in report.runs_halted
    with env.engine.begin() as c:
        assert c.execute(text("SELECT phase FROM code_task_stop_requests WHERE run_id = :i"), {"i": crun}).scalar() \
            == "authority_revoked"
