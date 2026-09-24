"""Private edition skill packs and optional Cloud readiness retain their behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

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
