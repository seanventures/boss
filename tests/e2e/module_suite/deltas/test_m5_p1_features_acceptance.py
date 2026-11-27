"""M5 P1 Features Acceptance Delta Suite (Armed for M5 merge & --profile m7).

Unmerged M5 feature slices are explicitly skipped with the pending slice name so tests
on `sv/main` pass honestly until M5 merges.
Covers:
- BOSSAPP-1222..1229: Acceptance workflow step, report v2, findings ledger & App Map evidence
- BOSSAPP-555..557: App Map scanner, confirmation API & storage
- BOSSAPP-558: Plain-git core repository onboarding without GitHub dependency
- BOSSAPP-627 & 768: Phase 4 Knowledge Graph learning loop (attribution, decay & promotion queue)
- BOSSAPP-779..801: Smart model routing & Quick Lane execution entrypoints
"""
from __future__ import annotations

import subprocess
import uuid

import pytest

pytestmark = pytest.mark.skip(
    reason="Pending unmerged slice: M5 P1 features (BOSSAPP-555..558, 627, 768, 779..801, 1222..1229)"
)


def test_m5_1222_1229_acceptance_report_v2_and_findings_ledger(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    rep = client.get("/v1/acceptance/reports", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert rep.status_code == 200, rep.text
    findings = client.get("/v1/acceptance/findings", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert findings.status_code == 200, findings.text


def test_m5_555_557_app_map_scan_and_confirm(client, tenant_factory, tmp_path):
    tenant = tenant_factory(seed_defaults=True)
    repo = tmp_path / "sample_app"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"sample","scripts":{"test":"echo ok"}}')
    scanned = client.post(
        "/v1/app-map/scan",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "path": str(repo)},
        headers=tenant.owner.headers,
    )
    assert scanned.status_code in (200, 201), scanned.text
    confirmed = client.post(
        "/v1/app-map/confirm",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "app_map": scanned.json()},
        headers=tenant.owner.headers,
    )
    assert confirmed.status_code == 200, confirmed.text


def test_m5_558_plain_git_core_repository_support(tenant_factory, tmp_path):
    tenant = tenant_factory(seed_defaults=False)
    bare = tmp_path / "plain_repo.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    res = tenant.session_client.post(
        "/v1/setup/repository/git",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "git_url": f"file://{bare}"},
        headers=tenant.csrf_headers,
    )
    assert res.status_code in (200, 201), res.text


def test_m5_627_768_knowledge_learning_attribution_and_promotion_queue(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    q = client.get(
        "/v1/knowledge/promotions",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert q.status_code == 200, q.text


def test_m5_779_801_smart_model_routing_and_quick_lane(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    route = client.post(
        "/v1/routing/recommend",
        json={"project_id": tenant.project_id, "task": "Rename a local helper function", "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert route.status_code == 200, route.text
    ql = client.post(
        "/v1/quick-lane/runs",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "prompt": f"Quick check {uuid.uuid4().hex[:6]}",
            "simulated": True,
        },
        headers=tenant.owner.headers,
    )
    assert ql.status_code in (200, 201), ql.text
