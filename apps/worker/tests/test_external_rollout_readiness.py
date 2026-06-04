from scripts.check_external_rollout_readiness import (
    BLOCKED,
    EXTERNAL_VERIFICATION_REQUIRED,
    READY,
    evaluate_readiness,
    format_text,
)


def _checks_by_id(report):
    return {check["id"]: check for check in report["checks"]}


def test_external_rollout_readiness_blocks_missing_hosted_prerequisites():
    report = evaluate_readiness(env={})
    checks = _checks_by_id(report)

    assert report["status"] == BLOCKED
    assert checks["community_local_mode"]["status"] == READY
    assert checks["hosted_account_connect_service"]["status"] == BLOCKED
    assert "BOSS_ACCOUNT_CONNECT_URL" in checks["hosted_account_connect_service"]["missing"]
    assert checks["enterprise_scim_runtime"]["status"] == BLOCKED
    assert checks["providerops_write_rollout"]["status"] == BLOCKED
    assert checks["live_enterprise_cluster_rollout"]["status"] == EXTERNAL_VERIFICATION_REQUIRED


def test_external_rollout_readiness_distinguishes_config_from_live_verification():
    report = evaluate_readiness(
        env={
            "BOSS_ACCOUNT_CONNECT_URL": "https://account-connect.example.test",
            "BOSS_ACCOUNT_CONNECT_TOKEN": "configured-secret",
            "BOSS_ENTERPRISE_SCIM_BEARER_TOKENS": "configured-secret",
            "BOSS_OIDC_ENABLED": "true",
            "BOSS_OIDC_ISSUER": "https://idp.example.test",
            "BOSS_OIDC_AUDIENCE": "boss",
            "BOSS_OIDC_JWKS_URL": "https://idp.example.test/.well-known/jwks.json",
            "BOSS_INFRA_REAL_PROVISION_ALLOWED": "true",
            "BOSS_DIGITALOCEAN_TOKEN": "configured-secret",
        }
    )
    checks = _checks_by_id(report)

    assert report["status"] == EXTERNAL_VERIFICATION_REQUIRED
    assert checks["hosted_account_connect_service"]["status"] == EXTERNAL_VERIFICATION_REQUIRED
    assert checks["enterprise_scim_runtime"]["status"] == EXTERNAL_VERIFICATION_REQUIRED
    assert checks["enterprise_identity_runtime"]["status"] == EXTERNAL_VERIFICATION_REQUIRED
    assert checks["providerops_write_rollout"]["status"] == EXTERNAL_VERIFICATION_REQUIRED
    assert "configured-secret" not in format_text(report)
