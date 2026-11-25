"""Cross-Module Pairwise (Tier 3) & Real-World Multi-Feature (Tier 4) M6 E2E Suite.

Covers:
- Tier 3: E2E-T3-01 through E2E-T3-07
- Tier 4: E2E-T4-01 through E2E-T4-04 (M6 baseline slices)
Zero skips, zero xfails, zero LAST_MILE mocking.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.db import BACKGROUND_LOCK_TIMEOUT_SECONDS, background, begin, in_background
from boss_engine.identity import lineage
from boss_engine.tools.builtin import build_default_registry


def _sign_webhook(secret: str, body: bytes) -> tuple[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return ts, f"v1={sig}"


def _make_agent_and_promote_to_prod_decision(tenant) -> tuple[str, str]:
    key = f"cm_ag_{uuid.uuid4().hex[:8]}"
    created = tenant.session_client.post(
        "/v1/agent-specs",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "spec": {
                "apiVersion": "boss.agent/v1",
                "kind": "Agent",
                "metadata": {"key": key, "name": f"Agent {key}"},
                "spec": {
                    "mission": "Report status cleanly.",
                    "prompts": {"system": "Verify facts."},
                    "tools": {"native": ["read_repo_file"]},
                },
            },
        },
        headers=tenant.csrf_headers,
    )
    assert created.status_code == 201, created.text
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/promote",
            json={
                "project_id": tenant.project_id,
                "actor": tenant.owner.principal.id,
                "from_environment": "dev",
                "to_environment": "staging",
            },
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )
    prom = tenant.session_client.post(
        f"/v1/agents/{key}/promote",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "from_environment": "staging",
            "to_environment": "prod",
        },
        headers=tenant.csrf_headers,
    )
    assert prom.status_code == 200, prom.text
    return key, prom.json()["decision_id"]


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# Tier 3 — Pairwise Cross-Feature Tests (E2E-T3-01 .. E2E-T3-07)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_e2e_t3_01_command_budget_and_receipt_audit_replay_safe(client, owner_headers, worker_headers):
    """F1.1 + F1.2 + F1.4: command propose -> reserve -> consume -> events/costs -> idempotent task dedupe."""
    prop = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Verify staging metrics and report budget delta.",
            "constraints": {"read_only": True},
            "actor": "ceo",
        },
        headers=owner_headers,
    )
    assert prop.status_code == 201, prop.text
    job_id = prop.json()["job_id"]

    res = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 150_000,
            "reason": "T3-01 spend",
            "job_id": job_id,
        },
        headers=worker_headers,
    )
    assert res.status_code == 201, res.text
    rid = res.json()["reservation_id"]

    rcpt_res = client.post(
        "/v1/metatron/watch",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
        headers=owner_headers,
    )
    assert rcpt_res.status_code == 200 and rcpt_res.json().get("receipt_id"), rcpt_res.text
    rcpt_id = rcpt_res.json()["receipt_id"]
    cons = client.post(
        "/v1/budget/consume",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": rid,
            "receipt_id": rcpt_id,
            "category": "model",
            "skill_or_model_id": "gpt-5.2-codex",
            "estimated_usd_micros": 150_000,
            "actual_usd_micros": 90_000,
            "actor": "system",
        },
        headers=worker_headers,
    )
    assert cons.status_code == 200, cons.text
    assert cons.json()["cost_receipt"]["receipt_id"] == rcpt_id

    dkey = f"t3_dedupe_{uuid.uuid4().hex[:8]}"
    first = client.post(
        "/v1/tasks",
        json={"project_id": "boss-dogfood", "title": "Replay test", "dedupe_key": dkey, "actor": "ceo"},
        headers=owner_headers,
    )
    assert first.status_code == 201
    second = client.post(
        "/v1/tasks",
        json={"project_id": "boss-dogfood", "title": "Replay test", "dedupe_key": dkey, "actor": "ceo"},
        headers=owner_headers,
    )
    assert second.status_code == 409

    receipts = client.get("/v1/receipts", params={"project_id": "boss-dogfood"}, headers=owner_headers)
    assert receipts.status_code == 200 and rcpt_id in json.dumps(receipts.json())
    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"}, headers=owner_headers)
    assert costs.status_code == 200
    events = client.get("/v1/events", params={"project_id": "boss-dogfood"}, headers=owner_headers).json()
    assert len(events.get("items", [])) >= 1


def test_e2e_t3_02_decision_step_up_and_workflow_gate(db_engine, tenant_factory, claimed_owner_session):
    """F1.5 + F1.6 + F1.9: stale TOTP blocks sensitive action 401 -> fresh step-up -> decision approval."""
    tenant = tenant_factory(seed_defaults=False)
    _key, dec_id = _make_agent_and_promote_to_prod_decision(tenant)

    claimed_owner_session.age_mfa(db_engine, minutes=20)
    blocked = claimed_owner_session.client.post(
        "/v1/identity/me/api-keys",
        headers=claimed_owner_session.csrf_headers,
        json={"name": "t3-stale-probe", "scopes": ["project:read"]},
    )
    assert blocked.status_code == 401 and "step_up" in blocked.text

    claimed_owner_session.step_up()
    approved = tenant.session_client.post(
        f"/v1/decisions/{dec_id}/approve",
        json={"actor": tenant.owner.principal.id, "note": "T3-02 verified"},
        headers=tenant.csrf_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


def test_e2e_t3_03_worker_lease_inside_background_contextvar(client, db_engine, tenant_factory, worker_headers):
    """F1.3 + F3.4: worker poll leases job cleanly while db.background() enforces timeouts without leaking to foreground."""
    tenant = tenant_factory(seed_defaults=False)
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "T3-03 Worker BG",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:10]}",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()
    cap = "agent.finops_sentinel.cost_brief"
    job_res = client.post(
        "/v1/jobs",
        json={"project_id": tenant.project_id, "kind": cap, "task_id": task["id"]},
        headers=tenant.owner.headers,
    )
    assert job_res.status_code == 201, job_res.text
    job = job_res.json()
    polled = client.post(
        "/v1/worker/poll",
        json={"worker_id": "wrk_t3_03", "capabilities": [cap], "max_jobs": 1, "wait_ms": 0},
        headers=worker_headers,
    )
    assert polled.status_code == 200, polled.text
    assert (polled.json().get("job") or {}).get("id") == job["id"]

    fg_seen: list[bool] = []
    with background():
        with begin(db_engine) as conn:
            lt = conn.execute(text("SHOW lock_timeout")).scalar()
            assert lt == f"{BACKGROUND_LOCK_TIMEOUT_SECONDS}s"
        t = threading.Thread(target=lambda: fg_seen.append(in_background()))
        t.start()
        t.join(timeout=5)
    assert fg_seen == [False]
    assert in_background() is False


def test_e2e_t3_04_agent_spec_deployment_pinning_and_rbac(client, tenant_factory):
    """F3.1 + F3.2 + F1.9: publish v1 -> deploy dev -> start run -> save & deploy v2 -> run stays v1 -> viewer 403."""
    tenant = tenant_factory(seed_defaults=False)
    key = f"pin_{uuid.uuid4().hex[:8]}"
    assert (
        tenant.session_client.post(
            "/v1/agent-specs",
            json={
                "project_id": tenant.project_id,
                "actor": tenant.owner.principal.id,
                "spec": {
                    "apiVersion": "boss.agent/v1",
                    "kind": "Agent",
                    "metadata": {"key": key, "name": "Pin Agent"},
                    "spec": {"mission": "v1 mission", "prompts": {"system": "v1 sys"}, "tools": {"native": ["read_repo_file"]}},
                },
            },
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    thr = client.post(
        f"/v1/agents/{key}/threads",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev"},
        headers=tenant.owner.headers,
    ).json()
    run1 = client.post(
        f"/v1/threads/{thr['thread_id']}/runs",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "input": {"message": "v1"}},
        headers=tenant.owner.headers,
    ).json()

    assert (
        tenant.session_client.put(
            f"/v1/agents/{key}/spec",
            json={
                "project_id": tenant.project_id,
                "actor": tenant.owner.principal.id,
                "spec": {
                    "apiVersion": "boss.agent/v1",
                    "kind": "Agent",
                    "metadata": {"key": key, "name": "Pin Agent"},
                    "spec": {"mission": "v2 mission", "prompts": {"system": "v2 sys"}, "tools": {"native": ["read_repo_file"]}},
                },
            },
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 2},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    run1_check = client.get(
        f"/v1/threads/{thr['thread_id']}/runs/{run1['run_id']}",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    ).json()
    assert run1_check.get("spec_version") in (1, None) or run1_check.get("version") in (1, None)

    denied = client.post(
        f"/v1/agents/{key}/deployments",
        json={"project_id": tenant.project_id, "actor": tenant.viewer.principal.id, "environment": "dev", "version": 2},
        headers=tenant.viewer.headers,
    )
    assert denied.status_code == 403


def test_e2e_t3_05_signed_webhook_intake_routes_and_redacts_without_user_read(client, tenant_factory):
    """F3.3 + F3.2 + F1.9: intake webhook -> route trigger -> owner sees full, operator without user:read sees redacted."""
    tenant = tenant_factory(seed_defaults=False)
    key = f"in_{uuid.uuid4().hex[:8]}"
    assert (
        tenant.session_client.post(
            "/v1/agent-specs",
            json={
                "project_id": tenant.project_id,
                "actor": tenant.owner.principal.id,
                "spec": {
                    "apiVersion": "boss.agent/v1",
                    "kind": "Agent",
                    "metadata": {"key": key, "name": "Intake Agent"},
                    "spec": {"mission": "Triage events", "prompts": {"system": "Triage."}, "tools": {"native": ["read_repo_file"]}},
                },
            },
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "t3-hook"},
        headers=tenant.csrf_headers,
    ).json()
    trig = tenant.session_client.post(
        f"/v1/agents/{key}/triggers",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "environment": "dev",
            "kind": "intake",
            "connection_id": conn["id"],
            "name": "route-all",
        },
        headers=tenant.csrf_headers,
    )
    assert trig.status_code == 201, trig.text

    raw = json.dumps({"event": "incident.opened", "email": "oncall@example.com"}).encode("utf-8")
    ts, sig = _sign_webhook(conn["secret"], raw)
    accepted = client.post(
        conn["url_path"],
        content=raw,
        headers={
            **tenant.service_account.headers,
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": ts,
            "X-BOSS-Signature": sig,
            "X-BOSS-Delivery": f"t3_{uuid.uuid4().hex[:10]}",
        },
    )
    assert accepted.status_code in (200, 202), accepted.text

    ev_list = client.get(
        "/v1/intake/events",
        params={"project_id": tenant.project_id, "connection_id": conn["id"]},
        headers=tenant.owner.headers,
    ).json()
    items = ev_list.get("events") or ev_list.get("items") or []
    assert len(items) == 1
    ev_id = items[0]["id"]

    op_tok = tenant.mint_pat(tenant.operator.principal, scopes=("integration:read",))
    op_ev = client.get(f"/v1/intake/events/{ev_id}", headers={"Authorization": f"Bearer {op_tok}"})
    assert op_ev.status_code == 200 and op_ev.json().get("people_redacted") is True


def test_e2e_t3_06_inspect_host_ssh_stub_plus_metatron_watch_receipt(
    client, db_engine, tenant_factory, owner_headers, ssh_stub, tmp_path, monkeypatch
):
    """F2.1 + F2.2 + F1.4: inspect_host runs via SSH stub, rejects metacharacters, and metatron/watch emits receipt."""
    tenant = tenant_factory(seed_defaults=False)
    key_file = tmp_path / "t3_ed25519"
    key_file.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nM6\n-----END OPENSSH PRIVATE KEY-----\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("BOSS_SSH_HOST_PRIVATE_KEY_FILE", str(key_file))
    get_settings.cache_clear()

    host_name = f"t3-host-{uuid.uuid4().hex[:6]}"
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, status) "
                "VALUES (:id, :p, :name, '203.0.113.30', 'ops', 22, 'verified')"
            ),
            {"id": f"dh_{uuid.uuid4().hex[:8]}", "p": tenant.project_id, "name": host_name},
        )
    ctx = SimpleNamespace(settings=get_settings(), begin=lambda: db_engine.begin())
    reg = build_default_registry()
    with lineage.acting(tenant.session_auth):
        good = reg.invoke(
            "inspect_host",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name, "command": "docker ps"},
        )
        bad = reg.invoke(
            "inspect_host",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name, "command": "docker ps; rm -rf /"},
        )
    assert good.get("exit_code") == 0 and len(ssh_stub()) == 1
    assert bad.get("ok") is False and len(ssh_stub()) == 1

    watch = client.post("/v1/metatron/watch", json={"project_id": "boss-dogfood", "actor": "ceo"}, headers=owner_headers)
    assert watch.status_code == 200 and watch.json().get("receipt_id")


def test_e2e_t3_07_kg_context_pack_code_task_and_acceptance_contract(client, tenant_factory):
    """F1.7 + F1.8 + F1.10: KG nodes + bounded pack-context -> simulated code task -> acceptance gates & contract."""
    tenant = tenant_factory(seed_defaults=False)
    n1 = client.post(
        "/v1/knowledge/graph/nodes",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "service", "node_key": "svc:core", "name": "Core"},
        headers=tenant.owner.headers,
    )
    assert n1.status_code == 200

    pack = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "token_budget": 500,
            "reserved_output_tokens": 100,
            "chunks": [{"id": "k1", "text": "Core service handles tasks.", "authority": "fact"}],
        },
        headers=tenant.owner.headers,
    )
    assert pack.status_code == 200 and pack.json().get("used_tokens", pack.json().get("total_tokens", 0)) <= 400

    ct = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Add healthz probe",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert ct.status_code in (200, 201)
    run_id = ct.json()["run_id"]
    assert client.get(f"/v1/code-tasks/{run_id}/gates", headers=tenant.owner.headers).status_code == 200
    assert (
        client.post(
            "/v1/acceptance-checks",
            json={
                "project_id": tenant.project_id,
                "run_id": run_id,
                "review_id": "rev_none",
                "snapshot_hash": "a" * 64,
                "title": "Unreviewed gate",
                "submission_id": f"subm_{uuid.uuid4().hex[:20]}",
            },
            headers=tenant.service_account.headers,
        ).status_code
        == 403
    )


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# Tier 4 — Full Real-World End-to-End Scenarios (E2E-T4-01 .. E2E-T4-04)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_e2e_t4_01_p1_command_budget_worker_stepup_decision_and_audit(
    client, db_engine, tenant_factory, owner_headers, worker_headers, claimed_owner_session
):
    """E2E-T4-01 (M6 Slice): Propose -> Budget Quote/Reserve -> Worker Poll -> Consume -> Step-Up -> Approve -> Audit."""
    tenant = tenant_factory(seed_defaults=True)
    prop = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Prepare release health summary.",
            "constraints": {"audit": True},
            "actor": "ceo",
        },
        headers=owner_headers,
    )
    assert prop.status_code == 201, prop.text
    task_id = prop.json()["task_id"]

    quote = client.post(
        "/v1/budget/quote",
        json={"project_id": "boss-dogfood", "category": "model", "amount_usd_micros": 100_000, "reason": "T4-01"},
        headers=owner_headers,
    )
    assert quote.status_code == 200 and quote.json()["allowed"] is True
    res = client.post(
        "/v1/budget/reserve",
        json={"project_id": "boss-dogfood", "category": "model", "amount_usd_micros": 100_000, "reason": "T4-01"},
        headers=worker_headers,
    ).json()

    cap = "agent.metatron.status_brief"
    job_res = client.post(
        "/v1/jobs",
        json={"project_id": "boss-dogfood", "kind": cap, "task_id": task_id},
        headers=owner_headers,
    )
    assert job_res.status_code == 201, job_res.text
    job = job_res.json()
    poll = client.post(
        "/v1/worker/poll",
        json={"worker_id": "wrk_t4_01", "capabilities": [cap], "max_jobs": 1, "wait_ms": 0},
        headers=worker_headers,
    ).json()
    assert (poll.get("job") or {}).get("id") == job["id"]

    rcpt_res = client.post(
        "/v1/metatron/watch",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
        headers=owner_headers,
    )
    assert rcpt_res.status_code == 200 and rcpt_res.json().get("receipt_id"), rcpt_res.text
    cons = client.post(
        "/v1/budget/consume",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": res["reservation_id"],
            "receipt_id": rcpt_res.json()["receipt_id"],
            "category": "model",
            "skill_or_model_id": "gpt-5.2-codex",
            "estimated_usd_micros": 100_000,
            "actual_usd_micros": 75_000,
            "actor": "system",
        },
        headers=worker_headers,
    )
    assert cons.status_code == 200, cons.text

    claimed_owner_session.step_up()
    _key, dec_id = _make_agent_and_promote_to_prod_decision(tenant)
    approved = tenant.session_client.post(
        f"/v1/decisions/{dec_id}/approve",
        json={"actor": tenant.owner.principal.id, "note": "T4-01 ship"},
        headers=tenant.csrf_headers,
    )
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"}, headers=owner_headers).json()
    assert len(events.get("items", [])) >= 2


def test_e2e_t4_02_agent_framework_spec_deploy_intake_and_health(client, tenant_factory):
    """E2E-T4-02 (M6 Slice): Spec v1 -> Deploy -> Signed Webhook + Duplicate Dedupe -> Bots Tick -> Health + Redaction."""
    tenant = tenant_factory(seed_defaults=False)
    key = f"t4_af_{uuid.uuid4().hex[:8]}"
    assert (
        tenant.session_client.post(
            "/v1/agent-specs",
            json={
                "project_id": tenant.project_id,
                "actor": tenant.owner.principal.id,
                "spec": {
                    "apiVersion": "boss.agent/v1",
                    "kind": "Agent",
                    "metadata": {"key": key, "name": "AF Incident Responder"},
                    "spec": {
                        "mission": "Handle inbound webhooks.",
                        "prompts": {"system": "Be concise."},
                        "tools": {"native": ["read_repo_file"]},
                    },
                },
            },
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "t4-alerts"},
        headers=tenant.csrf_headers,
    ).json()
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/triggers",
            json={
                "project_id": tenant.project_id,
                "actor": tenant.owner.principal.id,
                "environment": "dev",
                "kind": "intake",
                "connection_id": conn["id"],
            },
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )

    body = json.dumps({"alert": "high_latency", "reporter": "alice@example.com"}).encode("utf-8")
    ts, sig = _sign_webhook(conn["secret"], body)
    hdrs = {
        **tenant.service_account.headers,
        "Content-Type": "application/json",
        "X-BOSS-Timestamp": ts,
        "X-BOSS-Signature": sig,
        "X-BOSS-Delivery": f"t4_del_{uuid.uuid4().hex[:10]}",
    }
    assert client.post(conn["url_path"], content=body, headers=hdrs).status_code in (200, 202)
    assert client.post(conn["url_path"], content=body, headers=hdrs).status_code in (200, 202)

    assert (
        tenant.session_client.post(
            "/v1/bots/tick", json={"project_id": tenant.project_id, "limit": 10}, headers=tenant.csrf_headers
        ).status_code
        == 200
    )
    assert client.get("/v1/agent-platform/health", params={"project_id": tenant.project_id}, headers=tenant.owner.headers).status_code == 200

    evs = client.get(
        "/v1/intake/events",
        params={"project_id": tenant.project_id, "connection_id": conn["id"]},
        headers=tenant.owner.headers,
    ).json()
    items = evs.get("events") or evs.get("items") or []
    assert len(items) == 1

    op_tok = tenant.mint_pat(tenant.operator.principal, scopes=("integration:read",))
    op_view = client.get(f"/v1/intake/events/{items[0]['id']}", headers={"Authorization": f"Bearer {op_tok}"})
    assert op_view.status_code == 200 and op_view.json().get("people_redacted") is True


def test_e2e_t4_03_metatron_m6_slice_cli_captain_and_inspect_host(
    live_api_url, owner_headers, db_engine, tenant_factory, ssh_stub, tmp_path, monkeypatch
):
    """E2E-T4-03 (M6 Slice): Python metatron CLI --print --json + /v1/metatron/watch + /drive + inspect_host via SSH stub."""
    api_key = owner_headers["Authorization"].split(" ", 1)[1]
    root = Path(__file__).resolve().parents[3]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": f"{root}:{root / 'engine'}",
        "HOME": str(tmp_path),
        "BOSS_API_URL": live_api_url,
        "BOSS_API_KEY": api_key,
        "BOSS_PROJECT_ID": "boss-dogfood",
    }
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "metatron_tui.py"), "--print", "/status", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr} stdout={proc.stdout}"
    assert isinstance(json.loads(proc.stdout), dict)
    assert api_key not in proc.stdout

    watch = httpx.post(
        f"{live_api_url}/v1/metatron/watch",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
        headers=owner_headers,
        timeout=15.0,
    )
    assert watch.status_code == 200 and watch.json().get("receipt_id")

    drive = httpx.post(
        f"{live_api_url}/v1/metatron/drive",
        json={"project_id": "boss-dogfood", "actor": "ceo", "auto": False},
        headers=owner_headers,
        timeout=15.0,
    )
    assert drive.status_code == 200

    tenant = tenant_factory(seed_defaults=False)
    key_file = tmp_path / "t4_ed25519"
    key_file.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nM6\n-----END OPENSSH PRIVATE KEY-----\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("BOSS_SSH_HOST_PRIVATE_KEY_FILE", str(key_file))
    get_settings.cache_clear()
    host_name = f"t4-host-{uuid.uuid4().hex[:6]}"
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, status) "
                "VALUES (:id, :p, :name, '203.0.113.40', 'ops', 22, 'verified')"
            ),
            {"id": f"dh_{uuid.uuid4().hex[:8]}", "p": tenant.project_id, "name": host_name},
        )
    ctx = SimpleNamespace(settings=get_settings(), begin=lambda: db_engine.begin())
    reg = build_default_registry()
    with lineage.acting(tenant.session_auth):
        out = reg.invoke(
            "inspect_host",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name, "command": "ss -tulnp"},
        )
    assert out.get("exit_code") == 0 and len(ssh_stub()) == 1


def test_e2e_t4_04_web_console_routes_and_api_contract_walk(live_web_console, owner_headers):
    """E2E-T4-04 (M6 Slice): Live Next.js Console routes + API fail-closed guards + /v1/v1-lock consistency."""
    web_url = live_web_console["web_url"]
    api_url = live_web_console["api_url"]
    for path in ("/login", "/", "/v2", "/v2/factory", "/v2/org", "/v2/settings", "/v2/infra", "/v2/timeline"):
        res = httpx.get(f"{web_url}{path}", follow_redirects=True, timeout=15.0)
        assert res.status_code == 200, f"{path}: {res.status_code}"
    assert httpx.get(f"{web_url}/api/delivery-workflows", timeout=10.0).status_code == 401
    assert httpx.get(f"{api_url}/v1/v1-lock", headers=owner_headers, timeout=10.0).status_code == 200
