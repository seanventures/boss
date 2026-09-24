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
