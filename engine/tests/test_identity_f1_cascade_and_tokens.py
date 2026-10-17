"""F1 · cascade gaps (#14, #15) and the token/egress MEDIUM and LOW items.

Each test fails without the fix it names:
  #14  an expired grant runs the FULL cascade on schedule (identity maintenance); a workflow's
       "for this run" grant ends when the run ends;
  #15  a Passport suspend halts the legacy-path builds that run as it, and queued work; a person
       disable halts their workflow runs and queued jobs, flags the agents they sponsor and the
       service accounts left without an owner;
  M    the NOTIFY payload is a notice id (never truncated JSON); the egress listener reconnects and
       catches up; a token minted while a cascade holds the grant waits and is refused;
  L    a child of an AUTONOMOUS run cannot renew after its parent's run ends; renewal is single-use,
       rate-limited and names its predecessor; introspection checks what verification checks;
       DPoP proofs dated in the future are refused and replay rows outlive the proof; a human's
       ceiling is never "repaired"; set_rules is lineage; signing keys refuse boot; run tokens are
       renewed at runtime (egress does not die at 5 minutes); a run with no declared GitHub needs
       only reads; run end tells a separate egress process; mcp.json never carries a BOSS credential.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.identity import (
    cascade,
    dpop,
    github_app,
    grants,
    maintenance,
    passports,
    runs,
    signing_keys,
    tokens,
)
from boss_engine.identity.delegation import AUD_API, AUD_EGRESS, CascadeTrigger

from ._identity_agents_support import (
    BASE,
    BUILDER,
    agent_ctx,
    build_env,
    install_github,
    session_ctx,
    spawn,
    start,
    verify,
)


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


def _live_jtis(env, grant_id):
    with env.db() as c:
        return c.execute(text("SELECT count(*) FROM identity_run_credentials WHERE grant_id = :g AND revoked_at IS NULL"),
                         {"g": grant_id}).scalar()


# ---- #14 ---------------------------------------------------------------------------------------------------

def test_14_an_expired_grant_runs_the_full_cascade_on_schedule(env):
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    shim = start(env, g.id, agent=env.forge, run="run_ct:crun_exp", repos=("acme/web",))
    with env.db() as c:
        c.execute(text("INSERT INTO jobs (id, project_id, kind, status, grant_id) VALUES ('job_q1', :p, 'code.task', 'queued', :g)"),
                  {"p": env.project, "g": g.id})
        c.execute(text("UPDATE identity_delegation_grants SET expires_at = now() - interval '1 minute' WHERE id = :g"),
                  {"g": g.id})
    assert _live_jtis(env, g.id) >= 1
    out = maintenance.tick(env.engine)
    assert g.id in out["expired_grants"]
    with env.db() as c:
        assert grants.load(g.id, db=c).status.value == "expired"
        assert c.execute(text("SELECT count(*) FROM identity_egress_credentials WHERE run_id = 'run_ct:crun_exp' "
                              "AND revoked_at IS NULL")).scalar() == 0
        assert c.execute(text("SELECT status FROM jobs WHERE id = 'job_q1'")).scalar() == "cancelled"
        assert c.execute(text("SELECT 1 FROM code_task_stop_requests WHERE run_id = 'crun_exp'")).scalar() == 1
    assert _live_jtis(env, g.id) == 0
    del shim


def test_14_a_workflow_runs_this_run_grant_ends_with_the_run(env):
    g = env.grant(agents=(env.forge,), children=(), task_run_id="run_wf:wfr_done")
    with env.db() as c:
        c.execute(text("INSERT INTO workflow_runs (id, status) VALUES ('wfr_done', 'running')"))
    with env.engine.connect() as c:            # closed: an open read would block the teardown TRUNCATE
        assert maintenance.end_finished_workflow_grants(db=c) == []
    with env.db() as c:
        c.execute(text("UPDATE workflow_runs SET status = 'completed' WHERE id = 'wfr_done'"))
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [g.id]
    with env.db() as c:
        assert grants.load(g.id, db=c).status.value == "expired"


def test_14_a_failed_runs_grant_outlives_the_failure_so_a_retry_can_finish(env):
    """HelpDesk AI (wfr_fb4fb5de) failed on a deploy, was retried and deployed healthy — then its
    Monitor step died "the authority this run was started under has ended": the grant had been
    ended while the run was failed. A failed run is retryable, so its grant waits out the grace;
    a completed run's still ends at once."""
    g = env.grant(agents=(env.forge,), children=(), task_run_id="run_wf:wfr_retry")
    with env.db() as c:
        c.execute(text("INSERT INTO workflow_runs (id, status) VALUES ('wfr_retry', 'failed')"))
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [], "ended a retryable run's grant"
    with env.db() as c:
        c.execute(text("UPDATE workflow_runs SET updated_at = now() - interval '25 hours' "
                       "WHERE id = 'wfr_retry'"))
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [g.id], "the grace never ends"


def _child_work_tables(c):
    """What a full install has and this throwaway schema leaves out."""
    c.execute(text("ALTER TABLE code_task_runs ADD COLUMN IF NOT EXISTS finalize_ctx JSONB "
                   "NOT NULL DEFAULT '{}'::jsonb"))
    c.execute(text("CREATE TABLE IF NOT EXISTS workflow_step_runs (id TEXT PRIMARY KEY, "
                   "workflow_run_id TEXT, output JSONB)"))
    for column in ("step_id TEXT", "status TEXT", "lease_expires_at TIMESTAMPTZ"):
        c.execute(text(f"ALTER TABLE workflow_step_runs ADD COLUMN IF NOT EXISTS {column}"))
    c.execute(text("CREATE TABLE IF NOT EXISTS stack_deployments (id TEXT PRIMARY KEY, "
                   "project_id TEXT, target_id TEXT, repository TEXT, ref TEXT, commit_sha TEXT, "
                   "created_by TEXT, status TEXT, workflow_run_id TEXT)"))


def test_14_a_finished_runs_grant_waits_for_its_live_child_work(env):
    """A run that went terminal on one branch while another still works (a deploy failed while
    More tests was writing tests) keeps its grant until the last child settles: ending it killed
    that live code task, and every Retry re-asked consent."""
    g = env.grant(agents=(env.forge,), children=(), task_run_id="run_wf:wfr_kids")
    with env.db() as c:
        _child_work_tables(c)
        c.execute(text("INSERT INTO workflow_runs (id, status) VALUES ('wfr_kids', 'failed')"))
        # Failed long enough ago that its retry grace is over (RETRYABLE_GRANT_GRACE_HOURS).
        c.execute(text("UPDATE workflow_runs SET updated_at = now() - interval '25 hours' WHERE id = 'wfr_kids'"))
        c.execute(text("INSERT INTO code_task_runs (id, project_id, status, spec, finalize_ctx) VALUES "
                       "('crun_kid', :p, 'running', '{}', '{\"workflow_run_id\": \"wfr_kids\"}')"),
                  {"p": env.project})
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [], "ended under a live code task"
    with env.db() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = 'crun_kid'"))
        c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
                       "commit_sha, created_by, status, workflow_run_id) VALUES ('sdep_kid', :p, "
                       "'deptgt_x', 'acme/web', 'pull/1', 'c', 'test', 'applying', 'wfr_kids')"),
                  {"p": env.project})
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [], "ended under a live deploy"
    with env.db() as c:
        c.execute(text("UPDATE stack_deployments SET status = 'failed' WHERE id = 'sdep_kid'"))
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [g.id]
    with env.db() as c:
        c.execute(text("DELETE FROM stack_deployments WHERE id = 'sdep_kid'"))
        c.execute(text("DELETE FROM code_task_runs WHERE id = 'crun_kid'"))


def test_14_a_finished_runs_grant_waits_for_a_branch_still_in_flight(env):
    """BOSSAPP-540: pass 2 runs More tests beside deploy, monitor, report and smoke_test on an
    `all` join. A failure in one branch ends the run while a step of the other is still inside
    its call, before it has any child work of its own; ending the grant took that step's
    authority away mid-call. The grant waits while a worker holds a live lease on a step of the
    run, and ends once the lease is gone or has expired (a dead worker never holds it for ever)."""
    g = env.grant(agents=(env.forge,), children=(), task_run_id="run_wf:wfr_branch")
    with env.db() as c:
        _child_work_tables(c)
        c.execute(text("INSERT INTO workflow_runs (id, status) VALUES ('wfr_branch', 'failed')"))
        # Failed long enough ago that its retry grace is over (RETRYABLE_GRANT_GRACE_HOURS).
        c.execute(text("UPDATE workflow_runs SET updated_at = now() - interval '25 hours' WHERE id = 'wfr_branch'"))
        c.execute(text("INSERT INTO workflow_step_runs (id, workflow_run_id, step_id, status, "
                       "lease_expires_at, output) VALUES ('wfs_deploy', 'wfr_branch', 'deploy', "
                       "'running', now() + interval '5 minutes', '{}')"))
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [], "ended under a live branch"
    with env.db() as c:
        c.execute(text("UPDATE workflow_step_runs SET lease_expires_at = now() - interval '1 second' "
                       "WHERE id = 'wfs_deploy'"))
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [g.id], "a dead lease holds it"
    with env.db() as c:
        c.execute(text("DELETE FROM workflow_step_runs WHERE id = 'wfs_deploy'"))


def test_14_a_finished_runs_grant_ends_once_its_last_branch_settles(env):
    g = env.grant(agents=(env.forge,), children=(), task_run_id="run_wf:wfr_settled")
    with env.db() as c:
        _child_work_tables(c)
        c.execute(text("DELETE FROM workflow_step_runs WHERE id = 'wfs_smoke'"))  # a crashed earlier run's row
        c.execute(text("INSERT INTO workflow_runs (id, status) VALUES ('wfr_settled', 'failed')"))
        c.execute(text("UPDATE workflow_runs SET updated_at = now() - interval '25 hours' WHERE id = 'wfr_settled'"))
        c.execute(text("INSERT INTO workflow_step_runs (id, workflow_run_id, step_id, status, "
                       "lease_expires_at, output) VALUES ('wfs_smoke', 'wfr_settled', 'smoke_test', "
                       "'leased', now() + interval '5 minutes', '{}')"))
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == []
    with env.db() as c:
        c.execute(text("UPDATE workflow_step_runs SET status = 'succeeded', lease_expires_at = NULL "
                       "WHERE id = 'wfs_smoke'"))
    with env.db() as c:
        assert maintenance.end_finished_workflow_grants(db=c) == [g.id]
    with env.db() as c:
        c.execute(text("DELETE FROM workflow_step_runs WHERE id = 'wfs_smoke'"))


# ---- #15 ---------------------------------------------------------------------------------------------------

def test_15_suspending_a_passport_halts_its_legacy_path_builds_and_queued_work(env):
    env.world.grant(env.alice, {"agent_passport:suspend"})
    with env.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id) VALUES ('r-f', 'forge', :a)"), {"a": env.forge})
        c.execute(text("INSERT INTO code_task_runs (id, project_id, spec) VALUES ('crun_leg1', :p, '{\"role_key\": \"forge\"}'), "
                       "('crun_leg2', :p, '{}')"), {"p": env.project})
        # a build running as forge (legacy path: no token), one whose job's chain holds forge, one queued
        c.execute(text("INSERT INTO jobs (id, project_id, kind, status, payload, chain) VALUES "
                       "('job_l1', :p, 'code.task', 'running', '{\"run_id\": \"crun_leg1\"}', NULL), "
                       "('job_l2', :p, 'code.task', 'leased', '{\"run_id\": \"crun_leg2\"}', CAST(:ch AS JSONB)), "
                       "('job_l3', :p, 'code.task', 'queued', '{\"run_id\": \"crun_leg3\"}', CAST(:ch AS JSONB))"),
                  {"p": env.project, "ch": json.dumps([f"{env.forge}#run_x"])})
    with env.db() as c:
        report = passports.suspend(session_ctx(env.alice), env.forge, reason="investigate", db=c)
    assert {"run_ct:crun_leg1", "run_ct:crun_leg2"} <= set(report.runs_halted)
    with env.db() as c:
        stops = set(c.execute(text("SELECT run_id FROM code_task_stop_requests")).scalars().all())
        assert {"crun_leg1", "crun_leg2"} <= stops
        assert c.execute(text("SELECT status FROM jobs WHERE id = 'job_l3'")).scalar() == "cancelled"


def test_15_a_person_disable_halts_their_workflow_runs_and_jobs_and_flags_what_they_own(env):
    with env.db() as c:
        c.execute(text("INSERT INTO workflow_runs (id, status, on_behalf_of) VALUES ('wfr_alice', 'running', :u)"),
                  {"u": env.alice})
        c.execute(text("INSERT INTO jobs (id, project_id, kind, status, on_behalf_of) VALUES "
                       "('job_a1', :p, 'meeting.step', 'queued', :u)"), {"p": env.project, "u": env.alice})
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                       "('sva_ci', 'org_boss', 'service_account', 'active', 'CI')"))
        c.execute(text("INSERT INTO identity_service_accounts (principal_id, owner_principal_ids) VALUES ('sva_ci', ARRAY[:u])"),
                  {"u": env.alice})
        c.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :u"), {"u": env.alice})
        report = cascade.revoke_cascade(CascadeTrigger.PERSON_DISABLED, env.alice,
                                        actor=cascade.system_actor("org_boss"), reason="left", db=c)
    assert "run_wf:wfr_alice" in report.runs_halted
    with env.db() as c:
        assert c.execute(text("SELECT status FROM workflow_runs WHERE id = 'wfr_alice'")).scalar() == "paused"
        assert c.execute(text("SELECT status FROM jobs WHERE id = 'job_a1'")).scalar() == "cancelled"
    flagged = [e for e in env.world.events if e.event_type == "agent.passport.flagged"]
    assert {e.target_id for e in flagged} >= {env.atlas, env.forge, env.vera, env.rook}    # alice sponsors them
    assert any(e.event_type == "service_account.orphaned" and e.target_id == "sva_ci" for e in env.world.events)


# ---- NOTIFY, the listener, the mint race -----------------------------------------------------------------

def test_the_notify_payload_is_an_id_and_the_listener_fetches_every_run(env, monkeypatch):
    import psycopg

    from apps.egress.boss_egress.app import process_notices

    url = env.engine.url.render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://")
    listener = psycopg.connect(url, autocommit=True)
    listener.execute(f"LISTEN {cascade.NOTIFY_CHANNEL}")
    many = [f"run_ct:crun_{i:06d}_padding_padding" for i in range(600)]          # > 8000 bytes of ids
    with env.db() as c:
        cascade.notify(c, trigger="grant_revoked", target_id="dlg_x", runs=many, reason="test")
    note = next(listener.notifies(timeout=5, stop_after=1))
    payload = json.loads(note.payload)
    assert set(payload) == {"notice"}
    revoked: list[str] = []
    monkeypatch.setattr(github_app, "revoke_runs", lambda r, why: revoked.extend(r))
    assert process_notices(env.engine, 0) == payload["notice"]
    assert revoked == many
    listener.close()


def test_the_revocation_listener_reconnects_and_catches_up(env, monkeypatch):
    from apps.egress.boss_egress import app as egress_app

    revoked: list[str] = []
    monkeypatch.setattr(github_app, "revoke_runs", lambda r, why: revoked.extend(r))

    class Flaky:
        """The engine, except that the first LISTEN connection dies."""
        def __init__(self, engine):
            self.engine, self.failures = engine, 1

        def begin(self):
            return self.engine.begin()

        def raw_connection(self):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("database restarting")
            return self.engine.raw_connection()

    stop = threading.Event()
    t = threading.Thread(target=egress_app.listen_for_revocations, args=(Flaky(env.engine), stop),
                         kwargs={"backoff_max": 0.2, "start_after": 0}, daemon=True)
    with env.db() as c:                                       # written while the listener is down
        cascade.notify(c, trigger="grant_revoked", target_id="dlg_y", runs=["run_ct:crun_while_down"], reason="t")
    t.start()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and "run_ct:crun_while_down" not in revoked:
        time.sleep(0.1)
    with env.db() as c:
        cascade.notify(c, trigger="grant_revoked", target_id="dlg_z", runs=["run_ct:crun_after"], reason="t")
    while time.monotonic() < deadline and "run_ct:crun_after" not in revoked:
        time.sleep(0.1)
    stop.set()
    t.join(timeout=5)
    assert revoked == ["run_ct:crun_while_down", "run_ct:crun_after"]


def test_a_token_minted_while_a_cascade_holds_the_grant_waits_and_is_refused(env):
    g = env.grant()
    holding = threading.Event()
    result: dict = {}

    def revoke():
        with env.db() as c:
            cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(env.alice), reason="r", db=c)
            holding.set()
            time.sleep(1.0)                                   # the cascade's transaction is still open

    def mint():
        holding.wait(5)
        try:
            result["shim"] = start(env, g.id, run="run_ct:crun_race")
        except tokens.TokenError as exc:
            result["error"] = exc.reason

    a, b = threading.Thread(target=revoke), threading.Thread(target=mint)
    a.start(), b.start()
    a.join(), b.join()
    assert result.get("error") in ("grant_inactive", "grant_required"), result
    assert _live_jtis(env, g.id) == 0


# ---- tokens --------------------------------------------------------------------------------------------

def test_a_child_of_an_autonomous_run_cannot_renew_after_its_parent_ends(env):
    with env.db() as c:
        c.execute(text("UPDATE identity_agent_passports SET autonomous = true WHERE agent_id = :a"), {"a": env.forge})
    env.world.grant(env.forge, {"code_task:read"}, env.project)
    with env.db() as c:
        parent = runs.start_run(agent_ctx(env.forge), runs.RunStart(run_id="run_ag:auto1", agent_id=env.forge,
                                                                    project_id=env.project, scope=frozenset(BUILDER)),
                                grant_id=None, db=c)
    child = spawn(env, parent, env.vera, "vera", scope=("code_task:read",))
    assert verify(env, child).depth == 1
    with env.db() as c:                                       # the parent's run ends (only the parent's)
        c.execute(text("UPDATE identity_run_credentials SET revoked_at = now() WHERE run_id = 'run_ag:auto1'"))
    with env.db() as c, pytest.raises(tokens.TokenError) as e:
        runs.renew(child, db=c, base_url=BASE)
    assert e.value.reason == "parent_run_ended"


def test_renewal_is_single_use_rate_limited_and_names_its_predecessor(env, monkeypatch):
    shim = start(env, env.grant().id)
    tok = shim.tokens[AUD_API]
    from boss_engine.identity.delegation import TOKEN_ENDPOINT

    htu = BASE + TOKEN_ENDPOINT
    with env.db() as c:
        new = tokens.renew_run_token(tok.access_token, dpop_proof=shim.key.proof("POST", htu, access_token=tok.access_token),
                                     htm="POST", htu=htu, db=c)
    renewed = [e for e in env.world.events if e.event_type == "token.issued" and e.token_jti == new.jti]
    assert renewed and renewed[0].details.get("renewed_from") == tok.jti
    with env.db() as c, pytest.raises(tokens.TokenError):     # the same token cannot be renewed twice
        tokens.renew_run_token(tok.access_token, dpop_proof=shim.key.proof("POST", htu, access_token=tok.access_token),
                               htm="POST", htu=htu, db=c)
    monkeypatch.setattr(tokens, "EXCHANGE_LIMIT_PER_MINUTE", 1)
    current = new
    with pytest.raises(tokens.TokenError) as e:
        for _ in range(3):
            with env.db() as c:
                current = tokens.renew_run_token(
                    current.access_token, dpop_proof=shim.key.proof("POST", htu, access_token=current.access_token),
                    htm="POST", htu=htu, db=c)
    assert e.value.code == "rate_limited"


def test_introspection_refuses_what_verification_refuses(env):
    g = env.grant()
    atlas = start(env, g.id)
    forge = spawn(env, atlas, env.forge, "forge")
    with env.db() as c:
        assert tokens.introspect(forge.tokens[AUD_API].access_token, db=c)["active"]
        c.execute(text("UPDATE identity_delegation_grants SET max_depth = 1 WHERE id = :g"), {"g": g.id})
    with pytest.raises(tokens.TokenError):
        verify(env, forge)
    with env.db() as c:
        assert tokens.introspect(forge.tokens[AUD_API].access_token, db=c) == {"active": False}


def test_a_dpop_proof_dated_in_the_future_is_refused_and_replay_rows_outlive_proofs(env):
    key = dpop.DPoPKey.generate()
    now = time.time()
    with env.db() as c, pytest.raises(dpop.DPoPError):
        dpop.verify_proof(key.proof("GET", BASE + "/x", iat=int(now + 60)), htm="GET", htu=BASE + "/x", access_token=None, expected_jkt=None, db=c, now=now)
    with env.db() as c:
        dpop.verify_proof(key.proof("GET", BASE + "/y", iat=int(now + 20)), htm="GET", htu=BASE + "/y", access_token=None, expected_jkt=None, db=c, now=now)
        expires = c.execute(text("SELECT max(expires_at) FROM identity_dpop_replay")).scalar()
    assert expires.timestamp() >= int(now + 20) + dpop.MAX_AGE_SECONDS + dpop.CLOCK_SKEW_SECONDS


def test_a_ceiling_a_person_set_is_never_repaired_and_rules_changes_are_lineage(env):
    env.world.grant(env.alice, {"agent_passport:manage"})
    with env.db() as c:
        passports.set_ceiling(session_ctx(env.alice), env.rook, frozenset(), db=c)
        assert passports.get(env.rook, db=c).ceiling_preset == "custom"
        passports.set_rules(session_ctx(env.alice), env.rook, can_spawn=True, allowed_children=("vera",), db=c)
    assert any(e.event_type == "agent.passport.rules_changed" and e.target_id == env.rook for e in env.world.events)


def test_signing_keys_that_cannot_be_opened_refuse_boot(env):
    with env.db() as c:
        signing_keys.require_keys(org_id="org_boss", db=c)                  # fine as provisioned
        c.execute(text("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'signing_key'"))
    signing_keys.clear_cache()
    with env.db() as c, pytest.raises(signing_keys.SigningKeysMissing):
        signing_keys.require_keys(org_id="org_boss", db=c)
    with env.db() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("DELETE FROM identity_signing_keys"))
    with env.db() as c, pytest.raises(signing_keys.SigningKeysMissing):
        signing_keys.require_keys(org_id="org_boss", db=c)


# ---- egress at runtime --------------------------------------------------------------------------------

def test_run_tokens_are_renewed_at_runtime_so_egress_outlives_five_minutes(env, monkeypatch):
    install_github(monkeypatch, env)
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    shim = start(env, g.id, agent=env.forge, run="run_ct:crun_long", repos=("acme/web",))
    first = shim.tokens[AUD_EGRESS].jti
    out = runs.renew_due(env.engine, now=time.monotonic() + runs.RENEW_AFTER_SECONDS + 1)
    assert out.get("run_ct:crun_long") == "renewed"
    assert shim.tokens[AUD_EGRESS].jti != first
    with env.db() as c:
        assert c.execute(text("SELECT jti FROM identity_egress_credentials WHERE run_id = 'run_ct:crun_long'")).scalar() \
            == shim.tokens[AUD_EGRESS].jti
        assert runs.resolve_egress(shim.egress_credential, db=c).jti == shim.tokens[AUD_EGRESS].jti


def test_a_run_with_no_declared_needs_only_reads_github(env, monkeypatch):
    install_github(monkeypatch, env)
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))      # the grant says write
    reader = start(env, g.id, agent=env.forge, run="run_ct:crun_r", repos=("acme/web",))
    builder = start(env, g.id, agent=env.forge, run="run_ct:crun_b", repos=("acme/web",),
                    github_needs=runs.BUILD_GITHUB_NEEDS)
    with env.db() as c:
        assert github_app.run_permissions(verify(env, reader, audience=AUD_EGRESS), db=c)["contents"] == "read"
        assert github_app.run_permissions(verify(env, builder, audience=AUD_EGRESS), db=c)["contents"] == "write"


def test_run_end_tells_a_separate_egress_process(env):
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    shim = start(env, g.id, agent=env.forge, run="run_ct:crun_end", repos=("acme/web",))
    with env.db() as c:
        runs.end_run(shim.run_id, db=c, org_id="org_boss")
    with env.db() as c:
        assert c.execute(text("SELECT runs FROM identity_revocation_notices WHERE trigger = 'run_ended' "
                              "ORDER BY id DESC LIMIT 1")).scalar() == ["run_ct:crun_end"]


def test_mcp_json_never_carries_a_boss_credential(monkeypatch):
    from boss_engine.services import agents as agents_mod, code_task_mcp, mcp_registry

    servers = [{"name": "boss-self", "url": "http://api:8000/mcp", "tools": [{"name": "t"}]},
               {"name": "docs", "url": "https://docs.example/mcp", "tools": [{"name": "t"}]}]
    monkeypatch.setattr(agents_mod, "held_mcp_servers", lambda c, p, r: servers)
    monkeypatch.setattr(mcp_registry, "needs_person", lambda s: False)
    monkeypatch.setattr(mcp_registry, "tool_policy", lambda s, t: "allow")
    monkeypatch.setattr(mcp_registry, "auth_headers", lambda c, st, p, s: (
        {"Authorization": "Bearer boss_pat_" + "a" * 40} if s["name"] == "boss-self" else {"Authorization": "Bearer up"}))
    planned = code_task_mcp.plan(None, None, "p", "forge")
    assert set(planned["servers"]) == {"docs"}
