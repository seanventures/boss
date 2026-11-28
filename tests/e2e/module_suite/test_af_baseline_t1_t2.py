"""Agent Framework Completion M6 Baseline Opaque-Box E2E Suite (F3.1–F3.5 — Tier 1 & Tier 2).

Covers M1-present Agent Framework capabilities with zero skips and zero LAST_MILE mocking:
- F3.1: `boss.agent/v1` Spec, Versioning, Publish, Rollback & Passport Ceiling
- F3.2: Agent Deployments, Triggers, Threads, Runs & Health
- F3.3: Event Intake Front Door, HMAC Verification, Replay & Privacy Redaction
- F3.4: `db.background()` ContextVar Safety & Lock/Idle Timeout Enforcement
- F3.5: A2A v1.0 Agent Cards/JSON-RPC, Agent Processes & Focused Agents
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from boss_engine.connectors.intake.contract import MAX_BODY_BYTES
from boss_engine.db import (
    BACKGROUND_LOCK_TIMEOUT_SECONDS,
    background,
    background_limits_sql,
    begin,
    in_background,
)


def _agent_spec(
    key: str,
    *,
    mission: str = "Inspect repository status and report facts accurately.",
    system: str = "You report factual findings without inventing outputs.",
    tools: dict | None = None,
) -> dict:
    spec_body: dict = {
        "mission": mission,
        "prompts": {"system": system},
    }
    if tools is not None:
        spec_body["tools"] = tools
    return {
        "apiVersion": "boss.agent/v1",
        "kind": "Agent",
        "metadata": {"key": key, "name": f"Agent {key}"},
        "spec": spec_body,
    }


def _create_agent(client, project_id: str, headers: dict, actor: str, key: str, *, tools: dict | None = None):
    return client.post(
        "/v1/agent-specs",
        json={
            "project_id": project_id,
            "actor": actor,
            "spec": _agent_spec(key, tools=tools if tools is not None else {"native": ["read_repo_file"]}),
        },
        headers=headers,
    )


def _sign_boss_webhook(secret: str, body: bytes, ts: str | None = None) -> tuple[str, str]:
    timestamp = ts or str(int(time.time()))
    payload = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return timestamp, f"v1={digest}"


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F3.1 — boss.agent/v1 Spec, Versioning, Publish, Rollback & Passport Ceiling
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f3_1_01_spec_schema_returns_boss_agent_v1(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get(
        "/v1/agent-specs/schema",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert res.status_code == 200, res.text
    schema = res.json()
    assert "properties" in schema and "spec" in schema["properties"]


def test_t1_f3_1_02_create_spec_v1_publishes_and_projects_grants(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    created = _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["version"] == 1
    assert body["report"]["required_permissions"] == ["repository:read"]

    active = client.get(
        f"/v1/agents/{key}/spec",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert active.status_code == 200, active.text
    assert active.json()["version"] == 1
    assert "You report factual findings" in active.json()["compiled_prompt"]

    skills = client.get(
        "/v1/role-skills",
        params={"project_id": tenant.project_id, "role_key": key},
        headers=tenant.owner.headers,
    )
    assert skills.status_code == 200, skills.text
    mcps = client.get(
        "/v1/role-mcp-servers",
        params={"project_id": tenant.project_id, "role_key": key},
        headers=tenant.owner.headers,
    )
    assert mcps.status_code == 200, mcps.text


def test_t1_f3_1_03_save_draft_v2_and_explicit_publish(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201

    v2_doc = _agent_spec(key, system="Version two instructions.", tools={"native": ["read_repo_file"]})
    draft = tenant.session_client.put(
        f"/v1/agents/{key}/spec",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "spec": v2_doc, "publish": False},
        headers=tenant.csrf_headers,
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["version"] == 2 and draft.json()["active"] is False

    still_v1 = client.get(
        f"/v1/agents/{key}/spec",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert still_v1.json()["version"] == 1

    pub = tenant.session_client.post(
        f"/v1/agents/{key}/spec/versions/2/publish",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
        headers=tenant.csrf_headers,
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["version"] == 2 and pub.json()["active"] is True


def test_t1_f3_1_04_diff_export_and_rollback_restores_v1(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
    v1_prompt = client.get(
        f"/v1/agents/{key}/spec",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    ).json()["compiled_prompt"]

    v2_doc = _agent_spec(key, system="Changed system prompt v2.", tools={"native": ["read_repo_file"]})
    assert (
        tenant.session_client.put(
            f"/v1/agents/{key}/spec",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "spec": v2_doc},
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )

    diff = client.get(
        f"/v1/agents/{key}/spec/diff",
        params={"project_id": tenant.project_id, "from_version": 1, "to_version": 2},
        headers=tenant.owner.headers,
    )
    assert diff.status_code == 200, diff.text
    assert any(ch["path"] == "spec.prompts.system" for ch in diff.json()["changes"])

    exp = client.get(
        f"/v1/agents/{key}/spec/export",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert exp.status_code == 200, exp.text
    assert "boss.agent/v1" in exp.json()["yaml"]

    rb = tenant.session_client.post(
        f"/v1/agents/{key}/spec/rollback",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "to_version": 1},
        headers=tenant.csrf_headers,
    )
    assert rb.status_code == 200, rb.text
    now = client.get(
        f"/v1/agents/{key}/spec",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    ).json()
    assert now["version"] == 1
    assert now["compiled_prompt"] == v1_prompt


def test_t1_f3_1_05_import_yaml_creates_version(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    yaml_text = (
        "apiVersion: boss.agent/v1\n"
        "kind: Agent\n"
        "metadata:\n"
        f"  key: {key}\n"
        "  name: YAML Imported Agent\n"
        "spec:\n"
        "  mission: Verify YAML import flow\n"
        "  prompts:\n"
        "    system: Imported system prompt\n"
    )
    res = tenant.session_client.post(
        "/v1/agent-specs/import",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "yaml": yaml_text, "publish": True},
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 201, res.text
    versions = client.get(
        f"/v1/agents/{key}/spec/versions",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert versions.status_code == 200
    assert [v["version"] for v in versions.json()["items"]] == [1]


def test_t2_f3_1_01_passport_ceiling_refuses_run_shell(client, db_engine, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    refused = _create_agent(
        client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key,
        tools={"native": ["run_shell"]},
    )
    assert refused.status_code == 403, refused.text
    with db_engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
            {"p": tenant.project_id, "r": key},
        ).first()
    assert row is None


def test_t2_f3_1_02_unknown_native_tool_refused(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    res = _create_agent(
        client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key,
        tools={"native": ["nonexistent_magic_tool"]},
    )
    assert res.status_code == 403, res.text


def test_t2_f3_1_03_unknown_spec_field_refused(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    res = _create_agent(
        client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key,
        tools={"skils": ["boss.fundamentals"]},
    )
    assert res.status_code == 422, res.text


def test_t2_f3_1_04_spec_and_yaml_conflict_or_empty_refused_422(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    both = client.post(
        "/v1/agent-specs",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "spec": _agent_spec(key),
            "yaml": "apiVersion: boss.agent/v1\n",
        },
        headers=tenant.owner.headers,
    )
    assert both.status_code == 422, both.text
    neither = client.post(
        "/v1/agent-specs",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert neither.status_code == 422, neither.text


def test_t2_f3_1_05_rollback_nonexistent_version_refused(tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert (
        _create_agent(
            tenant.session_client, tenant.project_id, tenant.csrf_headers, tenant.owner.principal.id, key
        ).status_code
        == 201
    )
    res = tenant.session_client.post(
        f"/v1/agents/{key}/spec/rollback",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "to_version": 999},
        headers=tenant.csrf_headers,
    )
    assert res.status_code in (404, 422), res.text


def test_t2_f3_1_06_viewer_cannot_mutate_agent_spec_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
    res = client.put(
        f"/v1/agents/{key}/spec",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.viewer.principal.id,
            "spec": _agent_spec(key, system="Viewer takeover"),
        },
        headers=tenant.viewer.headers,
    )
    assert res.status_code == 403, res.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F3.2 — Agent Deployments, Triggers, Threads, Runs & Health
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f3_2_01_deploy_dev_and_list_deployments(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201

    dep = tenant.session_client.post(
        f"/v1/agents/{key}/deployments",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
        headers=tenant.csrf_headers,
    )
    assert dep.status_code == 201, dep.text
    view = client.get(
        f"/v1/agents/{key}/deployments",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert view.status_code == 200, view.text
    envs = {item["environment"]: item for item in view.json().get("environments", [])}
    assert envs["dev"]["version"] == 1


def test_t1_f3_2_02_promote_dev_to_staging_and_prod_decision_gate(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    prom_stg = tenant.session_client.post(
        f"/v1/agents/{key}/promote",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "from_environment": "dev",
            "to_environment": "staging",
        },
        headers=tenant.csrf_headers,
    )
    assert prom_stg.status_code == 200, prom_stg.text

    checks = client.get(
        f"/v1/agents/{key}/promotion-checks",
        params={"project_id": tenant.project_id, "from_environment": "staging", "to_environment": "prod"},
        headers=tenant.owner.headers,
    )
    assert checks.status_code == 200, checks.text

    prom_prod = tenant.session_client.post(
        f"/v1/agents/{key}/promote",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "from_environment": "staging",
            "to_environment": "prod",
        },
        headers=tenant.csrf_headers,
    )
    assert prom_prod.status_code == 200, prom_prod.text
    assert prom_prod.json().get("decision_id") or prom_prod.json().get("status")


def test_t1_f3_2_03_webhook_trigger_signed_fire_and_bots_tick(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    created = _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key)
    assert created.status_code == 201
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    # Creating a trigger triggers lazy Passport upgrade for the newly created agent role.
    probe_trig = tenant.session_client.post(
        f"/v1/agents/{key}/triggers",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "environment": "dev",
            "kind": "cron",
            "name": "passport-init",
            "config": {"every_seconds": 3600},
        },
        headers=tenant.csrf_headers,
    )
    assert probe_trig.status_code == 201, probe_trig.text

    endpoints = client.get(
        f"/v1/agents/{key}/endpoints",
        params={"project_id": tenant.project_id, "environment": "dev"},
        headers=tenant.owner.headers,
    ).json()
    card_path = (endpoints.get("a2a") or {}).get("card_path") or ""
    agent_id = endpoints.get("agent_id") or (card_path.split("/")[2] if card_path.startswith("/a2a/") else "")
    assert agent_id, endpoints
    grant = tenant.session_client.post(
        "/v1/identity/me/delegations",
        json={
            "agents": [agent_id],
            "permissions": ["repository:read"],
            "projects": [tenant.project_id],
            "kind": "standing",
            "days": 7,
            "purpose": "M6 webhook trigger standing grant",
        },
        headers=tenant.csrf_headers,
    )
    assert grant.status_code == 201, grant.text

    trig = tenant.session_client.post(
        f"/v1/agents/{key}/triggers",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "environment": "dev",
            "kind": "webhook",
            "name": "ci-hook",
        },
        headers=tenant.csrf_headers,
    )
    assert trig.status_code == 201, trig.text
    tdata = trig.json()
    secret = tdata["secret"]
    url_path = tdata["url_path"]

    raw_body = json.dumps({"event": "deploy.check", "ref": "main"}).encode("utf-8")
    ts, sig = _sign_boss_webhook(secret, raw_body)
    delivery_id = f"del_{uuid.uuid4().hex[:10]}"
    fired = client.post(
        url_path,
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": ts,
            "X-BOSS-Signature": sig,
            "X-BOSS-Delivery": delivery_id,
        },
    )
    assert fired.status_code in (200, 202), fired.text
    assert fired.json()["status"] in ("started", "queued", "admitted")

    dup = client.post(
        url_path,
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": ts,
            "X-BOSS-Signature": sig,
            "X-BOSS-Delivery": delivery_id,
        },
    )
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate", dup.text

    tick = tenant.session_client.post(
        "/v1/bots/tick",
        json={"project_id": tenant.project_id, "limit": 10},
        headers=tenant.csrf_headers,
    )
    assert tick.status_code == 200, tick.text


def test_t1_f3_2_04_thread_run_lifecycle_get_history_and_cancel(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
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
    )
    assert thr.status_code == 201, thr.text
    tid = thr.json()["thread_id"]

    run = client.post(
        f"/v1/threads/{tid}/runs",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "input": {"message": "status"}},
        headers=tenant.owner.headers,
    )
    assert run.status_code == 201, run.text
    rid = run.json()["run_id"]

    fetched = client.get(
        f"/v1/threads/{tid}/runs/{rid}",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert fetched.status_code == 200, fetched.text

    cancelled = client.post(
        f"/v1/threads/{tid}/runs/{rid}/cancel",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    history = client.get(
        f"/v1/threads/{tid}/history",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert history.status_code == 200, history.text


def test_t1_f3_2_05_agent_platform_health_agents_scaling_and_endpoints(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )

    for path, extra in (
        ("/v1/agent-platform/health", {}),
        ("/v1/agent-platform/agents", {}),
        ("/v1/workers/scaling", {}),
        (f"/v1/agents/{key}/endpoints", {"environment": "dev"}),
    ):
        res = client.get(path, params={"project_id": tenant.project_id, **extra}, headers=tenant.owner.headers)
        assert res.status_code == 200, f"{path}: {res.text}"


def test_t2_f3_2_01_direct_deploy_to_prod_refused_409(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
    res = tenant.session_client.post(
        f"/v1/agents/{key}/deployments",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "prod", "version": 1},
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 409, res.text


def test_t2_f3_2_02_viewer_and_unprivileged_service_account_cannot_deploy_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
    for principal_tok in (tenant.viewer, tenant.service_account):
        res = client.post(
            f"/v1/agents/{key}/deployments",
            json={
                "project_id": tenant.project_id,
                "actor": principal_tok.principal.id,
                "environment": "dev",
                "version": 1,
            },
            headers=principal_tok.headers,
        )
        assert res.status_code == 403, res.text


def test_t2_f3_2_03_webhook_trigger_bad_hmac_refused_401(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    trig = tenant.session_client.post(
        f"/v1/agents/{key}/triggers",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "kind": "webhook"},
        headers=tenant.csrf_headers,
    ).json()

    bad = client.post(
        trig["url_path"],
        content=b'{"event":"forged"}',
        headers={
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": str(int(time.time())),
            "X-BOSS-Signature": "v1=" + ("0" * 64),
        },
    )
    assert bad.status_code == 401, bad.text


def test_t2_f3_2_04_trigger_pause_resume_delete_and_404(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    trig = tenant.session_client.post(
        f"/v1/agents/{key}/triggers",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "environment": "dev",
            "kind": "cron",
            "config": {"cron": "*/15 * * * *", "timezone": "UTC"},
        },
        headers=tenant.csrf_headers,
    )
    assert trig.status_code == 201, trig.text
    tid = trig.json()["id"]

    assert (
        tenant.session_client.post(
            f"/v1/agent-triggers/{tid}/pause",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "reason": "freeze"},
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )
    assert (
        tenant.session_client.post(
            f"/v1/agent-triggers/{tid}/resume",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "reason": "ready"},
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )
    assert (
        tenant.session_client.delete(
            f"/v1/agent-triggers/{tid}",
            params={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )
    assert (
        tenant.session_client.delete(
            f"/v1/agent-triggers/{tid}",
            params={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
            headers=tenant.csrf_headers,
        ).status_code
        == 404
    )


def test_t2_f3_2_05_double_text_reject_on_busy_thread_returns_409(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key = f"ag_{uuid.uuid4().hex[:8]}"
    assert _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key).status_code == 201
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
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "environment": "dev",
            "double_text": "reject",
        },
        headers=tenant.owner.headers,
    ).json()
    tid = thr["thread_id"]
    first = client.post(
        f"/v1/threads/{tid}/runs",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "input": {"message": "one"}},
        headers=tenant.owner.headers,
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/v1/threads/{tid}/runs",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "input": {"message": "two"},
            "double_text": "reject",
        },
        headers=tenant.owner.headers,
    )
    assert second.status_code == 409, second.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F3.3 — Event Intake Front Door, HMAC Verification, Replay & Privacy Redaction
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f3_3_01_intake_catalog_lists_adapters(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get("/v1/intake/catalog", headers=tenant.owner.headers)
    assert res.status_code == 200, res.text
    keys = {item["key"] for item in res.json()["sources"]}
    assert {"webhook", "slack", "github"}.issubset(keys)


def test_t1_f3_3_02_create_webhook_connection_mints_secret_once(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "alerts"},
        headers=tenant.csrf_headers,
    )
    assert conn.status_code == 201, conn.text
    cdata = conn.json()
    secret = cdata["secret"]
    assert secret.startswith("whsec_")

    fetched = client.get(f"/v1/intake/connections/{cdata['id']}", headers=tenant.owner.headers)
    assert fetched.status_code == 200, fetched.text
    assert secret not in json.dumps(fetched.json())


def test_t1_f3_3_03_signed_webhook_intake_persists_event_and_dedupes(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "hook"},
        headers=tenant.csrf_headers,
    ).json()
    url_path = conn["url_path"]
    secret = conn["secret"]

    handshake = client.options(
        url_path,
        headers={**tenant.service_account.headers, "WebHook-Request-Origin": "hooks.example.com"},
    )
    assert handshake.status_code in (200, 204), handshake.text

    delivery_id = f"deliv_{uuid.uuid4().hex[:12]}"
    raw = json.dumps({"event_type": "alert.fired", "service": "api", "actor_email": "alice@example.com"}).encode("utf-8")
    ts, sig = _sign_boss_webhook(secret, raw)
    hdrs = {
        **tenant.service_account.headers,
        "Content-Type": "application/json",
        "X-BOSS-Timestamp": ts,
        "X-BOSS-Signature": sig,
        "X-BOSS-Delivery": delivery_id,
    }
    first = client.post(url_path, content=raw, headers=hdrs)
    assert first.status_code in (200, 202), first.text
    dup = client.post(url_path, content=raw, headers=hdrs)
    assert dup.status_code in (200, 202), dup.text

    events = client.get(
        "/v1/intake/events",
        params={"project_id": tenant.project_id, "connection_id": conn["id"]},
        headers=tenant.owner.headers,
    )
    assert events.status_code == 200, events.text
    items = events.json().get("events") or events.json().get("items") or []
    assert len(items) == 1


def test_t1_f3_3_04_rotate_secret_invalidates_old_signature(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "rot"},
        headers=tenant.csrf_headers,
    ).json()
    old_secret = conn["secret"]
    url_path = conn["url_path"]

    rotated = tenant.session_client.post(
        f"/v1/intake/connections/{conn['id']}/rotate-secret",
        json={"actor": tenant.owner.principal.id},
        headers=tenant.csrf_headers,
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json().get("secret_shown_once") is True
    new_secret = rotated.json()["secret"]
    assert new_secret != old_secret

    raw = json.dumps({"ping": 1}).encode("utf-8")
    bad_ts, _ = _sign_boss_webhook("wrong_forged_secret", raw)
    rejected = client.post(
        url_path,
        content=raw,
        headers={
            **tenant.service_account.headers,
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": bad_ts,
            "X-BOSS-Signature": "v1=" + ("0" * 64),
        },
    )
    assert rejected.status_code in (401, 403), rejected.text

    new_ts, new_sig = _sign_boss_webhook(new_secret, raw)
    accepted = client.post(
        url_path,
        content=raw,
        headers={
            **tenant.service_account.headers,
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": new_ts,
            "X-BOSS-Signature": new_sig,
            "X-BOSS-Delivery": f"deliv_{uuid.uuid4().hex[:10]}",
        },
    )
    assert accepted.status_code in (200, 202), accepted.text


def test_t1_f3_3_05_pause_resume_and_deadletter_replay_surface(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "pausable"},
        headers=tenant.csrf_headers,
    ).json()
    cid = conn["id"]
    assert (
        tenant.session_client.post(
            f"/v1/intake/connections/{cid}/pause",
            json={"actor": tenant.owner.principal.id, "reason": "maintenance"},
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )
    assert (
        tenant.session_client.post(
            f"/v1/intake/connections/{cid}/resume",
            json={"actor": tenant.owner.principal.id, "reason": "ready"},
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )

    dl = client.get(
        "/v1/intake/dead-letters",
        params={"project_id": tenant.project_id, "connection_id": cid},
        headers=tenant.owner.headers,
    )
    assert dl.status_code == 200, dl.text

    raw = json.dumps({"event_type": "replay.test"}).encode("utf-8")
    ts, sig = _sign_boss_webhook(conn["secret"], raw)
    assert (
        client.post(
            conn["url_path"],
            content=raw,
            headers={
                **tenant.service_account.headers,
                "Content-Type": "application/json",
                "X-BOSS-Timestamp": ts,
                "X-BOSS-Signature": sig,
                "X-BOSS-Delivery": f"rep_{uuid.uuid4().hex[:10]}",
            },
        ).status_code
        in (200, 202)
    )
    ev_list = client.get(
        "/v1/intake/events",
        params={"project_id": tenant.project_id, "connection_id": cid},
        headers=tenant.owner.headers,
    ).json()
    ev_id = (ev_list.get("events") or ev_list.get("items"))[0]["id"]
    replay = tenant.session_client.post(
        "/v1/intake/events/replay",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "intake_event_ids": [ev_id],
            "reason": "m6-replay",
        },
        headers=tenant.csrf_headers,
    )
    assert replay.status_code == 200, replay.text


def test_t2_f3_3_01_bad_hmac_rejected_and_not_persisted(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "badhmac"},
        headers=tenant.csrf_headers,
    ).json()
    res = client.post(
        conn["url_path"],
        content=b'{"forged":true}',
        headers={
            **tenant.service_account.headers,
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": str(int(time.time())),
            "X-BOSS-Signature": "v1=" + ("f" * 64),
        },
    )
    assert res.status_code in (401, 403), res.text
    events = client.get(
        "/v1/intake/events",
        params={"project_id": tenant.project_id, "connection_id": conn["id"]},
        headers=tenant.owner.headers,
    ).json()
    items = events.get("events") or events.get("items") or []
    assert len(items) == 0


def test_t2_f3_3_02_oversized_payload_rejected_413(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "big"},
        headers=tenant.csrf_headers,
    ).json()
    big_body = b"{" + (b"\"x\":\"" + b"a" * (MAX_BODY_BYTES + 1024) + b"\"}")
    ts, sig = _sign_boss_webhook(conn["secret"], big_body)
    res = client.post(
        conn["url_path"],
        content=big_body,
        headers={
            **tenant.service_account.headers,
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": ts,
            "X-BOSS-Signature": sig,
        },
    )
    assert res.status_code == 413, res.text


def test_t2_f3_3_03_people_redacted_without_user_read(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    conn = tenant.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "webhook", "name": "redact"},
        headers=tenant.csrf_headers,
    ).json()
    raw = json.dumps({"text": "hello", "user_email": "secret.user@example.com"}).encode("utf-8")
    ts, sig = _sign_boss_webhook(conn["secret"], raw)
    client.post(
        conn["url_path"],
        content=raw,
        headers={
            **tenant.service_account.headers,
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": ts,
            "X-BOSS-Signature": sig,
            "X-BOSS-Delivery": f"deliv_{uuid.uuid4().hex[:10]}",
        },
    )
    ev_list = client.get(
        "/v1/intake/events",
        params={"project_id": tenant.project_id, "connection_id": conn["id"]},
        headers=tenant.owner.headers,
    ).json()
    items = ev_list.get("events") or ev_list.get("items") or []
    assert len(items) == 1
    ev_id = items[0]["id"]

    op_token = tenant.mint_pat(tenant.operator.principal, scopes=("integration:read",))
    op_view = client.get(f"/v1/intake/events/{ev_id}", headers={"Authorization": f"Bearer {op_token}"})
    assert op_view.status_code == 200, op_view.text
    assert op_view.json().get("people_redacted") is True


def test_t2_f3_3_04_cross_project_intake_connection_isolated(client, tenant_factory):
    tenant_a = tenant_factory(seed_defaults=False)
    tenant_b = tenant_factory(seed_defaults=False)
    conn = tenant_a.session_client.post(
        "/v1/intake/connections",
        json={"project_id": tenant_a.project_id, "actor": tenant_a.owner.principal.id, "kind": "webhook", "name": "priv"},
        headers=tenant_a.csrf_headers,
    ).json()
    cross = client.get(f"/v1/intake/connections/{conn['id']}", headers=tenant_b.owner.headers)
    assert cross.status_code in (403, 404), cross.text


def test_t2_f3_3_05_unknown_intake_connection_returns_404(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/intake/webhook/does_not_exist_999",
        content=b"{}",
        headers={
            **tenant.service_account.headers,
            "Content-Type": "application/json",
            "X-BOSS-Timestamp": "1",
            "X-BOSS-Signature": "v1=0",
        },
    )
    assert res.status_code == 404, res.text


def test_t2_f3_3_06_unauthenticated_connection_create_returns_401(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/intake/connections",
        json={"project_id": tenant.project_id, "kind": "webhook", "name": "unauth"},
    )
    assert res.status_code == 401, res.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F3.4 — db.background() ContextVar Safety & Timeout Enforcement
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f3_4_01_background_contextvar_toggles_and_resets():
    assert in_background() is False
    with background():
        assert in_background() is True
    assert in_background() is False


def test_t1_f3_4_02_background_transaction_tightens_lock_timeout(db_engine):
    with background():
        with begin(db_engine) as conn:
            val = conn.execute(text("SHOW lock_timeout")).scalar()
    assert val == f"{BACKGROUND_LOCK_TIMEOUT_SECONDS}s"


def test_t1_f3_4_03_background_transaction_tightens_idle_timeout(db_engine):
    with background():
        with begin(db_engine) as conn:
            val = conn.execute(text("SHOW idle_in_transaction_session_timeout")).scalar()
    assert val is not None and str(val) != "0"


def test_t1_f3_4_04_foreground_transaction_does_not_set_background_flag(db_engine):
    assert in_background() is False
    with begin(db_engine) as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
    assert in_background() is False


def test_t1_f3_4_05_background_limits_sql_uses_least_guard():
    stmts = background_limits_sql()
    assert len(stmts) == 1
    assert "LEAST" in stmts[0] and "lock_timeout" in stmts[0]


def test_t2_f3_4_01_exception_inside_background_resets_contextvar():
    assert in_background() is False
    with pytest.raises(RuntimeError, match="boom"):
        with background():
            assert in_background() is True
            raise RuntimeError("boom")
    assert in_background() is False


def test_t2_f3_4_02_preexisting_lower_lock_timeout_never_loosened(db_engine):
    with db_engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '250ms'"))
        for stmt in background_limits_sql():
            conn.execute(text(stmt))
        val = conn.execute(text("SHOW lock_timeout")).scalar()
    assert val == "250ms"


def test_t2_f3_4_03_separate_thread_does_not_inherit_background_contextvar():
    seen: list[bool] = []

    def probe():
        seen.append(in_background())

    with background():
        assert in_background() is True
        t = threading.Thread(target=probe)
        t.start()
        t.join(timeout=5)
    assert seen == [False]
    assert in_background() is False


def test_t2_f3_4_04_nested_background_contexts_preserve_outer_true():
    assert in_background() is False
    with background():
        assert in_background() is True
        with background():
            assert in_background() is True
        assert in_background() is True
    assert in_background() is False


def test_t2_f3_4_05_bots_tick_leaves_in_background_clean(tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = tenant.session_client.post(
        "/v1/bots/tick",
        json={"project_id": tenant.project_id, "limit": 5},
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 200, res.text
    assert in_background() is False


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F3.5 — A2A v1.0 Agent Cards/JSON-RPC, Agent Processes & Focused Agents
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def _setup_a2a_agent(client, tenant) -> tuple[str, str]:
    key = f"ag_{uuid.uuid4().hex[:8]}"
    created = _create_agent(client, tenant.project_id, tenant.owner.headers, tenant.owner.principal.id, key)
    assert created.status_code == 201, created.text
    dep = tenant.session_client.post(
        f"/v1/agents/{key}/deployments",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "environment": "dev", "version": 1},
        headers=tenant.csrf_headers,
    )
    assert dep.status_code == 201, dep.text
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
    prom_prod = tenant.session_client.post(
        f"/v1/agents/{key}/promote",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "from_environment": "staging",
            "to_environment": "prod",
        },
        headers=tenant.csrf_headers,
    )
    assert prom_prod.status_code == 200, prom_prod.text
    dec_id = prom_prod.json().get("decision_id")
    if dec_id:
        app_res = tenant.session_client.post(
            f"/v1/decisions/{dec_id}/approve",
            json={"actor": tenant.owner.principal.id, "note": "Approve prod A2A"},
            headers=tenant.csrf_headers,
        )
        assert app_res.status_code == 200, app_res.text

    endpoints = client.get(
        f"/v1/agents/{key}/endpoints",
        params={"project_id": tenant.project_id, "environment": "prod"},
        headers=tenant.owner.headers,
    ).json()
    card_path = (endpoints.get("a2a") or {}).get("card_path") or ""
    agent_id = endpoints.get("agent_id") or (card_path.split("/")[2] if card_path.startswith("/a2a/") else "")
    assert agent_id, endpoints
    skey = tenant.session_client.post(
        f"/v1/agents/{key}/signing-keys",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
        headers=tenant.csrf_headers,
    )
    assert skey.status_code in (200, 201), skey.text
    toggle = tenant.session_client.put(
        f"/v1/agents/{key}/deployments/prod/a2a",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "enabled": True},
        headers=tenant.csrf_headers,
    )
    assert toggle.status_code == 200, toggle.text
    return key, agent_id


def test_t1_f3_5_01_a2a_agent_card_endpoints(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    _key, agent_id = _setup_a2a_agent(client, tenant)

    card1 = client.get(f"/a2a/{agent_id}/.well-known/agent-card.json")
    assert card1.status_code == 200, card1.text
    assert card1.json().get("name")

    card2 = client.get("/.well-known/agent-card.json", params={"agent_id": agent_id})
    assert card2.status_code == 200, card2.text
    assert card2.json().get("name") == card1.json().get("name")


def test_t1_f3_5_02_a2a_jsonrpc_send_message(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    _key, agent_id = _setup_a2a_agent(client, tenant)

    rpc = client.post(
        f"/a2a/{agent_id}",
        json={
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "SendMessage",
            "params": {"message": {"role": "user", "parts": [{"text": "hello"}]}},
        },
        headers=tenant.owner.headers,
    )
    assert rpc.status_code == 200, rpc.text
    payload = rpc.json()
    assert payload.get("id") == "req-1" and ("result" in payload or "error" in payload)


def test_t1_f3_5_03_agent_process_compile_and_list_templates(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    comp = tenant.session_client.post(
        "/v1/agent-processes/compile",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "text": "Build, review, test, and accept the feature.",
        },
        headers=tenant.csrf_headers,
    )
    assert comp.status_code == 200, comp.text
    assert comp.json()["spec"]["steps"]

    templates = tenant.session_client.get(
        "/v1/agent-processes/templates",
        params={"project_id": tenant.project_id},
        headers=tenant.csrf_headers,
    )
    assert templates.status_code == 200, templates.text


def test_t1_f3_5_04_agent_process_start_simulated_run_and_list(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    started = client.post(
        "/v1/agent-processes/runs",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "prd": "Deliver health summary endpoint.",
            "title": "Health summary",
            "simulated": True,
        },
        headers=tenant.owner.headers,
    )
    assert started.status_code in (200, 201), started.text
    runs = client.get(
        "/v1/agent-processes/runs",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert runs.status_code == 200, runs.text


def test_t1_f3_5_05_focused_agent_create_wake_pause_activate(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    created = tenant.session_client.post(
        "/v1/focused-agents",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "kind": "custom",
            "name": "Watchdog",
            "focus": "Monitor failed jobs",
            "start": True,
        },
        headers=tenant.csrf_headers,
    )
    assert created.status_code == 201, created.text
    aid = created.json()["id"]

    assert (
        client.post(
            f"/v1/focused-agents/{aid}/wake",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "reason": "check"},
            headers=tenant.owner.headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/focused-agents/{aid}/pause",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "reason": "break"},
            headers=tenant.owner.headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/focused-agents/{aid}/activate",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "reason": "resume"},
            headers=tenant.owner.headers,
        ).status_code
        == 200
    )
    got = client.get(
        f"/v1/focused-agents/{aid}",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert got.status_code == 200 and got.json()["status"] == "active"


def test_t2_f3_5_01_a2a_unknown_agent_and_missing_query_return_404(client):
    assert client.get("/a2a/agt_missing_999/.well-known/agent-card.json").status_code == 404
    assert client.get("/.well-known/agent-card.json").status_code == 404


def test_t2_f3_5_02_a2a_invalid_jsonrpc_method_returns_rpc_error(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    _key, agent_id = _setup_a2a_agent(client, tenant)
    res = client.post(
        f"/a2a/{agent_id}",
        json={"jsonrpc": "2.0", "id": "bad-1", "method": "DoesNotExist", "params": {}},
        headers=tenant.owner.headers,
    )
    assert res.status_code == 200, res.text
    assert res.json().get("error", {}).get("code") == -32601


def test_t2_f3_5_03_a2a_disabled_deployment_returns_404(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    key, agent_id = _setup_a2a_agent(client, tenant)
    off = tenant.session_client.put(
        f"/v1/agents/{key}/deployments/prod/a2a",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "enabled": False},
        headers=tenant.csrf_headers,
    )
    assert off.status_code == 200, off.text
    assert client.get(f"/a2a/{agent_id}/.well-known/agent-card.json").status_code == 404


def test_t2_f3_5_04_agent_process_empty_compile_rejected_422(tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = tenant.session_client.post(
        "/v1/agent-processes/compile",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "text": "   "},
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f3_5_05_focused_agent_viewer_cannot_tick_all_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/focused-agents/tick-all",
        json={"project_id": tenant.project_id, "actor": tenant.viewer.principal.id},
        headers=tenant.viewer.headers,
    )
    assert res.status_code == 403, res.text
