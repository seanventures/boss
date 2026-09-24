"""Private edition skill packs and optional Cloud readiness retain their behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.commercial import CommercialService
from boss_engine.services.entitlements import EntitlementBrokerService


@pytest.mark.parametrize(
    "edition",
    ["community", "cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted", "unknown"],
)
def test_edition_skill_pack_matches_private_edition_status(edition):
    settings = get_settings().model_copy(update={"boss_edition": edition})
    broker = EntitlementBrokerService(settings)
    commercial = CommercialService(settings)
    expected_pack = Path(commercial.EDITION_PACKS[commercial.normalize_edition()]).stem
    assert broker._edition_skill_bundles(None, "project") == broker._pack_skill_bundles(expected_pack)


def test_optional_managed_gateway_readiness_keeps_link_credentials():
    settings = get_settings().model_copy(
        update={
            "boss_edition": "community",
            "managed_cloud_model_gateway_enabled": True,
            "account_connect_url": "https://cloud.example.test",
            "account_connect_token": "local-test-token",
        }
    )
    broker = EntitlementBrokerService(settings)
    assert broker._runtime_flags("managed_cloud_model_gateway") == (True, True)

    missing_token = settings.model_copy(update={"account_connect_token": None})
    assert EntitlementBrokerService(missing_token)._runtime_flags(
        "managed_cloud_model_gateway"
    ) == (True, False)


def test_paid_gateway_policy_restores_private_registry_and_readiness():
    settings = get_settings().model_copy(
        update={
            "boss_edition": "enterprise_self_hosted",
            "enterprise_model_gateway_enabled": True,
            "enterprise_model_gateway_url": "https://gateway.example.test",
            "enterprise_model_gateway_token": "test-token",
            "enterprise_model_gateway_models": "enterprise-gateway-auto",
        }
    )
    broker = EntitlementBrokerService(settings)
    registry = broker._provider_registry()
    provider = next(item for item in registry["providers"] if item["id"] == "boss")
    assert provider["source_records"][0]["source_id"] == "enterprise_model_gateway"
    assert any(item["id"] == "boss:enterprise-gateway-auto" for item in registry["models"])
    assert registry["routing_policy"]["workload_class_defaults"]["multi_tenant_customer"]["preferred_source_kinds"][0] == "enterprise_model_gateway"
    assert broker._runtime_flags("enterprise_model_gateway") == (True, True)
    assert broker._credential_ref("enterprise_model_gateway") == "BOSS_ENTERPRISE_MODEL_GATEWAY_TOKEN"
    assert broker._routing_modes_for_source({"source_kind": "enterprise_model_gateway"}) == ["enterprise_policy", "quality_first", "budget_safe"]


def test_community_hides_persisted_paid_gateway_and_preserves_cloud_pool(engine):
    settings = get_settings().model_copy(update={"boss_edition": "enterprise_self_hosted"})
    paid = EntitlementBrokerService(settings)
    community = EntitlementBrokerService(
        settings.model_copy(update={"boss_edition": "community"})
    )
    project_id = "boss-dogfood"
    with engine.begin() as connection:
        paid.seed_default_sources(connection, project_id, actor="test")
        assert any(
            source["source_id"] == "enterprise_model_gateway"
            for source in paid.list_sources(connection, project_id)
        )
        community_sources = community.list_sources(connection, project_id)
        assert all(
            source["source_id"] != "enterprise_model_gateway"
            for source in community_sources
        )
        assert any(
            source["source_id"] == "managed_cloud_model_gateway"
            for source in community_sources
        )
        assert all(
            pool["source_id"] != "enterprise_model_gateway"
            for pool in community.list_entitlement_pools(connection, project_id)
        )
        assert connection.execute(
            text("SELECT COUNT(*) FROM entitlement_sources WHERE project_id = :project_id AND source_id = 'enterprise_model_gateway'"),
            {"project_id": project_id},
        ).scalar_one() == 1
