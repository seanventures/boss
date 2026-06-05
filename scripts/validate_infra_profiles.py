from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "deploy" / "environments" / "boss-infra-profiles.yaml"


class InfraProfileValidationError(RuntimeError):
    pass


def load_profiles(path: Path = PROFILE_PATH) -> dict[str, Any]:
    if not path.exists():
        raise InfraProfileValidationError(f"missing infra profile file: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise InfraProfileValidationError("infra profile file must contain a YAML object.")
    return payload


def validate_profiles(path: Path = PROFILE_PATH) -> dict[str, Any]:
    payload = load_profiles(path)
    if payload.get("kind") != "BossInfraEnvironmentProfiles":
        raise InfraProfileValidationError("kind must be BossInfraEnvironmentProfiles.")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise InfraProfileValidationError("profiles must be a non-empty list.")

    seen_ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise InfraProfileValidationError("each profile must be an object.")
        profile_id = _required_text(profile, "id")
        if profile_id in seen_ids:
            raise InfraProfileValidationError(f"duplicate profile id: {profile_id}")
        seen_ids.add(profile_id)
        by_id[profile_id] = profile
        _validate_profile(profile)

    required_profiles = {
        "local_dogfood",
        "staging_cluster",
        "production_control_plane",
        "enterprise_private_cluster",
    }
    missing = sorted(required_profiles - seen_ids)
    if missing:
        raise InfraProfileValidationError(f"missing required profiles: {missing}")

    return {
        "path": str(path),
        "version": payload.get("version"),
        "profile_count": len(profiles),
        "profiles": sorted(seen_ids),
        "by_id": by_id,
    }


def _validate_profile(profile: dict[str, Any]) -> None:
    profile_id = _required_text(profile, "id")
    environment = _required_text(profile, "environment")
    runtime = _required_mapping(profile, "runtime")
    datastore = _required_mapping(profile, "datastore")
    auth = _required_mapping(profile, "auth")
    scaling = _required_mapping(profile, "scaling")
    providerops = _required_mapping(profile, "providerops")
    rollout_gates = _required_mapping(profile, "rollout_gates")
    kill_switches = _required_mapping(profile, "kill_switches")

    if not _required_text(runtime, "deploy_artifact"):
        raise InfraProfileValidationError(f"{profile_id}: deploy_artifact is required.")
    if not (ROOT / runtime["deploy_artifact"]).exists():
        raise InfraProfileValidationError(
            f"{profile_id}: deploy_artifact does not exist: {runtime['deploy_artifact']}"
        )

    if environment != "local" and auth.get("api_auth_required") is not True:
        raise InfraProfileValidationError(
            f"{profile_id}: non-local environments must require API auth."
        )

    if environment in {"production", "enterprise_private"}:
        if auth.get("oidc_or_saml_required") is not True:
            raise InfraProfileValidationError(
                f"{profile_id}: production/private environments require OIDC or SAML."
            )
        if datastore.get("postgres_ha_required") is not True:
            raise InfraProfileValidationError(
                f"{profile_id}: production/private environments require HA Postgres."
            )
        if datastore.get("backup_required") is not True:
            raise InfraProfileValidationError(
                f"{profile_id}: production/private environments require backups."
            )
        if datastore.get("backup_restore_drill_required") is not True:
            raise InfraProfileValidationError(
                f"{profile_id}: production/private environments require a restore drill."
            )

    if scaling.get("budget_governor_wins") is not True:
        raise InfraProfileValidationError(
            f"{profile_id}: Budget Governor must always win over autoscaling."
        )
    worker_pools = scaling.get("worker_pools")
    if not isinstance(worker_pools, list) or not worker_pools:
        raise InfraProfileValidationError(f"{profile_id}: worker_pools must not be empty.")
    for pool in worker_pools:
        _validate_worker_pool(profile_id, pool)

    required_providerops_flags = {
        "require_changeset_for_write",
        "require_budget_reservation_for_write",
        "require_approval_for_destructive",
        "require_resource_receipt",
        "require_rollback_for_production_dns_or_deploy",
    }
    for key in sorted(required_providerops_flags):
        if providerops.get(key) is not True:
            raise InfraProfileValidationError(f"{profile_id}: providerops.{key} must be true.")
    if providerops.get("raw_provider_secrets_to_agents") is not False:
        raise InfraProfileValidationError(
            f"{profile_id}: raw provider secrets must never be exposed to agents."
        )

    commands = rollout_gates.get("required_commands")
    if not isinstance(commands, list) or "make smoke" not in commands:
        raise InfraProfileValidationError(
            f"{profile_id}: rollout gates must include make smoke."
        )
    if environment != "local":
        for required_command in ("make k8s-validate", "make release-blockers"):
            if required_command not in commands:
                raise InfraProfileValidationError(
                    f"{profile_id}: rollout gates must include {required_command}."
                )
        if rollout_gates.get("approval_required") is not True:
            raise InfraProfileValidationError(
                f"{profile_id}: non-local rollouts require approval."
            )
        if rollout_gates.get("rollback_required") is not True:
            raise InfraProfileValidationError(
                f"{profile_id}: non-local rollouts require rollback evidence."
            )

    for key in ("freeze_all_provisioning", "freeze_model_api_spend"):
        if kill_switches.get(key) is not True:
            raise InfraProfileValidationError(f"{profile_id}: kill_switches.{key} required.")
    if environment in {"production", "enterprise_private", "staging"}:
        for key in ("freeze_destructive_actions", "freeze_public_cloud_spillover"):
            if kill_switches.get(key) is not True:
                raise InfraProfileValidationError(
                    f"{profile_id}: kill_switches.{key} required."
                )


def _validate_worker_pool(profile_id: str, pool: Any) -> None:
    if not isinstance(pool, dict):
        raise InfraProfileValidationError(f"{profile_id}: worker pool must be an object.")
    pool_id = _required_text(pool, "id")
    job_kinds = pool.get("job_kinds")
    if not isinstance(job_kinds, list) or not job_kinds:
        raise InfraProfileValidationError(
            f"{profile_id}/{pool_id}: worker pool must list job_kinds."
        )
    min_replicas = pool.get("min_replicas")
    max_replicas = pool.get("max_replicas")
    if not isinstance(min_replicas, int) or not isinstance(max_replicas, int):
        raise InfraProfileValidationError(
            f"{profile_id}/{pool_id}: min_replicas and max_replicas must be integers."
        )
    if min_replicas < 0 or max_replicas < min_replicas:
        raise InfraProfileValidationError(
            f"{profile_id}/{pool_id}: replica range must be non-negative and ordered."
        )
    _required_text(pool, "scale_signal")


def _required_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        profile_id = payload.get("id", "unknown")
        raise InfraProfileValidationError(f"{profile_id}: {key} must be an object.")
    return value


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        profile_id = payload.get("id", "unknown")
        raise InfraProfileValidationError(f"{profile_id}: {key} must be a non-empty string.")
    return value.strip()


def main() -> None:
    result = validate_profiles()
    print(
        "Validated "
        f"{result['profile_count']} infra profiles from {PROFILE_PATH}: "
        f"{', '.join(result['profiles'])}."
    )


if __name__ == "__main__":
    main()
