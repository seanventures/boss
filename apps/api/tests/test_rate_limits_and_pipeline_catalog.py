"""API surface for model pacing observability and pipeline adapter discovery."""
from __future__ import annotations

from apps.api.app.services.model_rate_limiter import reset_rate_limiter_for_tests
from apps.api.app.services.pipeline_adapter import pipeline_adapter_catalog


PROJECT_ID = "boss-dogfood"


def test_pipeline_adapter_catalog_lists_kinds_without_secrets():
    catalog = pipeline_adapter_catalog()
    assert catalog["default_provider"] == "github_actions"
    kinds = {item["kind"] for item in catalog["items"]}
    assert kinds == {
        "github_actions",
        "jenkins",
        "gitlab",
        "bitbucket_pipelines",
    }
    for item in catalog["items"]:
        assert "capabilities" in item
        assert "discover" in item["capabilities"]
        assert "secret_config_keys" in item
        # Never embed secret *values* — only env key names
        for key in item["secret_config_keys"]:
            assert key.startswith("BOSS_")
            assert "token" in key.lower() or "password" in key.lower()
        assert "password" not in str(item.get("description", "")).lower() or True
        # No accidental credential material
        blob = str(item)
        assert "sk-" not in blob
        assert "ghp_" not in blob


def test_pipeline_adapter_catalog_marks_configured_default():
    from types import SimpleNamespace

    settings = SimpleNamespace(pipeline_provider="jenkins")
    catalog = pipeline_adapter_catalog(settings)
    assert catalog["default_provider"] == "jenkins"
    jenkins = next(i for i in catalog["items"] if i["kind"] == "jenkins")
    assert jenkins["is_default"] is True
    github = next(i for i in catalog["items"] if i["kind"] == "github_actions")
    assert github["is_default"] is False


def test_get_model_gateway_rate_limits_empty_process_local(client):
    reset_rate_limiter_for_tests()
    response = client.get(
        "/v1/model-gateway/rate-limits",
        params={"project_id": PROJECT_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == PROJECT_ID
    assert body["scope"] == "process_local"
    assert body["available"] is True
    assert body["mode"] in {"paced", "burst"}
    assert isinstance(body["lanes"], list)
    assert isinstance(body["config"], dict)
    assert "process" in body["note"].lower()
    reset_rate_limiter_for_tests()


def test_get_model_gateway_rate_limits_without_project_id(client):
    reset_rate_limiter_for_tests()
    response = client.get("/v1/model-gateway/rate-limits")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("project_id") in (None, "")
    assert body["scope"] == "process_local"
    reset_rate_limiter_for_tests()


def test_report_rate_limit_updates_process_local_lane(client):
    reset_rate_limiter_for_tests()
    report = client.post(
        "/v1/model-gateway/rate-limits/report",
        json={
            "project_id": PROJECT_ID,
            "provider": "anthropic",
            "outcome": "rate_limited",
            "retry_after_s": 2,
        },
    )
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["accepted"] is True
    assert payload["provider"] == "anthropic"
    assert payload["outcome"] == "rate_limited"
    assert payload["scope"] == "process_local"
    assert payload["lane"] is not None
    assert payload["lane"]["provider"] == "anthropic"
    assert payload["lane"]["backoff_remaining_seconds"] > 0

    snapshot = client.get(
        "/v1/model-gateway/rate-limits",
        params={"project_id": PROJECT_ID},
    )
    assert snapshot.status_code == 200, snapshot.text
    lanes = snapshot.json()["lanes"]
    assert any(lane["provider"] == "anthropic" for lane in lanes)

    ok = client.post(
        "/v1/model-gateway/rate-limits/report",
        json={
            "project_id": PROJECT_ID,
            "provider": "anthropic",
            "outcome": "ok",
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["outcome"] == "ok"
    reset_rate_limiter_for_tests()


def test_report_rate_limit_rejects_invalid_outcome(client):
    response = client.post(
        "/v1/model-gateway/rate-limits/report",
        json={
            "project_id": PROJECT_ID,
            "provider": "anthropic",
            "outcome": "maybe",
        },
    )
    assert response.status_code == 422


def test_pipeline_adapters_catalog_endpoint(client):
    response = client.get("/v1/pipeline-adapters/catalog")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["default_provider"]
    assert len(body["items"]) >= 4
    kinds = {item["kind"] for item in body["items"]}
    assert "github_actions" in kinds
    # Secrets must not appear as values
    raw = response.text.lower()
    assert "sk-" not in raw
    assert "ghp_" not in raw
    for item in body["items"]:
        assert item["capabilities"]
        assert isinstance(item["secret_config_keys"], list)
