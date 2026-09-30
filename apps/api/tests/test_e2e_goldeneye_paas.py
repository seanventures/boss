"""Domain E2E: Shared-server bare-metal PaaS + related cloud/elastic ops.

List/status endpoints must not 500. Actuation without credentials/flags
fails closed (4xx), never unhandled 5xx.
"""

from __future__ import annotations

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"

ALLOW_CLIENT = {400, 401, 403, 404, 409, 422, 503}


def _assert_not_5xx(response, label: str) -> None:
    code = response.status_code
    assert code < 500 or code == 503, f"{label} -> {code} {response.text[:300]}"


def test_shared_server_list_and_status_gets_do_not_500(client):
    """All Shared-server read surfaces return 2xx (or controlled 4xx), never 5xx."""
    catalog = client.get("/v1/shared-server/packages")
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    assert body.get("items")
    ids = {item["id"] for item in body["items"]}
    assert "layer0-host" in ids

    bootstrap = client.get("/v1/shared-server/catalog/bootstrap")
    assert bootstrap.status_code == 200, bootstrap.text
    assert bootstrap.json()["count"] >= 1
    assert bootstrap.json()["default_bootstrap"][0] == "layer0-host"

    for path in (
        "/v1/shared-server/status",
        "/v1/shared-server/nodes",
        "/v1/shared-server/apps",
        "/v1/shared-server/package-installs",
    ):
        response = client.get(path, params={"project_id": PROJECT_ID})
        assert response.status_code == 200, f"{path}: {response.text}"
        payload = response.json()
        assert payload.get("project_id") == PROJECT_ID
        if path == "/v1/shared-server/status":
            assert payload["status"] == "not_configured"
            assert payload["metrics"] is None
            assert payload["host"]["connected"] is False
        else:
            assert "items" in payload
            assert payload.get("count") == len(payload["items"])


def test_shared_server_register_install_deploy_simulated_e2e(client):
    """Node register → package install → app deploy (simulated, no SSH)."""
    reg = client.post(
        "/v1/shared-server/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "name": "e2e-ge-paas-node",
            "roles": ["apps", "edge"],
            "public_ip": "203.0.113.77",
            "ssh_host": "node-e2e.tailnet.example",
            "kit_version": "0.2.0",
        },
    )
    assert reg.status_code == 201, reg.text
    node = reg.json()
    assert node["status"] == "registered"
    node_id = node["id"]

    hb = client.post(
        f"/v1/shared-server/nodes/{node_id}/heartbeat",
        json={"project_id": PROJECT_ID, "status": "ready", "kit_version": "0.2.0"},
    )
    assert hb.status_code == 200, hb.text

    install = client.post(
        "/v1/shared-server/packages/install",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "package_id": "layer0-host",
            "node_id": node_id,
        },
    )
    assert install.status_code == 201, install.text
    assert install.json()["status"] in ("simulated", "planned", "installed")

    deploy = client.post(
        "/v1/shared-server/apps/deploy",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "name": "e2e-demo",
            "node_id": node_id,
            "image_ref": "ghcr.io/example/e2e-demo:1",
        },
    )
    assert deploy.status_code == 201, deploy.text
    assert deploy.json()["status"] in ("simulated", "deployed", "planned")

    apps = client.get("/v1/shared-server/apps", params={"project_id": PROJECT_ID})
    assert apps.status_code == 200
    names = {a["name"] for a in apps.json()["items"]}
    assert "e2e-demo" in names

    status = client.get("/v1/shared-server/status", params={"project_id": PROJECT_ID})
    assert status.status_code == 200
    assert status.json()["product"] == "shared-server"


def test_shared_server_actuation_fails_closed_without_inputs(client):
    """Missing project/package/image/app → 4xx, never 500."""
    cases = [
        ("post", "/v1/shared-server/nodes", {"name": "orphan"}),
        (
            "post",
            "/v1/shared-server/packages/install",
            {"project_id": PROJECT_ID, "actor": ACTOR},
        ),
        (
            "post",
            "/v1/shared-server/packages/install",
            {
                "project_id": PROJECT_ID,
                "actor": ACTOR,
                "package_id": "does-not-exist-pkg",
            },
        ),
        (
            "post",
            "/v1/shared-server/apps/deploy",
            {"project_id": PROJECT_ID, "actor": ACTOR, "name": "no-image"},
        ),
        (
            "post",
            "/v1/shared-server/apps/missing-app/rollback",
            {"project_id": PROJECT_ID, "actor": ACTOR},
        ),
        (
            "post",
            "/v1/shared-server/nodes/missing-node/heartbeat",
            {"project_id": PROJECT_ID},
        ),
    ]
    for method, path, body in cases:
        response = getattr(client, method)(path, json=body)
        assert response.status_code in ALLOW_CLIENT, (
            f"{method.upper()} {path} -> {response.status_code} {response.text[:200]}"
        )


def test_ssh_host_deploy_fails_closed_without_run_or_gate(client):
    """Governed SSH deploy never 500s when run missing or actuation blocked."""
    # Unknown run
    missing = client.post(
        "/v1/code-tasks/does-not-exist-run/deploy",
        json={"actor": ACTOR, "mode": "ssh", "host": "203.0.113.10"},
    )
    assert missing.status_code in (404, 422), missing.text

    # Missing actor
    no_actor = client.post(
        "/v1/code-tasks/does-not-exist-run/deploy",
        json={"mode": "dry_run"},
    )
    assert no_actor.status_code in ALLOW_CLIENT, no_actor.text


def test_cloud_ops_list_and_fail_closed_actuation(client):
    """Cloud catalog/status + elastic fabric reads; enroll/capacity fail closed."""
    for path in (
        "/v1/cloud/catalog",
        "/v1/cloud/status",
        "/v1/cloud/comparison",
        "/v1/cloud/capabilities",
        "/v1/cloud/workers",
        "/v1/elastic/status",
        "/v1/elastic/instances",
    ):
        response = client.get(path, params={"project_id": PROJECT_ID})
        _assert_not_5xx(response, f"GET {path}")
        assert response.status_code in (200, *ALLOW_CLIENT), response.text

    multi = client.get("/v1/multi-cloud/catalog")
    assert multi.status_code == 200, multi.text
    assert multi.json().get("provider_count", multi.json().get("version")) is not None

    # Unlinked community: worker enroll fail-closed (404/409), not 500
    enroll = client.post(
        "/v1/cloud/workers/enroll",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "worker_node_id": "e2e-worker-1",
        },
    )
    assert enroll.status_code in ALLOW_CLIENT, enroll.text

    # Capacity without desired_workers
    cap = client.post(
        "/v1/elastic/capacity",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert cap.status_code in ALLOW_CLIENT, cap.text

    # Capacity without project
    cap2 = client.post(
        "/v1/elastic/capacity",
        json={"actor": ACTOR, "desired_workers": 1},
    )
    assert cap2.status_code in ALLOW_CLIENT, cap2.text


def test_meta_catalog_includes_shared_server_and_cloud_domains(client):
    """Meta catalog surfaces shared-server + cloud domains with live routes."""
    catalog = client.get("/v1/meta/api-catalog")
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    domain_ids = {d["id"] for d in body["domains"]}
    assert "shared-server" in domain_ids
    assert "cloud" in domain_ids or "providers" in domain_ids
    assert body["route_count"] >= 200

    ge = client.get("/v1/meta/api-catalog/shared-server")
    assert ge.status_code == 200, ge.text
    ge_body = ge.json()
    assert ge_body["id"] == "shared-server"
    paths = {r["path"] for r in ge_body.get("routes") or []}
    assert any(p.startswith("/v1/shared-server/") for p in paths)

    unknown = client.get("/v1/meta/api-catalog/not-a-domain-xyz")
    assert unknown.status_code == 404
