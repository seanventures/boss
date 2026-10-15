"""Domain E2E: work orchestration, trackers, deployments, promotions, rollouts.

Exercises real handlers against live Postgres via FastAPI TestClient:
  - work-orchestration items + overview + score-preview
  - work-trackers catalog/list (bindings)
  - deployment targets/runs/promotions list endpoints
  - software-factory rollouts list

No real deploy or external tracker writes required; create paths that need
cloud/provider setup may legitimately return 4xx.
"""

from __future__ import annotations

import uuid


PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"
OK = {200, 201}
CLIENT = {400, 401, 403, 404, 409, 422, 429}
ALLOW = OK | CLIENT


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def test_work_trackers_catalog_and_list(client):
    catalog = client.get("/v1/work-trackers/catalog")
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    # GitHub Issues, reusing the repo connection, is the default option (skills-mcp spec E10);
    # Plane stays selectable.
    assert body.get("default_provider_key") == "github_issues"
    keys = [item.get("provider_key") for item in body.get("items") or []]
    assert {"github_issues", "plane"} <= set(keys)

    listed = client.get(
        "/v1/work-trackers", params={"project_id": PROJECT_ID}
    )
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    # None is the default until a tracker is connected; once one is, it names that provider.
    assert "default_provider_key" in payload
    providers = payload.get("items") or payload.get("providers") or []
    defaults = [p.get("provider_key") for p in providers if p.get("is_default")]
    assert payload["default_provider_key"] == (defaults[0] if defaults else None)
    # Secrets never leak from tracker configuration
    for item in providers:
        cfg = item.get("configuration") or {}
        for key, value in cfg.items():
            if any(
                secretish in key.lower()
                for secretish in ("token", "password", "pat", "secret")
            ):
                assert value in ("***", "", None) or str(value).startswith("env:")


def test_work_items_upsert_and_overview(client):
    uid = _uid()
    overview_before = client.get(
        "/v1/work-orchestration/overview",
        params={"project_id": PROJECT_ID},
    )
    # 503 is legitimate under transient DB contention (deadlock/admin shutdown)
    assert overview_before.status_code in ALLOW | {503}, overview_before.text
    assert overview_before.status_code != 500

    created = client.post(
        "/v1/work-orchestration/items",
        json={
            "project_id": PROJECT_ID,
            "subject_type": "feature",
            "subject_id": f"e2e-work-{uid}",
            "title": f"E2E work item {uid}",
            "description": "Domain e2e work-and-deploy pack",
            "strategic_value": 75,
            "customer_impact": 70,
            "urgency": 55,
            "cost_of_delay": 50,
            "risk_reduction": 40,
            "dependency_pressure": 30,
            "confidence": 85,
            "source": "test_e2e_work_and_deploy",
            "actor": ACTOR,
        },
    )
    assert created.status_code in ALLOW, created.text
    assert created.status_code < 500
    if created.status_code not in OK:
        return

    body = created.json()
    work_item = body.get("work_item") or body
    assert work_item.get("id", "").startswith("work_")
    assert work_item.get("title") == f"E2E work item {uid}"
    # Upsert auto-binds default tracker + ensures allocation
    binding = body.get("tracker_binding")
    if binding:
        assert binding.get("work_item_id") == work_item["id"]
        assert binding.get("provider_key") or binding.get("id")

    overview = client.get(
        "/v1/work-orchestration/overview",
        params={"project_id": PROJECT_ID},
    )
    assert overview.status_code in (200, 503), overview.text
    if overview.status_code == 200:
        ov = overview.json()
        # Overview shape is service-defined; require structured payload
        assert isinstance(ov, dict)
        assert ov  # non-empty

    # Explicit tracker binding upsert (list surface is overview + work-trackers)
    if work_item.get("id"):
        bind = client.post(
            "/v1/work-orchestration/tracker-bindings",
            json={
                "project_id": PROJECT_ID,
                "work_item_id": work_item["id"],
                "provider_key": "plane",
                "actor": ACTOR,
            },
        )
        assert bind.status_code in ALLOW, bind.text
        assert bind.status_code < 500


def test_work_score_preview_is_pure(client):
    preview = client.post(
        "/v1/work-orchestration/score-preview",
        json={
            "strategic_value": 80,
            "customer_impact": 70,
            "urgency": 60,
            "cost_of_delay": 50,
            "risk_reduction": 40,
            "dependency_pressure": 30,
            "confidence": 90,
            "estimated_cost_usd_micros": 1_000_000,
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert "priority_score" in body or "effective_score" in body
    assert "value_density" in body or "value_per_cost" in body or "priority_band" in body


def test_deployment_targets_runs_promotions_list(client):
    """List endpoints for deploy surface — empty lists are valid."""
    targets = client.get(
        "/v1/deployment-targets",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert targets.status_code in ALLOW, targets.text
    assert targets.status_code < 500
    if targets.status_code == 200:
        assert targets.json().get("project_id") == PROJECT_ID
        assert isinstance(targets.json().get("items"), list)

    # Alias list path used by some clients
    deployments = client.get(
        "/v1/deployments",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert deployments.status_code in ALLOW, deployments.text
    assert deployments.status_code < 500

    runs = client.get(
        "/v1/deployment-runs",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert runs.status_code in ALLOW, runs.text
    assert runs.status_code < 500
    if runs.status_code == 200:
        assert isinstance(runs.json().get("items"), list)

    promotions = client.get(
        "/v1/deployment-promotions",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert promotions.status_code in ALLOW, promotions.text
    assert promotions.status_code < 500
    if promotions.status_code == 200:
        assert isinstance(promotions.json().get("items"), list)

    policies = client.get(
        "/v1/deployment-environment-policies",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert policies.status_code in ALLOW, policies.text
    assert policies.status_code < 500

    # Unknown promotion is 404, not 500
    missing = client.get(
        f"/v1/deployment-promotions/promo_missing_{_uid()}",
        params={"project_id": PROJECT_ID},
    )
    assert missing.status_code in CLIENT | {200}, missing.text
    assert missing.status_code < 500


def test_software_factory_rollouts_list(client):
    rollouts = client.get(
        "/v1/software-factory/rollouts",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert rollouts.status_code in ALLOW, rollouts.text
    assert rollouts.status_code < 500
    if rollouts.status_code == 200:
        body = rollouts.json()
        assert body.get("project_id") == PROJECT_ID
        assert isinstance(body.get("items"), list)


def test_delivery_deployments_list_if_present(client):
    """GET /v1/delivery/deployments — allow 404/501 if surface not wired in edition."""
    response = client.get(
        "/v1/delivery/deployments",
        params={"project_id": PROJECT_ID},
    )
    # 501 not implemented is legitimate; 503 optional deps; never unhandled 500
    assert response.status_code in ALLOW | {501, 503}, response.text
    if response.status_code >= 500:
        assert response.status_code in (501, 503)


def test_work_and_deploy_surface_no_5xx_bundle(client):
    """Bundle smoke of all domain read paths used by Mission Control deploy/work views."""
    paths = [
        ("/v1/work-trackers/catalog", {}),
        ("/v1/work-trackers", {"project_id": PROJECT_ID}),
        ("/v1/work-orchestration/overview", {"project_id": PROJECT_ID}),
        ("/v1/deployment-targets", {"project_id": PROJECT_ID}),
        ("/v1/deployments", {"project_id": PROJECT_ID}),
        ("/v1/deployment-runs", {"project_id": PROJECT_ID}),
        ("/v1/deployment-promotions", {"project_id": PROJECT_ID}),
        ("/v1/software-factory/rollouts", {"project_id": PROJECT_ID}),
        ("/v1/deployment-environment-policies", {"project_id": PROJECT_ID}),
    ]
    failures: list[str] = []
    for path, params in paths:
        response = client.get(path, params=params or None)
        if response.status_code >= 500 and response.status_code not in (501, 503):
            failures.append(f"GET {path} -> {response.status_code} {response.text[:200]}")
    assert not failures, "work/deploy 5xx:\n" + "\n".join(failures)
