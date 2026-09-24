"""Release boundary checks for the generated Community HTTP contract."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_community_schema_contains_only_mounted_shared_identity_routes():
    document = yaml.safe_load((ROOT / "api/openapi.community.yaml").read_text())
    paths = document["paths"]
    assert "/saml/v2/{project_id}/metadata" in paths
    assert "/scim/v2/{project_id}/Users" in paths
    assert "/v1/enterprise/identity-providers" in paths
    assert not any(
        path.startswith(("/v1/account-connect", "/v1/marketplace", "/v1/self-improvement"))
        or (
            path.startswith("/v1/enterprise/")
            and not path.startswith(
                (
                    "/v1/enterprise/identity-providers",
                    "/v1/enterprise/identity-rollout-policies",
                    "/v1/enterprise/scim/",
                )
            )
        )
        for path in paths
    )


def test_community_schema_has_unique_stable_operation_ids():
    document = yaml.safe_load((ROOT / "api/openapi.community.yaml").read_text())
    operations = [
        operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    ]
    operation_ids = [operation["operationId"] for operation in operations]
    assert len(operation_ids) == len(set(operation_ids))


def test_cloud_contract_keeps_local_reads_and_excludes_hosted_operations():
    community = yaml.safe_load((ROOT / "api/openapi.community.yaml").read_text())["paths"]
    private = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())["paths"]
    hosted_operations = {
        ("/v1/cloud/workers/enroll", "post"),
        ("/v1/cloud/sync", "post"),
        ("/v1/cloud/entitlements", "get"),
        ("/v1/cloud/team-invites", "get"),
        ("/v1/cloud/team-invites", "post"),
        ("/v1/cloud/marketplace", "get"),
        ("/v1/cloud/retention", "put"),
    }
    for path, method in hosted_operations:
        assert method in private[path]
        assert method not in community.get(path, {})
    for path, method in (
        ("/v1/cloud/catalog", "get"),
        ("/v1/cloud/status", "get"),
        ("/v1/cloud/workers", "get"),
        ("/v1/cloud/retention", "get"),
        ("/v1/cloud/easy-connect", "post"),
        ("/v1/cloud/unlink", "post"),
    ):
        assert method in community[path]


def test_agent_template_routes_are_documented_in_both_editions():
    for artifact in ("openapi.community.yaml", "openapi.yaml"):
        paths = yaml.safe_load((ROOT / "api" / artifact).read_text())["paths"]
        assert "get" in paths["/v1/agent-studio/templates"]
        assert "post" in paths["/v1/agent-studio/templates/{template_id}/install"]
