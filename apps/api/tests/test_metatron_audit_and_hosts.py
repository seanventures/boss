"""Wave B tests: audit batch ingest, credential rejection, host listing & credential-lease."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.identity_fxd_support import session_ctx


def test_audit_batch_idempotency_and_credential_rejection(
    client: TestClient, engine, monkeypatch
):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    world = IdentityWorld.create(engine)
    operator = world.person("audit-op")
    world.bind(operator, "operator", project=world.project_a)

    ev_ok = f"ev_ok_{uuid4().hex[:8]}"
    ev_ticket = f"ev_tk_{uuid4().hex[:8]}"
    ev_bad_key = f"ev_bk_{uuid4().hex[:8]}"
    ev_bad_pat = f"ev_bp_{uuid4().hex[:8]}"
    ev_bad_url = f"ev_bu_{uuid4().hex[:8]}"

    payload = {
        "project_id": world.project_a,
        "session_id": "ses_audit_1",
        "events": [
            {
                "event_id": ev_ok,
                "ts": "2026-11-25T08:00:00Z",
                "kind": "tool_call",
                "tool": "ssh_run",
                "risk_class": "remote-read",
                "argv_redacted": ["sar", "-u", "1", "5"],
                "host": "web-1",
                "cookie_names": ["grafana_session"],
                "decision": "allow",
                "decided_by": "rule",
                "exit_code": 0,
            },
            {
                "event_id": ev_ticket,
                "ts": "2026-11-25T08:00:05Z",
                "kind": "tool_call",
                "ticket_id": "T-123",
                "tool": "ssh_run",
                "decision": "allow",
            },
            {
                "event_id": ev_bad_key,
                "ts": "2026-11-25T08:00:10Z",
                "kind": "http",
                "cookie_values": {"session": "supersecret"},
            },
            {
                "event_id": ev_bad_pat,
                "ts": "2026-11-25T08:00:15Z",
                "kind": "tool_call",
                "argv_redacted": ["curl", "-H", "Authorization: Bearer sk-ant-api03-secretvalue123456"],
            },
            {
                "event_id": ev_bad_url,
                "ts": "2026-11-25T08:00:20Z",
                "kind": "http",
                "path": "https://deploy:hunter2@registry.example.com/v2/",
            },
        ],
    }

    with world.acting_as(world.ctx(operator)):
        resp1 = client.post("/v1/metatron/audit", json=payload)
        assert resp1.status_code == 200, resp1.text
        body1 = resp1.json()
        assert set(body1["accepted"]) == {ev_ok, ev_ticket}
        assert {r["event_id"] for r in body1["rejected"]} == {ev_bad_key, ev_bad_pat, ev_bad_url}

        # Re-submitting the same batch is idempotent (no duplicate rows or 500s)
        resp2 = client.post("/v1/metatron/audit", json=payload)
        assert resp2.status_code == 200
        assert set(resp2.json()["accepted"]) == {ev_ok, ev_ticket}

        # Cross-project rejected
        assert (
            client.post(
                "/v1/metatron/audit",
                json={"project_id": world.project_b, "session_id": "s", "events": []},
            ).status_code
            == 403
        )

    with engine.connect() as c:
        stored = c.execute(
            text("SELECT event_id FROM metatron_audit_events WHERE project_id = :p"),
            {"p": world.project_a},
        ).scalars().all()
        assert set(stored) == {ev_ok, ev_ticket}
        attached = c.execute(
            text("SELECT count(*) FROM events WHERE project_id = :p AND event_type = 'metatron.audit_attached'"),
            {"p": world.project_a},
        ).scalar()
        assert attached == 1


def test_hosts_and_credential_lease_step_up_and_secret_access(
    client: TestClient, engine, monkeypatch
):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    world = IdentityWorld.create(engine)
    admin = world.person("host-admin")
    viewer = world.person("host-viewer")
    world.bind(admin, "admin", project=world.project_a)
    world.bind(viewer, "viewer", project=world.project_a)

    host_id = f"host_{uuid4().hex[:8]}"
    sref_id = f"sref_{uuid4().hex[:8]}"
    env_var = "BOSS_TEST_METATRON_SSH_KEY"
    fake_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest-lease-material\n-----END OPENSSH PRIVATE KEY-----"
    monkeypatch.setenv(env_var, fake_key)

    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO secret_refs (id, project_id, scope_id, ref_kind, ref_key, storage_backend, status, created_by) "
                "VALUES (:id, :p, :p, 'env', :rk, 'env', 'active', :cb)"
            ),
            {"id": sref_id, "p": world.project_a, "rk": env_var, "cb": admin.id},
        )
        c.execute(
            text(
                "INSERT INTO deployment_hosts "
                "(id, project_id, name, address, ssh_user, ssh_port, key_secret_ref, status, "
                " host_key_known_hosts, host_key_fingerprint) "
                "VALUES (:id, :p, 'web-1', '192.0.2.10', 'ops', 22, :sr, 'ready', "
                " '192.0.2.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey', 'SHA256:testfp')"
            ),
            {"id": host_id, "p": world.project_a, "sr": sref_id},
        )

    # 1. Viewer can list hosts (including host_key_known_hosts & fingerprint) but cannot lease credentials (403).
    with world.acting_as(world.ctx(viewer)):
        hresp = client.get("/v1/metatron/hosts", params={"project_id": world.project_a})
        assert hresp.status_code == 200, hresp.text
        hosts = hresp.json()["hosts"]
        found = next(h for h in hosts if h["id"] == host_id)
        assert found["host_key_fingerprint"] == "SHA256:testfp"
        assert found["host_key_known_hosts"].startswith("192.0.2.10 ")
        assert found["key_configured"] is True

        denied = client.post(
            f"/v1/metatron/hosts/{host_id}/credential-lease",
            json={"purpose": "diagnostics", "ttl_s": 60},
        )
        assert denied.status_code == 403

    # 2. Under enforce, boss_pat_ API key cannot satisfy step-up host:exec -> 401 step_up_required.
    pat_ctx = world.key_ctx(admin, ("host:exec", "host:read"))
    with world.acting_as(pat_ctx):
        step_up = client.post(
            f"/v1/metatron/hosts/{host_id}/credential-lease",
            json={"purpose": "diagnostics", "ttl_s": 60},
        )
        assert step_up.status_code == 401
        assert step_up.json()["error"] == "step_up_required"

    # 3. Fresh MFA session succeeds -> returns material with Cache-Control: no-store + receipts + secret_access_events.
    with world.acting_as(session_ctx(admin, mfa_age=timedelta(seconds=10))):
        lease_resp = client.post(
            f"/v1/metatron/hosts/{host_id}/credential-lease",
            json={"purpose": "run sar diagnostics", "ttl_s": 60, "session_id": "ses_m3"},
        )
        assert lease_resp.status_code == 200, lease_resp.text
        assert lease_resp.headers.get("cache-control") == "no-store"
        lease = lease_resp.json()
        assert lease["kind"] == "ssh_key"
        assert lease["material"] == fake_key
        assert lease["host_key_fingerprint"] == "SHA256:testfp"

    with engine.connect() as c:
        row = c.execute(
            text("SELECT redeemed_at, receipt_id FROM metatron_credential_leases WHERE id = :id"),
            {"id": lease["lease_id"]},
        ).mappings().one()
        assert row["redeemed_at"] is not None
        assert row["receipt_id"]
        ev_count = c.execute(
            text("SELECT count(*) FROM secret_access_events WHERE secret_ref_id = :sr AND outcome = 'resolved'"),
            {"sr": sref_id},
        ).scalar()
        assert ev_count >= 1
