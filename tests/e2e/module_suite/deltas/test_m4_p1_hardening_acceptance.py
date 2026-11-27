"""M4 P1 Hardening Acceptance Delta Suite (Armed for M4 merge & --profile m7).

Merged M4 slices (4B-S1, 4B-S2, 4D-1) run actively on `sv/main`; unmerged M4 slices
(`ai/codex/m4-4c-s1`, `ai/codex/m4-4c-s2`, `ai/codex/m4-4c-s3`, `ai/codex/m4-4c-s4`,
`ai/codex/m4-4d-2`) are explicitly skipped naming their pending branch.
Covers:
- BOSSAPP-1306: Concurrent budget reservations racing against daily/project cap never overshoot (4C-2)
- BOSSAPP-1397: Local VLLM_BASE_URL setup & keyless model binding (4B-S1 — active)
- BOSSAPP-1400: App preview directory `0700` permissions and raw body cap (4B-S2 — active)
- BOSSAPP-1407: `http_probe` permitted in chat/agent_step/mcp with observability:read (4C-1)
- BOSSAPP-1413: Workflow publish validation rejecting `config_deploy` missing `source_paths` (4C-1)
- BOSSAPP-1415 & 1444: Storage incident org confinement & multi-org tenant boundary (4C-2)
- BOSSAPP-1437..1440: Local `'this machine'` deployment target & host hardening (4C-3)
- BOSSAPP-1460 & 1461: Community gates removed (4C-1) & BOSS-side autonomy gate hardening (4C-2)
- BOSSAPP-1235 & 1239: PRD revise works cleanly & local merge rollback (4D-1)
- BOSSAPP-1029: Gate integrity fails closed when repo's own tests fail (active)
- E2E-T3-09: Pairwise concurrent budget + autonomy + PRD revise (4C-2)
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import build_failure, model_failures, preview_service
from boss_engine.services.code_agent import _bind_lane_credentials
from boss_engine.services.governed_code_task_service import _guest_engine_fields
from boss_engine.templates import gate as template_gate
from boss_engine.tools.builtin import build_default_registry

_PENDING_4C_1 = pytest.mark.skip(
    reason="Pending unmerged branch: ai/codex/m4-4c-s1 (M4 4C-1 BOSSAPP-1407/1413/1460)"
)
_PENDING_4C_2 = pytest.mark.skip(
    reason="Pending unmerged branch: ai/codex/m4-4c-s2 (M4 4C-2 BOSSAPP-1306/1415/1461)"
)
_PENDING_4C_1_AND_4C_3 = pytest.mark.skip(
    reason="Pending unmerged branches: ai/codex/m4-4c-s1 (BOSSAPP-1460) and ai/codex/m4-4c-s3 (BOSSAPP-1437/1440)"
)


@_PENDING_4C_2
def test_m4_1306_concurrent_budget_reservations_never_overshoot_cap(live_api_url, client, tenant_factory, worker_headers):
    import httpx

    tenant = tenant_factory(seed_defaults=True)

    # Daily cap is 50_000_000 micros; race 6 concurrent reservations of 15_000_000 each (90M > 50M)
    def reserve_chunk(idx: int) -> int:
        r = httpx.post(
            f"{live_api_url}/v1/budget/reserve",
            json={
                "project_id": tenant.project_id,
                "category": "model",
                "amount_usd_micros": 15_000_000,
                "reason": f"race-{idx}",
            },
            headers=worker_headers,
            timeout=15.0,
        )
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(reserve_chunk, range(6)))

    assert any(c == 201 for c in codes), f"Expected at least one 201, got {codes}"
    assert any(c != 201 for c in codes), f"Expected cap refusal, got {codes}"
    status = client.get("/v1/budget/status", params={"project_id": tenant.project_id}, headers=tenant.owner.headers).json()
    assert status["reserved_usd_micros"] <= 50_000_000


def test_m4_1397_local_vllm_base_url_configuration_supported(db_engine, tenant_factory, http_fake_factory, monkeypatch):
    """BOSSAPP-1397 (merged 4B-S1): connected keyless local vllm binds its base_url/model for host
    and guest builds, probes cleanly against a local OpenAI-compatible server, and explains missing
    VLLM_BASE_URL plainly without leaking the raw env var name."""
    import httpx

    tenant = tenant_factory(seed_defaults=True)
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)

    with http_fake_factory(
        lambda _m, _p, _h, _b: (200, {"Content-Type": "application/json"}, b'{"data":[{"id":"qwen2.5-coder"}]}')
    ) as (fake_url, recorded):
        vllm_base = f"{fake_url}/v1"
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO entitlement_sources ("
                    "  id, project_id, source_id, provider, source_kind, entitlement_kind, owner_type, owner_id,"
                    "  connection_status, credential_secret_ref_id, supported_models, connection_metadata, updated_at"
                    ") VALUES ("
                    "  :id, :p, :sid, 'vllm', 'local', 'local', 'project', :p,"
                    "  'connected', NULL, CAST(:models AS jsonb), CAST(:meta AS jsonb), :at"
                    ") ON CONFLICT (project_id, source_id) DO UPDATE SET "
                    "  connection_status = 'connected', supported_models = EXCLUDED.supported_models, "
                    "  connection_metadata = EXCLUDED.connection_metadata, updated_at = EXCLUDED.updated_at"
                ),
                {
                    "id": f"esrc_{uuid.uuid4().hex[:10]}",
                    "p": tenant.project_id,
                    "sid": "setup-vllm",
                    "models": json.dumps(["qwen2.5-coder"]),
                    "meta": json.dumps({"base_url": vllm_base, "model": "qwen2.5-coder"}),
                    "at": datetime.now(timezone.utc),
                },
            )
            settings = Settings(_env_file=None)
            bound = _bind_lane_credentials(settings, "vllm", connection=conn, project_id=tenant.project_id)
            guest = _guest_engine_fields(settings, "vllm", None, connection=conn, project_id=tenant.project_id)

        assert bound.vllm_base_url == vllm_base
        assert bound.openai_model == "qwen2.5-coder"
        assert guest["openai_base_url"] == vllm_base
        assert guest["model"] == "qwen2.5-coder"

        probe = httpx.get(f"{bound.vllm_base_url}/models", timeout=5.0)
        assert probe.status_code == 200
        assert probe.json()["data"][0]["id"] == "qwen2.5-coder"
        assert len(recorded) == 1

    explained = model_failures.explain("VLLM_BASE_URL is not configured.")
    assert explained["kind"] == "no_model"
    assert explained["retry"] is False
    assert "VLLM_BASE_URL" not in explained["headline"]
    action = build_failure.action_for(
        tenant.project_id,
        source="code_task",
        run_id="crun_1397",
        failure_class="needs_human",
        reason_code="model_not_configured",
        error="VLLM_BASE_URL is not configured.",
    )
    assert action["retry"] is None
    assert "VLLM_BASE_URL" not in action["fix"]["detail"]


def test_m4_1400_preview_body_cap_and_permissions(client, tenant_factory):
    """BOSSAPP-1400 (merged 4B-S2): preview root is created with 0700 permissions and oversized
    preview bodies fail closed with PreviewBodyTooLarge / 4xx refusal."""
    preview_service.PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    mode = preview_service.PREVIEW_ROOT.stat().st_mode & 0o777
    assert mode == 0o700, f"expected PREVIEW_ROOT mode 0700, got {oct(mode)}"

    limit = 64
    assert preview_service._read_bounded(io.BytesIO(b"a" * limit), limit) == b"a" * limit
    with pytest.raises(preview_service.PreviewBodyTooLarge):
        preview_service._read_bounded(io.BytesIO(b"a" * (limit + 1)), limit)

    tenant = tenant_factory(seed_defaults=False)
    run_id = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Preview test",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()["run_id"]
    big = client.post(
        f"/v1/code-tasks/{run_id}/preview",
        content=b"{" + b'"x":"' + b"a" * 2_000_000 + b'"}',
        headers={**tenant.owner.headers, "Content-Type": "application/json"},
    )
    assert big.status_code in (400, 413, 422), big.text


@_PENDING_4C_1
def test_m4_1407_http_probe_exposed_in_chat_tool_registry():
    reg = build_default_registry()
    spec = reg.get("http_probe")
    assert spec is not None
    assert spec.permission == "observability:read"
    assert {"chat", "agent_step", "mcp"}.issubset(spec.exposure.surfaces)


@_PENDING_4C_1
def test_m4_1413_publish_workflow_config_deploy_without_source_paths_rejected(tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = tenant.session_client.post(
        "/v1/delivery-workflows/publish",
        json={
            "project_id": tenant.project_id,
            "name": f"Missing Source Paths {uuid.uuid4().hex[:6]}",
            "actor": tenant.owner.principal.id,
            "spec": {
                "entry_step": "deploy_cfg",
                "steps": [
                    {
                        "id": "deploy_cfg",
                        "kind": "config_deploy",
                        "next": [],
                    }
                ],
            },
        },
        headers=tenant.csrf_headers,
    )
    assert res.status_code in (400, 422), res.text


@_PENDING_4C_2
def test_m4_1415_1444_storage_incidents_confined_to_caller_org(client, tenant_factory):
    tenant_a = tenant_factory(seed_defaults=False)
    tenant_b = tenant_factory(seed_defaults=False)
    res = client.get("/v1/infra/storage-incidents", params={"project_id": tenant_b.project_id}, headers=tenant_a.owner.headers)
    assert res.status_code in (403, 404), res.text


@_PENDING_4C_1_AND_4C_3
def test_m4_1437_1440_local_this_machine_deployment_target_and_1460_community_gates(client, tenant_factory):
    """BOSSAPP-1440 (4C-3): built-in 'this machine' deploy target exists; BOSSAPP-1460 (4C-1):
    community edition no longer drops edition-gated routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers import mount_edition_routers

    probe_app = FastAPI()
    probe_app.get("/v1/account-connect/link")(lambda: {"surface": "open"})
    mount_edition_routers(probe_app, "community")
    probe_client = TestClient(probe_app)
    assert probe_client.get("/v1/account-connect/link").status_code == 200

    tenant = tenant_factory(seed_defaults=True)
    targets = client.get(
        "/v1/deployment-targets",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert targets.status_code == 200, targets.text
    items = targets.json().get("items", targets.json())
    assert any(
        "this machine" in str(t.get("name", "")).lower() or t.get("connector_type") == "local_this_machine"
        for t in items
    ), items


@_PENDING_4C_2
def test_m4_1235_1239_1461_prd_revise_autonomy_and_e2e_t3_09(db_engine, live_api_url, client, tenant_factory, worker_headers):
    """Covers E2E-T3-09: valid decide rule persistence + autonomy gate hardening (BOSSAPP-1461)
    + concurrent budget cap (BOSSAPP-1306) + PRD revise endpoint presence (BOSSAPP-1235)."""
    from boss_engine.services import autonomy_gate

    tenant = tenant_factory(seed_defaults=True)
    window = {"from_day": "sat", "from_time": "00:00", "to_day": "sun", "to_time": "23:59", "tz": "UTC"}
    rule = tenant.session_client.put(
        "/v1/decide/rules/prod_deploy",
        json={
            "project_id": tenant.project_id,
            "scope": tenant.project_id,
            "rule": {"quiet": [window]},
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.csrf_headers,
    )
    assert rule.status_code == 200, rule.text
    saved = rule.json()["saved"]
    assert saved["kind"] == "prod_deploy" and window in saved["rule"]["quiet"]

    # BOSSAPP-1461: malformed risk tier / non-finite budget override must ask, never crash or allow.
    with db_engine.begin() as conn:
        verdict = autonomy_gate.evaluate_action(
            conn,
            project_id=tenant.project_id,
            action="deploy",
            risk_tier="not-an-int",  # type: ignore[arg-type]
            tools="malformed-tools",  # type: ignore[arg-type]
        )
    assert verdict.get("decision") == "ask" or verdict.get("allow") is False, verdict

    test_m4_1306_concurrent_budget_reservations_never_overshoot_cap(live_api_url, client, tenant_factory, worker_headers)


def test_m4_1029_gate_integrity_fails_when_repo_tests_fail():
    """BOSSAPP-1029: Gate integrity fails closed when the target repository's own tests fail —
    connector smoke and template tests both refuse to pass and hold_outcome names the failing checks."""
    manifest = {
        "schema": "boss.template/v1",
        "id": "demo-connector",
        "name": "Demo Connector",
        "version": "1.0.0",
        "summary": "Connector for fail-closed gate check",
        "adds": ["routes"],
        "stacks": {"node": {"frameworks": ["express"], "min_toolchain": "node20"}},
        "secrets": [],
        "egress": [],
        "routes": ["GET /health"],
        "tests": {"node": {"command": "npm test", "report": "junit", "min_tests": 1}},
        "license": "Apache-2.0",
        "min_boss": "1.0.0",
    }
    res = template_gate.evaluate(
        template={"id": "demo-connector", "version": "1.0.0", "digest": "sha256:test", "stack": "node", "manifest": manifest},
        files={"stacks/node/tests/smoke.test.js": b"test('ok', () => {})"},
        changed={"src/app.js": "export const ok = true;\n"},
        cwd=None,
        run_command=None,
        tests_passed=False,
        serve={"proven": True, "route_probes": {"/health": 200}},
        as_connector=True,
    )
    assert res.gates["template_tests"] is False, res
    assert res.evidence["template_tests"]["because"] == "tests", res.evidence
    held = template_gate.hold_outcome(
        ["template_tests"],
        template_tests=res.evidence["template_tests"],
        tests={"failed_check": {"command": "npm test", "first_error": "FAIL src/app.test.ts"}},
    )
    assert held is not None
    assert held["error"].startswith("Blocked:"), held
    assert "npm test" in held["reason"] or "checks failed" in held["reason"].lower(), held
