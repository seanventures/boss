"""Goldeneye bare-metal PaaS control plane tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api.app import main as api_main
from boss_engine.services.goldeneye_paas import GoldeneyePaasService

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


def test_goldeneye_catalog_bootstrap(client):
    response = client.get("/v1/goldeneye/catalog/bootstrap")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "default_bootstrap" in body
    order = body["default_bootstrap"]
    assert isinstance(order, list)
    assert order[0] == "layer0-host"
    assert "layer1-runtime" in order
    assert body["count"] == len(order)
    assert body["count"] >= 1


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


def test_goldeneye_real_install_calls_ssh_runner(client, monkeypatch):
    """When BOSS_SSH_HOST_DEPLOY_ALLOWED and node.ssh_host set, actuate via runner."""
    calls: list[dict] = []

    def fake_runner(*, host, user, port, command, timeout=600):
        calls.append(
            {
                "host": host,
                "user": user,
                "port": port,
                "command": command,
                "timeout": timeout,
            }
        )
        return {
            "returncode": 0,
            "stdout": "module layer0-host installed\n",
            "stderr": "",
        }

    # Gate on + injected runner on the live API service instance.
    monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", True)
    api_main.goldeneye_paas.ssh_runner = fake_runner
    try:
        reg = client.post(
            "/v1/goldeneye/nodes",
            json={
                "project_id": PROJECT_ID,
                "actor": "ceo",
                "name": "node-real",
                "roles": ["control"],
                "ssh_host": "node-real.tailnet.example",
                "ssh_user": "ubuntu",
                "ssh_port": 2222,
            },
        )
        assert reg.status_code == 201, reg.text
        node_id = reg.json()["id"]

        inst = client.post(
            "/v1/goldeneye/packages/install",
            json={
                "project_id": PROJECT_ID,
                "actor": "ceo",
                "package_id": "layer0-host",
                "node_id": node_id,
            },
        )
        assert inst.status_code == 201, inst.text
        body = inst.json()
        assert body["mode"] == "real"
        assert body["status"] == "installed"
        assert body["receipt_id"]
        assert calls, "expected SSH runner to be invoked"
        assert calls[0]["host"] == "node-real.tailnet.example"
        assert calls[0]["user"] == "ubuntu"
        assert calls[0]["port"] == 2222
        assert "ge module install" in calls[0]["command"]
        assert "layer0-host" in calls[0]["command"]
        assert (body.get("metadata") or {}).get("actuation", {}).get("host") == (
            "node-real.tailnet.example"
        )
    finally:
        api_main.goldeneye_paas.ssh_runner = None
        monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", False)


def test_goldeneye_real_install_failure_marks_error(client, monkeypatch):
    """Remote non-zero exit → status failed + error jsonb on the install row."""

    def fail_runner(*, host, user, port, command, timeout=600):
        return {
            "returncode": 17,
            "stdout": "",
            "stderr": "ge: module layer0-host install failed: not root\n",
        }

    monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", True)
    api_main.goldeneye_paas.ssh_runner = fail_runner
    try:
        reg = client.post(
            "/v1/goldeneye/nodes",
            json={
                "project_id": PROJECT_ID,
                "actor": "ceo",
                "name": "node-fail",
                "ssh_host": "node-fail.tailnet.example",
            },
        )
        assert reg.status_code == 201, reg.text
        node_id = reg.json()["id"]

        inst = client.post(
            "/v1/goldeneye/packages/install",
            json={
                "project_id": PROJECT_ID,
                "actor": "ceo",
                "package_id": "layer0-host",
                "node_id": node_id,
            },
        )
        assert inst.status_code == 201, inst.text
        body = inst.json()
        assert body["mode"] == "real"
        assert body["status"] == "failed"
        err = body.get("error") or {}
        assert err.get("returncode") == 17
        assert "not root" in (err.get("stderr") or "")
        assert err.get("host") == "node-fail.tailnet.example"
    finally:
        api_main.goldeneye_paas.ssh_runner = None
        monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", False)


def test_actuate_remote_module_install_uses_adapter_when_no_runner(monkeypatch):
    """Unit: without injected runner, production path calls SSHHostAdapter._run_remote."""
    settings = SimpleNamespace(
        ssh_host_deploy_allowed=True,
        ssh_host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n",
        ssh_host_private_key_file=None,
    )
    svc = GoldeneyePaasService(settings)

    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run_remote(self, *, host, user, port, command, timeout=120):
        captured["host"] = host
        captured["user"] = user
        captured["port"] = port
        captured["command"] = command
        captured["timeout"] = timeout
        return FakeProc()

    monkeypatch.setattr(
        "boss_engine.services.ssh_host_adapter.SSHHostAdapter._run_remote",
        fake_run_remote,
    )

    result = svc._actuate_remote_module_install(
        node={
            "ssh_host": "edge.example",
            "ssh_user": "root",
            "ssh_port": 22,
        },
        package_id="layer1-runtime",
    )
    assert result["ok"] is True
    assert result["via"] == "ssh_host_adapter"
    assert captured["host"] == "edge.example"
    assert "ge module install" in captured["command"]
    assert "layer1-runtime" in captured["command"]


def test_actuate_requires_ssh_host():
    settings = SimpleNamespace(ssh_host_deploy_allowed=True)
    svc = GoldeneyePaasService(settings)
    with pytest.raises(ValueError, match="ssh_host"):
        svc._actuate_remote_module_install(
            node={"ssh_host": "", "ssh_user": "root", "ssh_port": 22},
            package_id="layer0-host",
        )


def _assert_honest_empty_fleet(body: dict) -> None:
    """Empty / unconfigured Goldeneye must never invent host.connected or metrics."""
    assert body["product"] == "goldeneye"
    assert body["status"] == "not_configured"
    assert body["configured"] is False
    assert body["node_count"] == 0
    assert body["app_count"] == 0
    assert body["nodes"] == []
    assert body["apps"] == []
    assert body["host"]["connected"] is False
    assert body["metrics"] is None
    edition = body["edition"]
    assert edition["community"]["kit"] is True
    assert edition["community"]["control_plane"] is True
    assert edition["community"]["self_hosted_metal"] is True
    assert edition["platform_only"]
    assert any("multi-tenant" in item.lower() or "hosted" in item.lower()
               for item in edition["platform_only"])


def test_goldeneye_status_empty_is_honest_not_configured(client):
    """No registered nodes → 200 not_configured, never a fake connected host."""
    status = client.get("/v1/goldeneye/status", params={"project_id": PROJECT_ID})
    assert status.status_code == 200, status.text
    _assert_honest_empty_fleet(status.json())


def test_goldeneye_status_ensure_tables_after_drop(client, engine):
    """Missing goldeneye_nodes must recreate via ensure_tables, not 500."""
    from sqlalchemy import text

    api_main.goldeneye_paas._tables_ready = False
    with engine.begin() as connection:
        for tbl in (
            "goldeneye_deploys",
            "goldeneye_apps",
            "goldeneye_package_installs",
            "goldeneye_nodes",
        ):
            connection.execute(text(f"DROP TABLE IF EXISTS {tbl} CASCADE"))

    status = client.get("/v1/goldeneye/status", params={"project_id": PROJECT_ID})
    assert status.status_code == 200, status.text
    _assert_honest_empty_fleet(status.json())

    # Tables exist again — register must work after the drop.
    api_main.goldeneye_paas._tables_ready = False
    reg = client.post(
        "/v1/goldeneye/nodes",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "name": "after-drop",
            "roles": ["apps"],
        },
    )
    assert reg.status_code == 201, reg.text
    again = client.get("/v1/goldeneye/status", params={"project_id": PROJECT_ID})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["node_count"] >= 1
    assert body["status"] != "not_configured"
    # Still no invented metrics; connected stays false without a heartbeat.
    assert body["metrics"] is None
    assert body["host"]["connected"] is False
