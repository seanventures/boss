"""Goldeneye bare-metal PaaS control plane tests."""

from __future__ import annotations


PROJECT_ID = "boss-dogfood"


def test_goldeneye_package_catalog(client):
    response = client.get("/v1/goldeneye/packages")
    assert response.status_code == 200, response.text
    payload = response.json()
    # catalog document version 2 when assembled from modules; 1 if embedded
    assert payload["version"] in (1, 2)
    ids = [item["id"] for item in payload["items"]]
    assert "layer0-host" in ids
    assert "layer1-runtime" in ids
    assert "layer2-edge" in ids
    assert "deploy-cli" in ids
    assert "minio" in ids
    assert "plane" in ids
    assert "layer0-host" in payload["default_bootstrap"]
    # modular kit exposes path/recipe under modules/
    layer0 = next(i for i in payload["items"] if i["id"] == "layer0-host")
    recipe = layer0.get("recipe") or layer0.get("path") or ""
    if recipe:
        assert "layer0-host" in recipe


def test_goldeneye_register_node_install_and_deploy(client):
    # Register node
    reg = client.post(
        "/v1/goldeneye/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "name": "node-1",
            "roles": ["control", "edge", "apps"],
            "public_ip": "203.0.113.10",
            "tailnet_ip": "100.64.0.10",
            "ssh_host": "node-1.tailnet.example",
            "kit_version": "0.1.0",
        },
    )
    assert reg.status_code == 201, reg.text
    node = reg.json()
    assert node["name"] == "node-1"
    assert node["status"] == "registered"
    assert "control" in node["roles"]
    node_id = node["id"]

    # Idempotent update
    reg2 = client.post(
        "/v1/goldeneye/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "name": "node-1",
            "public_ip": "203.0.113.11",
            "roles": ["apps"],
        },
    )
    assert reg2.status_code == 201, reg2.text
    assert reg2.json()["id"] == node_id
    assert reg2.json()["public_ip"] == "203.0.113.11"

    listed = client.get("/v1/goldeneye/nodes", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1

    # Package install without dependency fails
    bad = client.post(
        "/v1/goldeneye/packages/install",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "package_id": "layer1-runtime",
            "node_id": node_id,
        },
    )
    assert bad.status_code == 422
    assert "dependency" in bad.json()["detail"].lower() or "Missing" in bad.json()["detail"]

    # Bootstrap package (simulated — default gate off)
    inst0 = client.post(
        "/v1/goldeneye/packages/install",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "package_id": "layer0-host",
            "node_id": node_id,
        },
    )
    assert inst0.status_code == 201, inst0.text
    body0 = inst0.json()
    assert body0["package_id"] == "layer0-host"
    assert body0["status"] == "simulated"
    assert body0["mode"] == "simulated"
    assert body0["receipt_id"]

    inst1 = client.post(
        "/v1/goldeneye/packages/install",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "package_id": "layer1-runtime",
            "node_name": "node-1",
        },
    )
    assert inst1.status_code == 201, inst1.text
    assert inst1.json()["status"] == "simulated"

    # Deploy app (simulated)
    dep = client.post(
        "/v1/goldeneye/apps/deploy",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "name": "hello",
            "node_id": node_id,
            "image_ref": "registry.example/hello:v1",
            "domain": "hello.apps.example.com",
        },
    )
    assert dep.status_code == 201, dep.text
    app = dep.json()
    assert app["name"] == "hello"
    assert app["status"] == "simulated"
    assert app["image_ref"] == "registry.example/hello:v1"
    assert app["public_url"] == "https://hello.apps.example.com"
    assert app.get("latest_deploy_id")

    # Redeploy records previous image for rollback
    dep2 = client.post(
        "/v1/goldeneye/apps/deploy",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "name": "hello",
            "node_id": node_id,
            "image_ref": "registry.example/hello:v2",
        },
    )
    assert dep2.status_code == 201, dep2.text
    assert dep2.json()["image_ref"] == "registry.example/hello:v2"
    assert dep2.json()["previous_image_ref"] == "registry.example/hello:v1"

    rb = client.post(
        "/v1/goldeneye/apps/hello/rollback",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert rb.status_code == 200, rb.text
    assert rb.json()["image_ref"] == "registry.example/hello:v1"

    apps = client.get("/v1/goldeneye/apps", params={"project_id": PROJECT_ID})
    assert apps.status_code == 200
    assert any(a["name"] == "hello" for a in apps.json()["items"])

    # Heartbeat
    hb = client.post(
        f"/v1/goldeneye/nodes/{node_id}/heartbeat",
        json={"project_id": PROJECT_ID, "kit_version": "0.1.1"},
    )
    assert hb.status_code == 200, hb.text
    assert hb.json()["status"] == "ready"
    assert hb.json()["kit_version"] == "0.1.1"

    status = client.get("/v1/goldeneye/status", params={"project_id": PROJECT_ID})
    assert status.status_code == 200, status.text
    overview = status.json()
    assert overview["product"] == "goldeneye"
    assert overview["node_count"] >= 1
    assert overview["app_count"] >= 1
    assert overview["real_host_effects"] is False
    assert overview["kit"]["package_count"] >= 8


def test_goldeneye_unknown_package(client):
    reg = client.post(
        "/v1/goldeneye/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "name": "node-x",
        },
    )
    assert reg.status_code == 201
    bad = client.post(
        "/v1/goldeneye/packages/install",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "package_id": "not-a-real-package",
            "node_name": "node-x",
        },
    )
    assert bad.status_code == 422
