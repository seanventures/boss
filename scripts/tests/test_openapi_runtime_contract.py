"""Release boundary checks for the edition HTTP contracts."""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]

RAW_BODY_OPERATIONS = {
    ("post", "/saml/v2/{project_id}/acs"),
    ("post", "/v1/reliability/alert"),
    ("post", "/scim/v2/{project_id}/Groups"),
    ("put", "/scim/v2/{project_id}/Groups/{group_id}"),
    ("post", "/scim/v2/{project_id}/Users"),
    ("put", "/scim/v2/{project_id}/Users/{user_id}"),
    ("post", "/v1/webhooks/inbound/{source}"),
    ("post", "/v1/webhooks/github"),
    ("post", "/v1/webhooks/bitbucket"),
    ("post", "/v1/webhooks/plane"),
}
RAW_HEADER_OPERATIONS = {
    ("post", "/v1/webhooks/inbound/{source}"),
    ("post", "/v1/webhooks/github"),
}


def _mounted_enterprise_schema() -> dict:
    """Import a fresh enterprise app, independent of other tests' edition."""
    env = os.environ.copy()
    env["BOSS_EDITION"] = "enterprise"
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "apps/api"), str(ROOT / "engine")))
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import json; from app.main import app; print(json.dumps(app.openapi()))",
        ],
        cwd=ROOT,
        env=env,
        text=True,
    )
    return json.loads(output)


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


def test_private_schema_names_every_operation_uniquely():
    document = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())
    operations = [
        operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    ]
    operation_ids = [operation.get("operationId") for operation in operations]
    assert all(operation_ids)
    assert len(operation_ids) == len(set(operation_ids))


def test_private_request_inputs_match_mounted_enterprise_routes():
    private_document = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())
    static = private_document["paths"]
    mounted = _mounted_enterprise_schema()["paths"]
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    mounted_ops = {
        (method, path): operation
        for path, path_item in mounted.items()
        for method, operation in path_item.items()
        if method in methods
    }
    static_ops = {
        (method, path): operation
        for path, path_item in static.items()
        for method, operation in path_item.items()
        if method in methods
    }
    assert static_ops.keys() == mounted_ops.keys()

    for key, live_operation in mounted_ops.items():
        documented = static_ops[key]
        live_params = {
            (parameter["in"], parameter["name"]): parameter
            for parameter in live_operation.get("parameters", [])
        }
        documented_params = {
            (parameter["in"], parameter["name"]): parameter
            for parameter in documented.get("parameters", [])
        }
        assert live_params.keys() <= documented_params.keys(), key
        extra_params = documented_params.keys() - live_params.keys()
        if key in RAW_HEADER_OPERATIONS:
            assert all(location == "header" for location, _ in extra_params), key
        else:
            assert not extra_params, key
        for identity, parameter in live_params.items():
            actual = documented_params[identity].copy()
            if "description" not in parameter:
                actual.pop("description", None)
            assert actual == parameter, (key, identity)

        live_body = live_operation.get("requestBody")
        documented_body = documented.get("requestBody")
        if live_body is not None:
            runtime_content = live_body.get("content", {})
            runtime_json = runtime_content.get("application/json", {}).get("schema", {})
            documented_json = (
                (documented_body or {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if (
                len(runtime_content) == 1
                and runtime_json.get("type") == "object"
                and runtime_json.get("additionalProperties") is True
                and not runtime_json.get("properties")
                and documented_json.get("properties")
            ):
                # `payload: dict` exposes no fields to FastAPI. The private
                # contract retains manually documented accepted fields.
                assert documented_body["required"] == live_body.get("required", False), key
                assert documented_json["additionalProperties"] is True, key
                assert documented_body["content"].keys() == runtime_content.keys(), key
            else:
                assert documented_body == live_body, key
        elif key in RAW_BODY_OPERATIONS:
            assert documented_body and documented_body["required"] is True, key
        else:
            assert documented_body is None, key

    assert static_ops[("post", "/v1/workflows")]["requestBody"]["required"] is True
    assert static_ops[("get", "/v1/workflows")]["parameters"]
    # The request uses a component reference in both published contracts. Resolve the
    # reference before checking its fields instead of assuming an inline schema.
    for document in (private_document,
                     yaml.safe_load((ROOT / "api/openapi.community.yaml").read_text())):
        schema_ref = document["paths"]["/v1/elastic/capacity"]["post"]["requestBody"][
            "content"]["application/json"]["schema"]["$ref"]
        assert schema_ref.startswith("#/components/schemas/")
        schema = document["components"]["schemas"][schema_ref.rsplit("/", 1)[-1]]
        assert "desired_workers" in schema["properties"]
        assert "desired_workers" in schema["required"]
    assert "requestBody" not in static_ops[("post", "/v1/reliability/meta-eval")]


def test_live_openapi_names_mounted_operations_without_shadowed_handlers():
    from fastapi.routing import APIRoute

    from apps.api.app.main import app
    from boss_engine.services.api_operation_ids import stable_operation_id

    document = app.openapi()
    ids = []
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_id = operation["operationId"]
            assert operation_id == stable_operation_id(method, path)
            ids.append(operation_id)
    assert len(ids) == len(set(ids))

    handlers = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            key = (method, route.path)
            assert key not in handlers, f"{key} is shadowed by two route handlers"
            handlers.add(key)


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
