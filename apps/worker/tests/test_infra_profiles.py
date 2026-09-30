from copy import deepcopy

import pytest
import yaml

from scripts.validate_infra_profiles import (
    InfraProfileValidationError,
    PROFILE_PATH,
    validate_profiles,
)


def _write_profiles(tmp_path, payload):
    path = tmp_path / "boss-infra-profiles.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _load_payload():
    return yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))


def _profile(payload, profile_id):
    return next(item for item in payload["profiles"] if item["id"] == profile_id)


def test_infra_profiles_capture_required_operating_environments():
    result = validate_profiles()

    assert result["profile_count"] == 4
    assert result["profiles"] == [
        "enterprise_private_cluster",
        "local_dogfood",
        "production_control_plane",
        "staging_cluster",
    ]


def test_infra_profiles_require_budget_governor_over_autoscaler(tmp_path):
    payload = _load_payload()
    _profile(payload, "production_control_plane")["scaling"][
        "budget_governor_wins"
    ] = False

    with pytest.raises(InfraProfileValidationError, match="Budget Governor"):
        validate_profiles(_write_profiles(tmp_path, payload))


def test_infra_profiles_require_provider_write_gates(tmp_path):
    payload = _load_payload()
    _profile(payload, "staging_cluster")["providerops"][
        "require_changeset_for_write"
    ] = False

    with pytest.raises(InfraProfileValidationError, match="require_changeset"):
        validate_profiles(_write_profiles(tmp_path, payload))


def test_infra_profiles_require_non_local_auth_and_release_gates(tmp_path):
    payload = _load_payload()
    staging = _profile(payload, "staging_cluster")
    staging["auth"]["api_auth_required"] = False

    with pytest.raises(InfraProfileValidationError, match="API auth"):
        validate_profiles(_write_profiles(tmp_path, payload))

    payload = _load_payload()
    staging = _profile(payload, "staging_cluster")
    staging["rollout_gates"]["required_commands"].remove("make release-blockers")

    with pytest.raises(InfraProfileValidationError, match="make release-blockers"):
        validate_profiles(_write_profiles(tmp_path, payload))


def test_infra_profiles_require_production_ha_and_restore_drill(tmp_path):
    payload = _load_payload()
    production = _profile(payload, "production_control_plane")
    production["datastore"]["backup_restore_drill_required"] = False

    with pytest.raises(InfraProfileValidationError, match="restore drill"):
        validate_profiles(_write_profiles(tmp_path, payload))


def test_infra_profiles_require_valid_worker_pool_ranges(tmp_path):
    payload = _load_payload()
    pool = deepcopy(
        _profile(payload, "enterprise_private_cluster")["scaling"]["worker_pools"][0]
    )
    pool["max_replicas"] = pool["min_replicas"] - 1
    _profile(payload, "enterprise_private_cluster")["scaling"]["worker_pools"][0] = pool

    with pytest.raises(InfraProfileValidationError, match="replica range"):
        validate_profiles(_write_profiles(tmp_path, payload))
