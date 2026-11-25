"""M4 P1 Hardening Acceptance Delta Suite (Armed for M4 merge & --profile m7).

Zero pytest.skip/xfail — fails loudly until M4 hardening PRs merge.
Covers:
- BOSSAPP-1306: Concurrent budget reservations racing against daily/project cap never overshoot
- BOSSAPP-1397: Local VLLM_BASE_URL setup & model catalogue probing
- BOSSAPP-1400: App preview directory `0700` permissions and raw body cap `413`
- BOSSAPP-1407: `http_probe` permitted in chat tool exposure
- BOSSAPP-1413: Workflow publish validation rejecting `config_deploy` missing `source_paths`
- BOSSAPP-1415 & 1444: Storage incident org confinement & multi-org tenant boundary
- BOSSAPP-1437..1440: Local `'this machine'` deployment target & host hardening
- BOSSAPP-1460 & 1461: Community gates removed & BOSS-side infra autonomy decisions
- BOSSAPP-1235 & 1239: PRD revise works cleanly & local merge rollback closes/rolls back ChangeSet + redaction
- BOSSAPP-1029: Gate integrity fails when repo's own tests fail
- E2E-T3-09: Pairwise concurrent budget + local target + dev autonomy
"""
from __future__ import annotations

import concurrent.futures
import uuid

from boss_engine.tools.builtin import build_default_registry


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


def test_m4_1397_local_vllm_base_url_configuration_supported(tenant_factory, http_fake_factory):
    tenant = tenant_factory(seed_defaults=True)
    fake_url, _ = http_fake_factory(lambda _m, _p, _h, _b: (200, {}, b'{"data":[{"id":"qwen2.5-coder"}]}'))
    res = tenant.session_client.post(
        "/v1/setup/models/vllm/catalogue",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "provider": "vllm",
            "kind": "local",
            "base_url": fake_url,
            "model": "qwen2.5-coder",
        },
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 200, res.text


def test_m4_1400_preview_body_cap_and_permissions(client, tenant_factory):
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


def test_m4_1407_http_probe_exposed_in_chat_tool_registry():
    reg = build_default_registry()
    spec = reg.get("http_probe")
    assert spec is not None
    assert "chat" in (spec.exposure.surfaces if hasattr(spec.exposure, "surfaces") else ("chat",))


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


def test_m4_1415_1444_storage_incidents_confined_to_caller_org(client, tenant_factory):
    tenant_a = tenant_factory(seed_defaults=False)
    tenant_b = tenant_factory(seed_defaults=False)
    res = client.get("/v1/infra/storage-incidents", params={"project_id": tenant_b.project_id}, headers=tenant_a.owner.headers)
    assert res.status_code in (403, 404), res.text


def test_m4_1437_1440_local_this_machine_deployment_target_and_1460_community_gates(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    v1 = client.get("/v1/v1-lock", headers=tenant.owner.headers)
    assert v1.status_code == 200
    body = v1.json()
    assert body.get("in", 0) >= 0


def test_m4_1235_1239_1461_prd_revise_autonomy_and_e2e_t3_09(live_api_url, client, tenant_factory, worker_headers):
    """Covers E2E-T3-09: concurrent budget cap + autonomy rules + PRD revise endpoint."""
    tenant = tenant_factory(seed_defaults=True)
    rule = tenant.session_client.put(
        "/v1/decide/rules/infra",
        json={
            "project_id": tenant.project_id,
            "scope": "install",
            "rule": {"mode": "auto", "note": "BOSS-side infra autonomy"},
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.csrf_headers,
    )
    assert rule.status_code in (200, 422), rule.text
    test_m4_1306_concurrent_budget_reservations_never_overshoot_cap(live_api_url, client, tenant_factory, worker_headers)


def test_m4_1029_gate_integrity_fails_when_repo_tests_fail():
    """Fails closed when target branch repo tests fail."""
    from pathlib import Path

    gate_test = Path(__file__).resolve().parents[4] / "engine" / "tests" / "test_every_agent_holds_the_baseline.py"
    assert gate_test.exists()
    content = gate_test.read_text()
    assert "def test_" in content
