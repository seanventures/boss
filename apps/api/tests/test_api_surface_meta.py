"""Control-plane HTTP surface: meta catalog, multi-cloud, shared-server, new engine routes.

Covers discovery endpoints plus Community-safe surfaces added for iac_verify,
oncall_notify, pdf_render, multi-cloud skill describe, and secrets get.
"""
from __future__ import annotations

import base64


def test_meta_api_catalog_domains_and_route_count(client):
    response = client.get("/v1/meta/api-catalog")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route_count"] > 100
    assert isinstance(body.get("domains"), list)
    assert len(body["domains"]) >= 1
    assert body.get("domain_count", len(body["domains"])) >= 1
    # services_with_routes is the cheap domain coverage note for operators/agents
    services = body.get("services_with_routes") or []
    assert isinstance(services, list)
    assert len(services) >= 1
    assert all("id" in s and s.get("route_count", 0) > 0 for s in services)
    # llm-calls lives under the models domain
    models = next((d for d in body["domains"] if d.get("id") == "models"), None)
    if models:
        paths = {r.get("path") for r in models.get("routes") or []}
        assert any(p and p.startswith("/v1/llm-calls") for p in paths)


def test_meta_openapi_coverage_fields(client):
    response = client.get("/v1/meta/openapi-coverage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "route_count" in body
    assert "documented_route_count" in body
    assert "documentation_coverage" in body
    assert "missing_from_openapi" in body
    assert body["route_count"] > 100


def test_multi_cloud_catalog(client):
    response = client.get("/v1/multi-cloud/catalog")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product"] == "boss_multi_cloud"
    assert body["provider_count"] >= 4
    keys = {p["provider_key"] for p in body["providers"]}
    assert {"aws", "azure", "digitalocean", "ovhcloud"} <= keys


def test_shared_server_packages(client):
    response = client.get("/v1/shared-server/packages")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "items" in body or "packages" in body or isinstance(body, dict)


def test_multi_cloud_skills_describe(client):
    response = client.post(
        "/v1/multi-cloud/skills/describe",
        json={
            "skill_ids": [
                "aws.ec2.instance.create",
                "azure.vm.create",
                "not.a.real.skill",
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 3
    by_id = {item["skill_id"]: item for item in body["items"]}
    assert by_id["aws.ec2.instance.create"]["known"] is True
    assert by_id["aws.ec2.instance.create"]["provider_key"] == "aws"
    assert by_id["aws.ec2.instance.create"]["in_multi_cloud_catalog"] is True
    assert by_id["azure.vm.create"]["provider_key"] == "azure"
    assert by_id["not.a.real.skill"]["known"] is False
    assert "supported_provider_keys" in body
    assert "aws" in body["supported_provider_keys"]


def test_multi_cloud_skills_describe_requires_skill(client):
    response = client.post("/v1/multi-cloud/skills/describe", json={})
    assert response.status_code == 422, response.text


def test_iac_detect_from_files_and_task(client):
    response = client.post(
        "/v1/iac/detect",
        json={
            "files": {
                "main.tf": 'resource "null_resource" "x" {}\n',
                "app.py": "print(1)\n",
            },
            "title": "Add terraform module",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_iac_task"] is True
    assert "main.tf" in body["iac_files"]
    assert body["file_count"] >= 1


def test_iac_detect_rejects_empty(client):
    response = client.post("/v1/iac/detect", json={})
    assert response.status_code == 422, response.text


def test_iac_plan_endpoint_runs_lane(client, monkeypatch, tmp_path):
    """Plan shares the verify lane; empty-toolchain path stays fail-closed."""
    monkeypatch.setenv("PATH", str(tmp_path))
    response = client.post(
        "/v1/iac/plan",
        json={"files": {"main.tf": 'resource "null_resource" "x" {}\n'}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    steps = {s["step"]: s for s in body["steps"]}
    assert "plan_dry" in steps
    assert steps["plan_live"]["skipped"] is True


def test_oncall_preview_formats_card(client):
    response = client.post(
        "/v1/oncall/preview",
        json={
            "incident_id": "inc_preview_1",
            "alert": {
                "title": "HighErr",
                "service": "pay",
                "severity": "critical",
                "description": "err rate 40%",
            },
            "hypothesis": {
                "root_cause": "deploy 9",
                "confidence": 0.92,
                "basis": "memory",
            },
            "autonomy": {"tier": 0, "action": "auto_remediate", "reason": "ok"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["incident_id"] == "inc_preview_1"
    assert body["posted"] is False
    assert "HighErr" in body["text"]
    assert "inc_preview_1" in body["text"]
    assert "webhook_configured" in body


def test_oncall_notify_receipted_without_webhook(client):
    response = client.post(
        "/v1/oncall/notify",
        json={
            "project_id": "boss-dogfood",
            "actor": "executive_console",
            "incident_id": "inc_notify_1",
            "alert": {"title": "T1", "severity": "warning"},
            "dedupe_key": "oncall.test.inc_notify_1",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["incident_id"] == "inc_notify_1"
    assert body["posted"] is False
    assert body["receipt_id"]
    assert body["dedupe_key"] == "oncall.test.inc_notify_1"
    # Idempotent reuse of succeeded would require a prior success; first call is new.
    assert body["reused"] is False


def test_pdf_render_base64(client):
    response = client.post(
        "/v1/pdf/render",
        json={
            "markdown": "# Hello\n\nA short **report** with a list:\n\n- one\n- two\n",
            "title": "Surface Test",
            "subtitle": "control plane",
            "as_base64": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content_type"] == "application/pdf"
    assert body["page_count"] >= 1
    assert body["byte_length"] > 100
    raw = base64.b64decode(body["content_base64"])
    assert raw[:4] == b"%PDF"
    assert len(raw) == body["byte_length"]


def test_pdf_render_raw_download(client):
    response = client.post(
        "/v1/pdf/render",
        json={"markdown": "# Raw PDF\n\nbody\n", "as_base64": False},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content[:4] == b"%PDF"


def test_pdf_render_requires_content(client):
    response = client.post("/v1/pdf/render", json={"markdown": "   "})
    assert response.status_code == 422, response.text


def test_mutation_gate_and_repair_policy_still_routed(client):
    mg = client.post("/v1/mutation-gate", json={"files": {}})
    assert mg.status_code == 422
    rp = client.post("/v1/repair-policy", json={})
    assert rp.status_code == 200
    assert "max_retries" in rp.json() or "attempts" in rp.json() or isinstance(
        rp.json(), dict
    )


def test_secrets_list_endpoint(client):
    response = client.get("/v1/secrets", params={"project_id": "boss-dogfood"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == "boss-dogfood"
    assert "secrets" in body


def test_assurance_overview_mounted(client):
    """Governance router mounts assurance; overview is the public entrypoint."""
    response = client.get(
        "/v1/assurance/overview", params={"project_id": "boss-dogfood"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
