"""BOSSAPP-1415: HTTP route multi-org isolation for /v1/infra/storage/*."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authz


def test_infra_storage_routes_enforce_instance_admin_and_org_scoped_reads(client, engine):
    world_a = IdentityWorld.create(engine)
    world_b = IdentityWorld.create(engine)
    admin_a = world_a.person("admin_a")
    world_a.bind(admin_a, "admin")
    inst = world_b.person("inst_admin")
    world_b.bind(inst, "owner")

    tag = uuid.uuid4().hex[:8]
    inc_b = f"si_http_b_{tag}"
    inc_global = f"si_http_g_{tag}"
    job_b = f"job_http_b_{tag}"
    host_b = f"host_b_{tag}"

    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO storage_incidents (id, host_id, host_label, status, severity, cause_key, "
            "cause_text, target_pct, project_id, report) VALUES "
            "(:id, :h, :h, 'critical', 'critical', 'backups.no_retention', 'Org B disk full', "
            "25, :pb, '{\"agent_notes\": \"org-b-only\"}'::jsonb)"),
            {"id": inc_b, "h": host_b, "pb": world_b.project_a})
        c.execute(text(
            "INSERT INTO storage_incidents (id, host_id, host_label, status, severity, cause_key, "
            "cause_text, target_pct, project_id) VALUES "
            "(:id, :h, :h, 'warn', 'warn', 'logs.uncapped', 'Install log pressure', 25, NULL)"),
            {"id": inc_global, "h": f"host_g_{tag}"})
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, payload, status, last_error) VALUES "
            "(:j, :pb, 'code.task', '{}'::jsonb, 'queued', CAST(:err AS JSONB))"),
            {"j": job_b, "pb": world_b.project_a,
             "err": json.dumps({"code": "infra_hold", "incident_id": inc_b})})

    try:
        # 1. Pre-separation multi-org DB: Admin A is STILL refused all 5 storage mutators (403).
        with world_a.acting_as(world_a.ctx(admin_a)):
            for method, path, body in (
                ("POST", f"/v1/infra/storage/incidents/{inc_b}/acknowledge", {}),
                ("POST", "/v1/infra/storage/clean", {"dry_run": True}),
                ("POST", f"/v1/infra/storage/incidents/{inc_b}/replan", {"tool": "storage.remove_backups", "args": {}}),
                ("POST", "/v1/infra/storage/hold/release", {"reason": "test"}),
                ("PUT", "/v1/infra/storage/rules", {"rules": [], "thresholds": {"warn_free_pct": 25.0}}),
            ):
                resp = client.request(method, path, json=body)
                assert resp.status_code == 403, (method, path, resp.status_code, resp.text)

        # 2. Once an instance admin is named, Admin A gets 403 from PDP (instance:manage)
        #    and Org-scoped read-only summaries on GETs, 404 on Org B incident detail/report.
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO identity_instance_admins (principal_id, granted_by) VALUES (:p, 'test')"),
                {"p": inst.id})
            authz.bump_epoch(world_a.org, db=c, reason="test.inst")
            authz.bump_epoch(world_b.org, db=c, reason="test.inst")

        with world_a.acting_as(world_a.ctx(admin_a)):
            cur = client.get("/v1/infra/storage/incidents/current")
            assert cur.status_code == 200, cur.text
            ids = {i["id"] for i in cur.json()["incidents"]}
            assert inc_b not in ids
            assert inc_global in ids
            assert cur.json()["hold"]["paused_runs"] == 0
            assert cur.json()["org_scoped"] is True

            lst = client.get("/v1/infra/storage/incidents")
            assert lst.status_code == 200
            assert inc_b not in {i["id"] for i in lst.json()["incidents"]}

            hold = client.get("/v1/infra/storage/hold")
            assert hold.status_code == 200
            assert hold.json()["hold"]["paused_runs"] == 0
            assert hold.json()["hold"]["org_scoped"] is True

            summ = client.get("/v1/infra/storage/summary", params={"host_id": host_b})
            assert summ.status_code == 200
            assert summ.json()["open_incident_id"] is None
            assert summ.json()["org_scoped"] is True

            det = client.get(f"/v1/infra/storage/incidents/{inc_b}")
            assert det.status_code == 404
            rep = client.get(f"/v1/infra/storage/incidents/{inc_b}/report.md")
            assert rep.status_code == 404

        # 3. Instance admin with MFA session can mutate & inspect Org B incident.
        with world_b.acting_as(world_b.ctx(inst, mfa=True)):
            ack = client.post(f"/v1/infra/storage/incidents/{inc_b}/acknowledge", json={})
            assert ack.status_code == 200, ack.text
            det_ok = client.get(f"/v1/infra/storage/incidents/{inc_b}")
            assert det_ok.status_code == 200
            assert det_ok.json()["incident"]["id"] == inc_b
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_b})
            c.execute(text("DELETE FROM storage_incidents WHERE id IN (:a, :b)"),
                      {"a": inc_b, "b": inc_global})
            c.execute(text("DELETE FROM identity_instance_admins WHERE principal_id = :p"),
                      {"p": inst.id})


def test_infra_storage_report_md_scopes_paused_work_and_orgless_caller_fails_closed(client, engine):
    from datetime import datetime, timedelta, timezone
    from boss_engine.identity.principal import (
        AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus, SessionRef,
    )
    from boss_engine.services import storage_incidents

    world_a = IdentityWorld.create(engine)
    world_b = IdentityWorld.create(engine)
    admin_a = world_a.person("admin_a_rep")
    world_a.bind(admin_a, "admin")

    tag = uuid.uuid4().hex[:8]
    inc_a = f"si_http_a_{tag}"
    job_a = f"job_http_a_{tag}"
    job_b = f"job_http_ba_{tag}"
    host_a = f"host_a_{tag}"

    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO storage_incidents (id, host_id, host_label, status, severity, cause_key, "
            "cause_text, target_pct, project_id) VALUES "
            "(:id, :h, :h, 'resolved', 'critical', 'backups.no_retention', 'Org A inc', 25, :pa)"),
            {"id": inc_a, "h": host_a, "pa": world_a.project_a})
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, payload, status, last_error) VALUES "
            "(:ja, :pa, 'code.task', '{}'::jsonb, 'queued', CAST(:err AS JSONB)), "
            "(:jb, :pb, 'code.task', '{}'::jsonb, 'queued', CAST(:err AS JSONB))"),
            {"ja": job_a, "pa": world_a.project_a, "jb": job_b, "pb": world_b.project_a,
             "err": json.dumps({"code": "infra_hold", "incident_id": inc_a})})
        # Persist an install-wide report (with paused_runs=2) as resolve() does in production.
        inc_row = storage_incidents.get(c, inc_a)
        full_rep = storage_incidents.build_report(c, inc_row)
        assert full_rep["paused_runs"] == 2
        c.execute(text("UPDATE storage_incidents SET report = CAST(:r AS JSONB) WHERE id = :id"),
                  {"r": json.dumps(full_rep, default=str), "id": inc_a})

    now = datetime.now(timezone.utc)
    orgless_ctx = AuthContext(
        Principal(f"usr_orgless_{tag}", PrincipalKind.USER, None, PrincipalStatus.ACTIVE, "Orgless"),
        AuthMethod.SESSION,
        session=SessionRef(
            id=f"ses_orgless_{tag}", mfa_at=now, auth_methods=("password",),
            idle_expires_at=now + timedelta(hours=1),
            absolute_expires_at=now + timedelta(hours=8),
        ),
    )

    try:
        # Org A sees only its own paused job count (1) in report.md and detail JSON, never Org B's.
        with world_a.acting_as(world_a.ctx(admin_a)):
            rep = client.get(f"/v1/infra/storage/incidents/{inc_a}/report.md")
            assert rep.status_code == 200, rep.text
            assert "paused 1 run" in rep.text
            assert "Work paused: 1 " in rep.text
            assert "paused 2 runs" not in rep.text

            det = client.get(f"/v1/infra/storage/incidents/{inc_a}")
            assert det.status_code == 200, det.text
            assert det.json()["incident"]["report"]["paused_runs"] == 1
            assert len(det.json()["paused_work"]) == 1

        # An authenticated principal with org_id=None fails closed under enforce AND shadow modes.
        with world_a.acting_as(orgless_ctx):
            assert client.get("/v1/infra/storage/incidents/current").status_code == 401
        with world_a.acting_as(orgless_ctx, mode="shadow"):
            cur = client.get("/v1/infra/storage/incidents/current")
            assert cur.status_code == 200
            assert cur.json()["incidents"] == []
            assert cur.json()["hold"]["paused_runs"] == 0
            assert client.get("/v1/infra/storage/incidents").json()["incidents"] == []
            assert client.get("/v1/infra/storage/hold").json()["hold"]["paused_runs"] == 0
            assert client.get("/v1/infra/storage/summary", params={"host_id": host_a}).json()["open_incident_id"] is None
            assert client.get(f"/v1/infra/storage/incidents/{inc_a}").status_code == 404
            assert client.get(f"/v1/infra/storage/incidents/{inc_a}/report.md").status_code == 404
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM jobs WHERE id IN (:ja, :jb)"), {"ja": job_a, "jb": job_b})
            c.execute(text("DELETE FROM storage_incidents WHERE id = :a"), {"a": inc_a})
