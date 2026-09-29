"""Agent Studio catalog seed is isolated, idempotent, no-spend, and fail-closed."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from apps.api.app.config import get_settings
from apps.api.app.db import begin
from apps.api.app.routers import agent_studio_seed as seed_mod
from apps.api.app.services.agent_studio import (
    AgentStudioSchemaMissingError,
    AgentStudioService,
)


PROJECT_ID = "boss-dogfood"


def _shipped_agent_keys() -> set[str]:
    return {
        path.stem
        for path in (get_settings().specpack_root / "agents").glob("*.yaml")
    }


class _UndefinedTable(Exception):
    """Stand-in for psycopg.errors.UndefinedTable (name is what we match)."""


_UndefinedTable.__name__ = "UndefinedTable"


def test_catalog_seed_is_idempotent_receipted_and_never_spends(client, engine):
    settings = get_settings()
    expected_agent_keys = _shipped_agent_keys()
    with begin(engine) as connection:
        jobs_before = connection.execute(text("SELECT count(*) FROM jobs")).scalar_one()
        reservations_before = connection.execute(
            text("SELECT count(*) FROM budget_reservations")
        ).scalar_one()

    first = client.post(
        "/v1/agent-studio/catalog/seed",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "seeded"
    assert body["agent_count"] == len(expected_agent_keys), body["skipped"]
    assert {item["agent_key"] for item in body["agents"]} == expected_agent_keys
    assert body["panel_count"] == 3
    assert body["workflow_count"] >= 1
    assert len(body["workflows"]) == body["workflow_count"]
    assert body["external_effects"] == 0
    assert body["model_calls"] == 0
    assert body["jobs_enqueued"] == 0
    assert body["budget_reserved"] is False
    assert body["launched_live_agents"] is False
    assert body["receipt_id"]
    assert body["dedupe_key"] == f"agent-studio:catalog-seed:{settings.default_project_id}"

    alias = client.post(
        "/v1/agent-studio/seed",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert alias.status_code == 200, alias.text
    assert alias.json()["receipt_id"] == body["receipt_id"]
    assert all(item["duplicate"] for item in alias.json()["agents"])
    assert all(item["duplicate"] for item in alias.json()["panels"])
    assert alias.json()["workflow_count"] == body["workflow_count"]
    assert all(item["duplicate"] for item in alias.json()["workflows"])

    second = client.post(
        "/v1/agent-studio/catalog/seed",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["receipt_id"] == body["receipt_id"]
    assert all(item["duplicate"] for item in second.json()["agents"])

    with begin(engine) as connection:
        jobs_after = connection.execute(text("SELECT count(*) FROM jobs")).scalar_one()
        reservations_after = connection.execute(
            text("SELECT count(*) FROM budget_reservations")
        ).scalar_one()
        studio_jobs = connection.execute(
            text("SELECT count(*) FROM jobs WHERE kind = 'agent.studio.execute'")
        ).scalar_one()
        receipt = connection.execute(
            text("SELECT status, effect_kind FROM receipts WHERE id = :id"),
            {"id": body["receipt_id"]},
        ).mappings().one()
    assert jobs_after == jobs_before
    assert reservations_after == reservations_before
    assert studio_jobs == 0
    assert receipt["status"] == "succeeded"
    assert receipt["effect_kind"] == "agent_studio.catalog_seed"


def test_mobile_template_is_selectable_but_does_not_claim_device_execution(client, engine):
    settings = get_settings()
    response = client.get(
        "/v1/agent-studio/templates",
        params={"project_id": settings.default_project_id},
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {item["id"] for item in items} == _shipped_agent_keys()
    assert sum(item["id"] == "mobile_app_operator" for item in items) == 1
    assert sum(
        item["id"] != "mobile_app_operator"
        and item.get("category") != "general"
        for item in items
    ) == 20
    mobile = next(item for item in items if item["id"] == "mobile_app_operator")
    assert mobile["readiness"] == "definition_only"
    assert mobile["mobile"]["execution_status"] == "adapter_required"
    assert mobile["mobile"]["capabilities"] == [
        "build", "test", "launch", "interact", "screenshot", "logs"
    ]
    assert set(mobile["mobile"]["device_targets"]) == {
        "android_emulator", "android_physical", "ios_simulator", "ios_physical"
    }
    assert "macos" in mobile["mobile"]["host_requirements"]["ios_simulator"]

    with begin(engine) as connection:
        jobs_before = connection.execute(text("SELECT count(*) FROM jobs")).scalar_one()
    installed = client.post(
        "/v1/agent-studio/templates/mobile_app_operator/install",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert installed.status_code == 200, installed.text
    body = installed.json()
    assert body["agent_key"] == "mobile_app_operator"
    assert body["status"] == "installed"
    assert body["receipt_id"]
    assert body["model_calls"] == body["jobs_enqueued"] == body["external_effects"] == 0
    assert body["launched_live_agent"] is False
    with begin(engine) as connection:
        jobs_after = connection.execute(text("SELECT count(*) FROM jobs")).scalar_one()
        receipt = connection.execute(
            text("SELECT status FROM receipts WHERE id=:id"), {"id": body["receipt_id"]}
        ).scalar_one()
    assert jobs_after == jobs_before
    assert receipt == "succeeded"

    missing = client.post(
        "/v1/agent-studio/templates/not_a_template/install",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert missing.status_code == 404


def test_template_install_restores_missing_builtin_role_without_replacing_existing_roles(
    client, engine
):
    settings = get_settings()
    with begin(engine) as connection:
        pm_before = connection.execute(
            text("SELECT spec FROM roles WHERE project_id=:p AND role_key='pm'"),
            {"p": settings.default_project_id},
        ).scalar_one()
        deleted = connection.execute(
            text("DELETE FROM roles WHERE project_id=:p AND role_key='docs_writer'"),
            {"p": settings.default_project_id},
        ).rowcount
    assert deleted == 1
    response = client.post(
        "/v1/agent-studio/templates/technical_writer/install",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert response.status_code == 200, response.text
    with begin(engine) as connection:
        restored = connection.execute(
            text("SELECT spec FROM roles WHERE project_id=:p AND role_key='docs_writer'"),
            {"p": settings.default_project_id},
        ).scalar_one()
        pm_after = connection.execute(
            text("SELECT spec FROM roles WHERE project_id=:p AND role_key='pm'"),
            {"p": settings.default_project_id},
        ).scalar_one()
    assert restored["role_id"] == "docs_writer"
    assert pm_after == pm_before


def test_catalog_tables_ready_fails_closed_when_missing():
    settings = get_settings()
    service = AgentStudioService(settings)

    class _Missing:
        def execute(self, *_a, **_k):
            class _R:
                def scalar(self):
                    return None

            return _R()

    ready, missing = service._catalog_tables_ready(_Missing())
    assert ready is False
    assert "agent_definitions" in missing
    assert "agent_versions" in missing
    assert "panel_definitions" in missing


def test_seed_catalog_raises_schema_missing_before_writes():
    settings = get_settings()
    service = AgentStudioService(settings)

    class _Missing:
        def execute(self, *_a, **_k):
            class _R:
                def scalar(self):
                    return None

            return _R()

    try:
        service.seed_catalog(_Missing(), PROJECT_ID, actor="ceo")
    except AgentStudioSchemaMissingError as error:
        assert "agent_definitions" in str(error)
        assert error.missing
    else:
        raise AssertionError("expected AgentStudioSchemaMissingError")


def test_catalog_seed_http_fails_closed_when_tables_missing(client, monkeypatch):
    monkeypatch.setattr(
        AgentStudioService,
        "_catalog_tables_ready",
        lambda *_a, **_k: (False, ["agent_definitions"]),
    )
    response = client.post(
        "/v1/agent-studio/catalog/seed",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert response.status_code == 503, response.text
    assert response.status_code != 500
    assert "agent_definitions" in response.json()["detail"]

    alias = client.post(
        "/v1/agent-studio/seed",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert alias.status_code == 503, alias.text
    assert "agent_definitions" in alias.json()["detail"]


def test_schema_error_mapping_is_503_never_500():
    mapped = seed_mod._schema_http_error(
        AgentStudioSchemaMissingError(["agent_definitions"])
    )
    assert mapped.status_code == 503
    assert "agent_definitions" in str(mapped.detail)

    programming = ProgrammingError(
        "SELECT 1",
        {},
        _UndefinedTable('relation "agent_definitions" does not exist'),
    )
    mapped_sql = seed_mod._schema_http_error(programming)
    assert mapped_sql.status_code == 503
    assert "not applied" in str(mapped_sql.detail)


def test_isolated_seed_router_is_mounted_on_app(client):
    missing = client.post("/v1/agent-studio/seed", json={})
    assert missing.status_code == 422
    assert client.get("/v1/agent-studio/catalog/seed").status_code == 405
    assert client.get("/v1/agent-studio/seed").status_code == 405


def test_catalog_get_fails_closed_503_when_tables_missing(client, monkeypatch):
    monkeypatch.setattr(
        AgentStudioService,
        "_catalog_tables_ready",
        lambda *_a, **_k: (False, ["agent_definitions"]),
    )
    response = client.get(
        "/v1/agent-studio/catalog",
        params={"project_id": PROJECT_ID},
    )
    assert response.status_code == 503, response.text
    assert response.status_code != 500
    assert "agent_definitions" in response.json()["detail"]
