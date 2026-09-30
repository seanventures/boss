"""Assurance demo seed is honest, fail-closed, and idempotent."""
from __future__ import annotations

from apps.api.app.db import begin
from apps.api.app.services.assurance import AssuranceService
from apps.api.app.config import get_settings


PROJECT_ID = "boss-dogfood"


def test_seed_demo_creates_one_metric_and_case(client):
    first = client.post(
        "/v1/assurance/seed-demo",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["seeded"] is True
    assert body["production_health_claimed"] is False
    assert body["metric"]["metric_key"] == "community.demo.proof_gate"
    assert body["metric"]["seeded"] is True
    assert body["metric"]["metadata"]["seeded"] is True
    assert body["metric"]["metadata"]["not_production_health"] is True
    assert body["case"]["title"].startswith("Demo proof gate")
    assert body["case"]["seeded"] is True
    assert body["case"]["subject_ref"] == "demo:community-assurance-seed"

    overview = client.get("/v1/assurance/overview", params={"project_id": PROJECT_ID})
    assert overview.status_code == 200
    assert any(item["id"] == body["case"]["id"] for item in overview.json()["cases"])
    assert any(
        item["metric_key"] == "community.demo.proof_gate"
        for item in overview.json()["metrics"]
    )


def test_seed_demo_is_idempotent(client):
    first = client.post(
        "/v1/assurance/seed-demo",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    second = client.post(
        "/v1/assurance/seed-demo",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True
    assert second.json()["metric"]["id"] == first.json()["metric"]["id"]
    assert second.json()["case"]["id"] == first.json()["case"]["id"]


def test_seed_demo_fails_closed_when_tables_missing(engine):
    settings = get_settings()
    service = AssuranceService(settings)
    class _Missing:
        def execute(self, *_a, **_k):
            class _R:
                def scalar(self):
                    return None
            return _R()

    ready, missing = service._assurance_tables_ready(_Missing())
    assert ready is False
    assert "ground_truth_metric_definitions" in missing
    assert "assurance_cases" in missing

    with begin(engine) as connection:
        # Real DB has tables — seed must not invent a healthy production claim.
        result = service.seed_demo(connection, project_id=PROJECT_ID, actor="ceo")
    assert result["production_health_claimed"] is False
    assert result["seeded"] is True
