"""BOSSAPP-1415: Option A — install-wide storage mutations require instance:manage &
multi-org instance admin; org callers receive org-scoped read-only summaries only."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import get_settings
from boss_engine.identity.principal import (
    AuthContext,
    AuthMethod,
    Principal,
    PrincipalKind,
    PrincipalStatus,
    SessionRef,
)
from boss_engine.services import autonomy_gate, storage_cleanup
from boss_engine.tools.builtin import build_default_registry
from boss_engine.tools.storage_tools import STORAGE_WRITES

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("BOSS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")


@pytest.fixture()
def multi_org_storage(monkeypatch):
    eng = create_engine(DATABASE_URL, future=True)
    tag = uuid.uuid4().hex[:8]
    org_a, org_b = f"org_a_{tag}", f"org_b_{tag}"
    ws_a, ws_b = f"ws_a_{tag}", f"ws_b_{tag}"
    proj_a, proj_b = f"pa_{tag}", f"pb_{tag}"
    admin_a_id, inst_id = f"usr_a_{tag}", f"usr_inst_{tag}"
    inc_b_id = f"si_b_{tag}"
    job_b_id = f"job_b_{tag}"
    host_id = f"host_{tag}"

    with eng.begin() as c:
        for oid, wid, pid in ((org_a, ws_a, proj_a), (org_b, ws_b, proj_b)):
            c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": oid})
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :w)"),
                      {"w": wid, "o": oid})
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                      {"p": pid, "w": wid})
        c.execute(text(
            "INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
            "VALUES (:id, :o, 'user', 'active', 'Admin A')"), {"id": admin_a_id, "o": org_a})
        c.execute(text(
            "INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
            "VALUES (:id, :o, 'user', 'active', 'Inst Admin')"), {"id": inst_id, "o": org_b})
        c.execute(text(
            "INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id, granted_by) VALUES (:rb, :o, 'principal', :s, 'arl_admin', 'org', :o, 'test')"),
            {"rb": f"rbd_{tag}", "o": org_a, "s": admin_a_id})
        c.execute(text(
            "INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id, granted_by) VALUES (:rb, :o, 'principal', :s, 'arl_owner', 'org', :o, 'test')"),
            {"rb": f"rbi_{tag}", "o": org_b, "s": inst_id})
        c.execute(text(
            "INSERT INTO storage_incidents (id, host_id, host_label, status, severity, cause_key, "
            "cause_text, target_pct, project_id, report) VALUES "
            "(:id, :h, :h, 'critical', 'critical', 'backups.no_retention', "
            "'Org B backup pressure', 25, :pb, '{\"agent_notes\": \"secret org B note\"}'::jsonb)"),
            {"id": inc_b_id, "h": host_id, "pb": proj_b})
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, payload, status, last_error) VALUES "
            "(:j, :pb, 'code.task', '{}'::jsonb, 'queued', CAST(:err AS JSONB))"),
            {"j": job_b_id, "pb": proj_b,
             "err": json.dumps({"code": "infra_hold", "incident_id": inc_b_id})})

    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")

    now = datetime.now(timezone.utc)
    sess_a = SessionRef(
        id=f"ses_a_{tag}", mfa_at=now, auth_methods=("password", "passkey"),
        idle_expires_at=now + timedelta(hours=1), absolute_expires_at=now + timedelta(hours=8),
    )
    sess_inst = SessionRef(
        id=f"ses_i_{tag}", mfa_at=now, auth_methods=("password", "passkey"),
        idle_expires_at=now + timedelta(hours=1), absolute_expires_at=now + timedelta(hours=8),
    )
    admin_a = AuthContext(
        Principal(admin_a_id, PrincipalKind.USER, org_a, PrincipalStatus.ACTIVE, "Admin A"),
        AuthMethod.SESSION,
        session=sess_a,
    )
    inst_admin = AuthContext(
        Principal(inst_id, PrincipalKind.USER, org_b, PrincipalStatus.ACTIVE, "Inst Admin"),
        AuthMethod.SESSION,
        session=sess_inst,
    )
    ctx = SimpleNamespace(
        engine=eng, settings=settings,
        begin=lambda: eng.begin(),
    )
    try:
        yield SimpleNamespace(
            eng=eng, ctx=ctx, org_a=org_a, org_b=org_b, proj_a=proj_a, proj_b=proj_b,
            admin_a=admin_a, inst_admin=inst_admin, inc_b_id=inc_b_id, host_id=host_id,
        )
    finally:
        with eng.begin() as c:
            for tbl, col, val in (
                ("events", "project_id", proj_a),
                ("events", "project_id", proj_b),
                ("decisions", "project_id", proj_a),
                ("decisions", "project_id", proj_b),
                ("jobs", "id", job_b_id),
                ("storage_incidents", "id", inc_b_id),
                ("identity_instance_admins", "principal_id", inst_id),
                ("identity_role_bindings", "org_id", org_a),
                ("identity_role_bindings", "org_id", org_b),
                ("identity_settings", "org_id", org_a),
                ("identity_settings", "org_id", org_b),
                ("identity_principals", "org_id", org_a),
                ("identity_principals", "org_id", org_b),
                ("projects", "id", proj_a),
                ("projects", "id", proj_b),
                ("workspaces", "id", ws_a),
                ("workspaces", "id", ws_b),
                ("orgs", "id", org_a),
                ("orgs", "id", org_b),
            ):
                c.execute(text(f"DELETE FROM {tbl} WHERE {col} = :v"), {"v": val})
        eng.dispose()


def test_storage_write_tools_refuse_org_admin_and_reads_are_org_scoped(multi_org_storage):
    m = multi_org_storage
    reg = build_default_registry()

    # Every storage write tool must declare instance:manage.
    for tool_name in STORAGE_WRITES:
        assert reg.get(tool_name).permission == "instance:manage"

    # Even BEFORE any instance_admins row exists, a multi-org DB refuses Org A admin mutations.
    for tool_name, args in (
        ("storage_run_step", {"incident_id": m.inc_b_id}),
        ("storage_propose_rule", {"incident_id": m.inc_b_id, "kind": "logs", "rule": {"max_size_mb": 10}, "why": "big"}),
        ("storage_report", {"incident_id": m.inc_b_id, "cause_text": "hijacked"}),
        ("work_hold_set", {"incident_id": m.inc_b_id}),
        ("work_hold_release", {"incident_id": m.inc_b_id}),
    ):
        res = reg.invoke(tool_name, m.ctx, project_id=m.proj_a, actor=m.admin_a.principal.id,
                         args=args, auth=m.admin_a)
        assert res["ok"] is False and res["error"] == "forbidden", (tool_name, res)

    # Reads for Admin A: storage_plan on Org B incident returns no such incident;
    # storage_inspect returns org-scoped summary (0 paused runs for Org A, no Org B incident).
    plan_res = reg.invoke("storage_plan", m.ctx, project_id=m.proj_a, actor=m.admin_a.principal.id,
                          args={"incident_id": m.inc_b_id}, auth=m.admin_a)
    assert plan_res["ok"] is False and "no such incident" in plan_res["error"]

    insp = reg.invoke("storage_inspect", m.ctx, project_id=m.proj_a, actor=m.admin_a.principal.id,
                      args={"host_id": m.host_id}, auth=m.admin_a)
    assert insp["summary"]["org_scoped"] is True
    assert insp["summary"]["open_incident_id"] is None
    assert insp["incident"] is None
    assert insp["hold"]["paused_runs"] == 0

    # Once instance admin is named, instance admin holds instance:manage and sees/mutates install storage.
    with m.eng.begin() as c:
        c.execute(text(
            "INSERT INTO identity_instance_admins (principal_id, granted_by) VALUES (:p, 'test')"),
            {"p": m.inst_admin.principal.id})
        from boss_engine.identity import authz
        authz.bump_epoch(m.org_b, db=c, reason="test.inst")

    # Now Admin A lacks instance:manage at authz gate too.
    refused_after = reg.invoke("storage_report", m.ctx, project_id=m.proj_a,
                               actor=m.admin_a.principal.id,
                               args={"incident_id": m.inc_b_id, "cause_text": "nope"},
                               auth=m.admin_a)
    assert refused_after["ok"] is False and refused_after["error"] == "forbidden"

    # Instance admin succeeds on storage_report & sees Org B incident in storage_plan.
    ok_rep = reg.invoke("storage_report", m.ctx, project_id=m.proj_b,
                        actor=m.inst_admin.principal.id,
                        args={"incident_id": m.inc_b_id, "cause_text": "Updated by instance admin"},
                        auth=m.inst_admin)
    assert ok_rep["ok"] is True
    ok_plan = reg.invoke("storage_plan", m.ctx, project_id=m.proj_b,
                         actor=m.inst_admin.principal.id,
                         args={"incident_id": m.inc_b_id}, auth=m.inst_admin)
    assert ok_plan["incident_id"] == m.inc_b_id


def test_storage_delete_risk_tier_matches_dev_vs_prod_autonomy(multi_org_storage, monkeypatch):
    m = multi_org_storage
    step = {"tool": "storage.remove_backups", "args": {"keep_newest": 5},
            "gb": 10.0, "plan_hash": "ph_1", "action_id": None}
    inc = {"id": m.inc_b_id, "host_id": m.host_id, "project_id": m.proj_b,
           "cause_text": "Backups", "free_pct_now": 10.0, "target_pct": 25.0}

    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    with m.eng.begin() as c:
        out_dev = storage_cleanup._ask(c, inc, [step], dict(step), ["b1"])
        rt_dev = autonomy_gate.route(c, {
            "project_id": m.proj_b,
            "risk_tier": c.execute(text("SELECT risk_tier FROM decisions WHERE id = :d"),
                                   {"d": out_dev["decision_id"]}).scalar(),
            "recommendation": {"action": "storage_delete", "incident_id": m.inc_b_id},
        })
        c.execute(text("DELETE FROM decisions WHERE id = :d"), {"d": out_dev["decision_id"]})
    assert rt_dev.acts is True

    monkeypatch.setenv("BOSS_INSTALL_TIER", "prod")
    with m.eng.begin() as c:
        out_prod = storage_cleanup._ask(c, inc, [step], dict(step), ["b1"])
        rt_prod = autonomy_gate.route(c, {
            "project_id": m.proj_b,
            "risk_tier": c.execute(text("SELECT risk_tier FROM decisions WHERE id = :d"),
                                   {"d": out_prod["decision_id"]}).scalar(),
            "recommendation": {"action": "storage_delete", "incident_id": m.inc_b_id},
        })
        c.execute(text("DELETE FROM decisions WHERE id = :d"), {"d": out_prod["decision_id"]})
    assert rt_prod.acts is False


def test_storage_scoping_fails_closed_for_orgless_non_operator_and_scopes_report_markdown(
    multi_org_storage, monkeypatch,
):
    from boss_engine.services import storage_incidents
    from boss_engine.tools import registry as tool_registry

    m = multi_org_storage
    tag = uuid.uuid4().hex[:6]
    inc_a_id = f"si_a_{tag}"
    job_a_id = f"job_a_{tag}"
    job_b_on_a_id = f"job_ba_{tag}"

    with m.eng.begin() as c:
        c.execute(text(
            "INSERT INTO storage_incidents (id, host_id, host_label, status, severity, cause_key, "
            "cause_text, target_pct, project_id) VALUES "
            "(:id, :h, :h, 'resolved', 'critical', 'backups.no_retention', 'Org A inc', 25, :pa)"),
            {"id": inc_a_id, "h": m.host_id, "pa": m.proj_a})
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, payload, status, last_error) VALUES "
            "(:ja, :pa, 'code.task', '{}'::jsonb, 'queued', CAST(:err AS JSONB)), "
            "(:jb, :pb, 'code.task', '{}'::jsonb, 'queued', CAST(:err AS JSONB))"),
            {"ja": job_a_id, "pa": m.proj_a, "jb": job_b_on_a_id, "pb": m.proj_b,
             "err": json.dumps({"code": "infra_hold", "incident_id": inc_a_id})})

        # 1. Direct service helpers fail closed when is_operator=False and org_id=None.
        assert storage_incidents.scoped_detail(c, m.inc_b_id, None, is_operator=False) is None
        cur_none = storage_incidents.scoped_current(c, None, is_operator=False)
        assert cur_none["incidents"] == []
        assert cur_none["hold"]["paused_runs"] == 0
        assert storage_incidents.scoped_listing(c, None, is_operator=False) == []
        hold_none = storage_incidents.scoped_hold_status(c, None, is_operator=False)
        assert hold_none["incident_id"] is None and hold_none["paused_runs"] == 0
        summ_none = storage_incidents.scoped_summary(c, None, is_operator=False, host_id=m.host_id)
        assert summ_none["open_incident_id"] is None and summ_none["last_cleanup"] is None

        # 2. report_markdown scoped to org_a counts only Org A's paused job (1), never Org B's (2).
        inc_a = storage_incidents.get(c, inc_a_id)
        md_org_a = storage_incidents.report_markdown(c, inc_a, org_id=m.org_a)
        assert "paused 1 run" in md_org_a
        assert "Work paused: 1 " in md_org_a

    # 3. Tools with an org-less principal and empty project_id fail closed even in shadow mode.
    monkeypatch.setattr(m.ctx.settings, "authz_mode", "shadow")
    reg = build_default_registry()
    orgless = AuthContext(
        Principal(f"usr_nolink_{tag}", PrincipalKind.USER, None, PrincipalStatus.ACTIVE, "No Org"),
        AuthMethod.SESSION,
        session=m.admin_a.session,
    )
    tok = tool_registry.CURRENT_TOOL_AUTH.set(orgless)
    try:
        plan_res = reg.invoke("storage_plan", m.ctx, project_id="", actor=orgless.principal.id,
                              args={"incident_id": m.inc_b_id}, auth=orgless)
        assert plan_res["ok"] is False and "no such incident" in plan_res["error"]
        insp_res = reg.invoke("storage_inspect", m.ctx, project_id="", actor=orgless.principal.id,
                              args={"host_id": m.host_id, "incident_id": m.inc_b_id}, auth=orgless)
        assert insp_res["incident"] is None
        assert insp_res["summary"]["open_incident_id"] is None
        assert insp_res["hold"]["paused_runs"] == 0
        # An authenticated org-less principal never borrows the org of the project_id it
        # passes (shadow mode included): Org B's incident stays invisible.
        plan_b = reg.invoke("storage_plan", m.ctx, project_id=m.proj_b, actor=orgless.principal.id,
                            args={"incident_id": m.inc_b_id}, auth=orgless)
        assert plan_b["ok"] is False and "no such incident" in plan_b["error"]
        insp_b = reg.invoke("storage_inspect", m.ctx, project_id=m.proj_b,
                            actor=orgless.principal.id,
                            args={"host_id": m.host_id, "incident_id": m.inc_b_id}, auth=orgless)
        assert insp_b["incident"] is None
        assert insp_b["summary"]["open_incident_id"] is None
        assert insp_b["hold"]["paused_runs"] == 0
    finally:
        tool_registry.CURRENT_TOOL_AUTH.reset(tok)
        with m.eng.begin() as c:
            c.execute(text("DELETE FROM jobs WHERE id IN (:ja, :jb)"),
                      {"ja": job_a_id, "jb": job_b_on_a_id})
            c.execute(text("DELETE FROM storage_incidents WHERE id = :i"), {"i": inc_a_id})
