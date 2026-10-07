"""MCP API operation discovery and authority boundary."""

from __future__ import annotations

import json

import httpx

from boss_engine.mcp import MCPServer
from boss_engine.mcp.api_surface import ApiSurface, operation_alias
from boss_engine.tools.registry import ToolRegistry, ToolSpec

import pytest


@pytest.fixture(autouse=True)
def _caller_is_the_launch_key(monkeypatch):
    """These tests drive the bridge as a stdio session would: the caller IS the key the surface
    was launched with (M8: the bridge forwards the caller's own credential and refuses without
    one — pinned in test_identity_f2_tools_mcp_caller.py)."""
    from boss_engine.mcp import api_surface

    original = ApiSurface.invoke

    def invoke(self, *args, **kwargs):
        if api_surface.CALLER_AUTHORIZATION.get():
            return original(self, *args, **kwargs)
        token = api_surface.CALLER_AUTHORIZATION.set(self.launch_authorization or "Bearer test-caller")
        try:
            return original(self, *args, **kwargs)
        finally:
            api_surface.CALLER_AUTHORIZATION.reset(token)

    monkeypatch.setattr(ApiSurface, "invoke", invoke)


SCHEMA = {
    "openapi": "3.1.0",
    "paths": {
        "/v1/tasks": {
            "get": {"summary": "List tasks", "parameters": [
                {"name": "project_id", "in": "query", "required": True,
                 "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "required": False,
                 "schema": {"type": "integer"}},
                {"name": "actor", "in": "query", "required": True,
                 "schema": {"type": "string"}},
            ]},
            "post": {"summary": "Create task", "requestBody": {
                "required": True, "content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/TaskCreate",
                }}}
            }},
        },
        "/v1/tasks/{task_id}": {
            "get": {"summary": "Get task", "parameters": [
                {"name": "task_id", "in": "path", "required": True,
                 "schema": {"type": "string"}},
            ]},
            "delete": {"summary": "Delete task", "parameters": [
                {"name": "task_id", "in": "path", "required": True,
                 "schema": {"type": "string"}},
            ]},
        },
        "/v1/projects/{project_id}/tasks/{task_id}": {
            "get": {"parameters": [
                {"name": "project_id", "in": "path", "required": True,
                 "schema": {"type": "string"}},
                {"name": "task_id", "in": "path", "required": True,
                 "schema": {"type": "string"}},
            ]},
        },
        "/v1/worker/poll": {"post": {"summary": "Lease worker job"}},
        "/v1/providers": {"post": {"summary": "Configure provider", "requestBody": {
            "required": True, "content": {"application/json": {"schema": {
                "type": "object", "properties": {"project_id": {"type": "string"},
                                                  "auth": {"type": "object"}},
            }}}
        }}},
        "/v1/webhooks/github": {"post": {"summary": "GitHub callback"}},
        "/v1/previews/{preview_id}/web/{path}": {
            "get": {"summary": "Preview reverse proxy"},
        },
        "/saml/v2/{project_id}/acs": {"post": {"summary": "SAML callback"}},
    },
    "components": {"schemas": {
        "TaskCreate": {"type": "object", "required": ["title", "project_id"],
                       "properties": {"title": {"type": "string"},
                                      "project_id": {"type": "string"},
                                      "actor": {"type": "string"}}},
    }},
}


def _surface(*, principal: str | None = "operator", status: int = 200):
    seen = []

    def send(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"path": request.url.path, "ok": status < 400})

    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    return ApiSurface(SCHEMA, client=client, principal=principal,
                      edition="community"), seen


def test_alias_is_unique_for_same_path_different_methods_and_fastapi_ids():
    surface, _ = _surface()
    aliases = {op.alias for op in surface.operations.values()}
    assert len(aliases) == 10
    assert operation_alias("GET", "/v1/tasks") != operation_alias("POST", "/v1/tasks")
    assert all(alias.startswith(("get_", "post_", "delete_")) for alias in aliases)


def test_discovery_is_paginated_and_names_excluded_operations():
    surface, _ = _surface(principal=None)
    found = surface.find(limit=2, offset=0)
    assert found["total"] == 10
    assert len(found["operations"]) == 2
    worker = surface.find(query="worker/poll")["operations"][0]
    assert worker["availability"] == "excluded"
    assert worker["reason"] == "worker_lease_protocol"
    for path in ("/v1/webhooks/github", "/v1/previews/{preview_id}/web/{path}",
                 "/saml/v2/{project_id}/acs"):
        op = next(op for op in surface.operations.values() if op.path == path)
        assert surface.availability(op) == "excluded"
    task = surface.find(query="List tasks")["operations"][0]
    assert task["availability"] == "credential_required"


def test_description_contains_exact_path_query_and_body_schema():
    surface, _ = _surface()
    read = surface.describe(operation_alias("GET", "/v1/tasks"))
    assert read["inputSchema"]["properties"]["query"]["properties"] == {
        "limit": {"type": "integer"},
    }
    created = surface.describe(operation_alias("POST", "/v1/tasks"))
    assert "body" in created["inputSchema"]["required"]
    assert created["inputSchema"]["properties"]["body"]["$ref"] == "#/$defs/TaskCreate"
    assert created["inputSchema"]["$defs"]["TaskCreate"]["properties"]["title"] == {
        "type": "string",
    }


def test_project_and_actor_are_fixed_to_session_for_api_reads_and_writes():
    surface, seen = _surface()
    read = surface.invoke(operation_alias("GET", "/v1/tasks"), project_id="alpha",
                          query={"limit": 4})
    assert read["ok"] is True
    assert seen[-1].url.params["project_id"] == "alpha"
    assert seen[-1].url.params["limit"] == "4"
    assert seen[-1].url.params["actor"] == "operator"
    assert surface.invoke(operation_alias("GET", "/v1/tasks"), project_id="alpha",
                          query={"project_id": "beta"})["error"] == "unknown_path_or_query_parameter"

    created = surface.invoke(operation_alias("POST", "/v1/tasks"), project_id="alpha",
                             body={"title": "One"})
    assert created["ok"] is True
    assert json.loads(seen[-1].content) == {
        "title": "One", "project_id": "alpha", "actor": "operator",
    }
    assert surface.invoke(operation_alias("POST", "/v1/tasks"), project_id="alpha",
                          body={"title": "One", "project_id": "beta"})["error"] == "project_is_session_bound"
    assert surface.invoke(operation_alias("POST", "/v1/tasks"), project_id="alpha",
                          body={"title": "One", "actor": "ceo"})["error"] == "actor_is_session_bound"
    assert surface.invoke(operation_alias("GET", "/v1/projects/{project_id}/tasks/{task_id}"),
                          project_id="alpha", path={"task_id": "task 1"})["ok"] is True
    assert seen[-1].url.raw_path == b"/v1/projects/alpha/tasks/task%201"


def test_required_path_and_query_authority_are_bound_without_caller_arguments():
    schema = {"paths": {
        "/v1/edition": {"get": {"parameters": [
            {"name": "project_id", "in": "query", "required": True,
             "schema": {"type": "string"}},
            {"name": "actor", "in": "query", "required": True,
             "schema": {"type": "string"}},
        ]}},
        "/v1/projects/{project_id}/actors/{actor}": {"get": {"parameters": [
            {"name": "project_id", "in": "path", "required": True,
             "schema": {"type": "string"}},
            {"name": "actor", "in": "path", "required": True,
             "schema": {"type": "string"}},
        ]}},
    }}
    seen = []
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(
        lambda request: (seen.append(request), httpx.Response(200, json={"ok": True}))[1]
    ))
    surface = ApiSurface(schema, client=client, principal="member-1",
                         pinned_project_id="alpha")
    for path in schema["paths"]:
        alias = operation_alias("GET", path)
        described = surface.describe(alias)["inputSchema"]
        assert described["required"] == ["operation"]
        assert described["properties"]["path"]["properties"] == {}
        assert described["properties"]["query"]["properties"] == {}
        assert surface.invoke(alias, project_id="alpha")["ok"] is True
    assert seen[0].url.path == "/v1/edition"
    assert dict(seen[0].url.params) == {"project_id": "alpha", "actor": "member-1"}
    assert seen[1].url.path == "/v1/projects/alpha/actors/member-1"
    assert surface.invoke(operation_alias("GET", "/v1/edition"), project_id="alpha",
                          query={"project_id": "beta"})["error"] == (
        "unknown_path_or_query_parameter"
    )


def test_curated_community_body_fields_are_described_and_session_bound():
    schema = {"paths": {"/v1/elastic/capacity": {"post": {"requestBody": {
        "required": True, "content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": True,
        }}}
    }}}}}
    seen = []
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(
        lambda request: (seen.append(json.loads(request.content)),
                         httpx.Response(201, json={"ok": True}))[1]
    ))
    surface = ApiSurface(schema, client=client, principal="member-1",
                         pinned_project_id="alpha")
    alias = operation_alias("POST", "/v1/elastic/capacity")
    described = surface.describe(alias)
    assert described["mounted_input_schema_quality"] == "loose_json_body"
    assert described["input_schema_quality"] == "curated_json_body"
    body_schema = described["inputSchema"]["properties"]["body"]
    assert body_schema["required"] == ["desired_workers"]
    assert body_schema["properties"]["desired_workers"]["type"] == "integer"
    assert "project_id" not in body_schema["properties"]
    assert "actor" not in body_schema["properties"]
    assert surface.invoke(alias, project_id="alpha", body={"desired_workers": 2})["ok"]
    assert seen == [{"desired_workers": 2, "project_id": "alpha", "actor": "member-1"}]
    assert surface.invoke(alias, project_id="alpha", body={
        "desired_workers": 2, "project_id": "beta",
    })["error"] == "project_is_session_bound"
    assert surface.invoke(alias, project_id="alpha", body={
        "desired_workers": 2, "actor": "ceo",
    })["error"] == "actor_is_session_bound"
    assert len(seen) == 1


def test_curated_fields_never_replace_an_updated_mounted_schema():
    schema = {"paths": {"/v1/elastic/capacity": {"post": {"requestBody": {
        "required": True, "content": {"application/json": {"schema": {
            "type": "object", "properties": {"desired_workers": {"type": "integer"}},
        }}}
    }}}}}
    surface = ApiSurface(schema)
    described = surface.describe(operation_alias("POST", "/v1/elastic/capacity"))
    assert described["input_schema_quality"] == "typed_json_body"
    assert described["inputSchema"]["properties"]["body"]["properties"] == {
        "desired_workers": {"type": "integer"},
    }


def test_policy_and_read_only_denials_do_not_send_http_request():
    surface, seen = _surface()
    assert surface.invoke(operation_alias("DELETE", "/v1/tasks/{task_id}"),
                          project_id="alpha", path={"task_id": "t1"},
                          read_only=True)["error"] == (
        "read_only"
    )
    assert surface.invoke(operation_alias("POST", "/v1/worker/poll"),
                          project_id="alpha")["error"] == "excluded"
    assert surface.invoke(operation_alias("POST", "/v1/tasks"), project_id="alpha",
                          body={"title": "One"}, read_only=True)["error"] == "read_only"
    assert seen == []


def test_api_authorization_failure_is_visible_and_not_converted_to_success():
    surface, _ = _surface(status=403)
    result = surface.invoke(operation_alias("GET", "/v1/tasks"), project_id="alpha")
    assert result["ok"] is False
    assert result["status"] == 403


def test_api_response_never_echoes_submitted_secret_values():
    marker = "FAKE-SECRET-NOT-FOR-LOGS-12345"
    def send(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={
            "detail": f"invalid value {marker}",
            "credential": marker,
            "nested": [{"message": f"tried {marker}"}],
        })
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    surface = ApiSurface(SCHEMA, client=client, principal="operator")
    result = surface.invoke(operation_alias("POST", "/v1/providers"), project_id="alpha",
                            body={"auth": {"api_key": marker}})
    assert result["ok"] is False and result["status"] == 422
    assert marker not in json.dumps(result)
    assert result["result"]["credential"] == "[redacted]"


def test_secret_value_echo_is_removed_but_reference_metadata_remains_usable():
    marker = "FAKE-SECRET-MATERIAL-998877"
    schema = {"paths": {"/v1/secrets": {"post": {"requestBody": {
        "required": True, "content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": True,
        }}}
    }}}}}
    def send(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["project_id"] == "alpha"
        assert json.loads(request.content)["actor"] == "operator"
        return httpx.Response(201, json={
            "secret_ref_id": "ref-1", "status": "active", "value": marker,
            "detail": {marker: f"stored {marker}"},
        })
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    surface = ApiSurface(schema, client=client, principal="operator")
    result = surface.invoke(operation_alias("POST", "/v1/secrets"), project_id="alpha",
                            body={"project_id": "alpha", "actor": "operator", "value": marker})
    assert result["ok"] is True
    assert result["result"]["secret_ref_id"] == "ref-1"
    assert marker not in json.dumps(result)


def test_ordinary_budget_and_setting_values_remain_visible():
    schema = {"paths": {
        "/v1/budget/quote": {"post": {"requestBody": {"required": True,
            "content": {"application/json": {"schema": {
                "type": "object", "properties": {"value": {"type": "number"}},
            }}}}}},
        "/v1/settings/{key}": {"put": {"parameters": [{
            "in": "path", "name": "key", "required": True, "schema": {"type": "string"},
        }], "requestBody": {"required": True, "content": {"application/json": {
            "schema": {"type": "object", "additionalProperties": True},
        }}}}},
    }}
    def send(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/budget/quote":
            return httpx.Response(200, json={"value": 17, "key": "monthly_budget"})
        return httpx.Response(200, json={"key": "max_workers", "value": "8"})
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    surface = ApiSurface(schema, client=client, principal="operator")
    quote = surface.invoke(operation_alias("POST", "/v1/budget/quote"),
                           project_id="alpha", body={"value": 17})
    assert quote["result"] == {"value": 17, "key": "monthly_budget"}
    setting = surface.invoke(operation_alias("PUT", "/v1/settings/{key}"),
                             project_id="alpha", path={"key": "max_workers"},
                             body={"value": "8"})
    assert setting["result"] == {"key": "max_workers", "value": "8"}


def test_global_loose_body_is_not_given_an_unrequested_project_or_actor():
    schema = {"paths": {"/v1/global-config": {"post": {"requestBody": {
        "required": True, "content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": True,
        }}}
    }}}}}
    seen = []
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(
        lambda request: (seen.append(json.loads(request.content)),
                         httpx.Response(200, json={"ok": True}))[1]
    ))
    surface = ApiSurface(schema, client=client, principal="operator")
    alias = operation_alias("POST", "/v1/global-config")
    assert surface.invoke(alias, project_id="alpha", body={"mode": "on"})["ok"]
    assert seen[-1] == {"mode": "on"}
    assert surface.invoke(alias, project_id="alpha", body={
        "project_id": "beta",
    })["error"] == "project_is_session_bound"
    assert surface.invoke(alias, project_id="alpha", body=[{
        "project_id": "beta",
    }])["error"] == "invalid_json_body"


def test_held_and_confirmable_routes_never_dispatch_without_approval():
    schema = {"paths": {
        "/v1/activity/retention/prune": {"post": {}},
        "/v1/deployment-hosts/{host_id}/key": {"post": {}},
        "/v1/delivery/deploy": {"post": {}},
        "/v1/improvement-programs/trials/{trial_id}/promote": {"post": {}},
        "/v1/kms/keys/{key_entry_id}/lease": {"post": {}},
        "/v1/kms/leases/{lease_id}/resolve": {"post": {}},
    }}
    seen = []
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(
        lambda request: (seen.append(request), httpx.Response(200, json={}))[1]
    ))
    surface = ApiSurface(schema, client=client, principal="operator")
    for path in schema["paths"]:
        alias = operation_alias("POST", path)
        described = surface.describe(alias)
        assert described["ok"] is True
        assert described["availability"] in {
            "operator_only_unavailable", "excluded", "confirmation_required",
        }
        result = surface.invoke(alias, project_id="alpha")
        assert result["ok"] is False
    proposal_alias = operation_alias(
        "POST", "/v1/improvement-programs/trials/{trial_id}/promote"
    )
    assert surface.describe(proposal_alias)["availability"] == "confirmation_required"
    assert seen == []


def test_preapproved_changeset_apply_and_workflow_mapping_are_api_policy_callable():
    schema = {"paths": {
        "/v1/changesets/{changeset_id}/apply": {"post": {"parameters": [{
            "in": "path", "name": "changeset_id", "required": True,
            "schema": {"type": "string"},
        }]}},
        "/v1/delivery-workflows/apply": {"post": {}},
    }}
    surface, _ = _surface()
    scoped = ApiSurface(schema, client=surface.client, principal="operator")
    for path in schema["paths"]:
        op = scoped.operations[operation_alias("POST", path)]
        assert scoped.availability(op) == "callable"


def test_soft_task_archive_and_safe_migration_rollback_use_api_policy_with_session_scope():
    schema = {"paths": {
        "/v1/tasks/{task_id}": {"delete": {"parameters": [
            {"in": "path", "name": "task_id", "required": True,
             "schema": {"type": "string"}},
            {"in": "query", "name": "project_id", "required": True,
             "schema": {"type": "string"}},
            {"in": "query", "name": "actor", "required": True,
             "schema": {"type": "string"}},
        ]}},
        "/v1/workflow-runs/{run_id}/migrations/rollback": {"post": {
            "parameters": [{"in": "path", "name": "run_id", "required": True,
                            "schema": {"type": "string"}}],
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/WorkflowMigrationRollbackRequest"},
            }}},
        }},
    }, "components": {"schemas": {"WorkflowMigrationRollbackRequest": {
        "type": "object", "required": ["project_id", "actor", "migration_id"],
        "properties": {"project_id": {"type": "string"}, "actor": {"type": "string"},
                       "migration_id": {"type": "string"}},
    }}}}
    seen = []
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(
        lambda request: (seen.append(request), httpx.Response(200, json={"ok": True}))[1]
    ))
    surface = ApiSurface(schema, client=client, principal="mcp-operator",
                         pinned_project_id="alpha")
    archive = operation_alias("DELETE", "/v1/tasks/{task_id}")
    rollback = operation_alias("POST", "/v1/workflow-runs/{run_id}/migrations/rollback")
    for alias in (archive, rollback):
        op = surface.operations[alias]
        assert surface.availability(op) == "callable"
        assert surface.availability(op, read_only=True) == "read_only"
        assert surface.invoke(alias, project_id="beta")["error"] == "project_is_session_bound"
    assert surface.invoke(archive, project_id="alpha", path={"task_id": "t1"})["ok"]
    assert seen[-1].url.path == "/v1/tasks/t1"
    assert dict(seen[-1].url.params) == {"project_id": "alpha", "actor": "mcp-operator"}
    assert surface.invoke(rollback, project_id="alpha", path={"run_id": "r1"},
                          body={"migration_id": "m1"})["ok"]
    assert json.loads(seen[-1].content) == {
        "migration_id": "m1", "project_id": "alpha", "actor": "mcp-operator",
    }
    assert surface.invoke(rollback, project_id="alpha", path={"run_id": "r1"},
                          body={"migration_id": "m1", "project_id": "beta"})["error"] == (
        "project_is_session_bound"
    )
    assert len(seen) == 2


def test_mcp_server_keeps_engine_registry_small_and_read_only_hides_api_write():
    surface, seen = _surface()
    ctx = type("Context", (), {"durable_workspace_configured": True})()
    server = MCPServer(ctx, project_id="alpha", registry=ToolRegistry(),
                       api_surface=surface, read_only=True)
    names = {tool["name"] for tool in server._tools()}
    assert names == {"api_find_operations", "api_describe_operation", "api_read_operation"}
    call = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "api_write_operation", "arguments": {
            "operation": operation_alias("POST", "/v1/tasks"),
            "body": {"title": "No"},
        }},
    })
    assert call["result"]["isError"] is True
    assert seen == []
    found = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "api_find_operations", "arguments": {"query": "Create task"}},
    })
    payload = json.loads(found["result"]["content"][0]["text"])
    assert payload["operations"][0]["availability"] == "read_only"


def test_domain_selection_advertises_concrete_typed_tools_without_full_catalog_flood():
    surface, seen = _surface()
    ctx = type("Context", (), {"durable_workspace_configured": True})()
    compact = MCPServer(ctx, project_id="alpha", registry=ToolRegistry(), api_surface=surface)
    assert len(compact._tools()) == 5
    domain = MCPServer(ctx, project_id="alpha", registry=ToolRegistry(),
                       api_surface=surface, api_domains={"core"})
    tools = {tool["name"]: tool for tool in domain._tools()}
    read_name = operation_alias("GET", "/v1/tasks")
    write_name = operation_alias("POST", "/v1/tasks")
    assert read_name in tools and write_name in tools
    assert operation_alias("POST", "/v1/worker/poll") not in tools
    assert tools[write_name]["inputSchema"]["$defs"]["TaskCreate"]["properties"] == {
        "title": {"type": "string"},
    }
    response = domain.handle({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": write_name, "arguments": {"body": {"title": "Scoped"}}},
    })
    assert response["result"]["isError"] is False
    assert json.loads(seen[-1].content)["project_id"] == "alpha"


def test_remote_api_bridge_can_write_without_local_database_but_native_writes_stay_hidden():
    surface, seen = _surface()
    ctx = type("Context", (), {"durable_workspace_configured": False})()
    server = MCPServer(ctx, project_id="alpha", registry=ToolRegistry(), api_surface=surface)
    names = {tool["name"] for tool in server._tools()}
    assert "api_write_operation" in names
    reply = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "api_write_operation", "arguments": {
            "operation": operation_alias("POST", "/v1/tasks"),
            "body": {"title": "Remote"},
        }},
    })
    assert reply["result"]["isError"] is False
    assert json.loads(seen[-1].content)["title"] == "Remote"


def test_installed_api_container_cannot_use_native_database_authority_in_api_mode():
    surface, seen = _surface()
    called = []
    registry = ToolRegistry()
    registry.register(ToolSpec(
        "native_write", "native DB write", {"type": "object", "properties": {}},
        lambda ctx, *, project_id, actor, args: (called.append(project_id) or {"ok": True}),
        kind="write", reversibility="safe", permission="system:read"))
    registry.register(ToolSpec(
        "native_read", "native DB read", {"type": "object", "properties": {}},
        lambda ctx, *, project_id, actor, args: (called.append(project_id) or {"ok": True}),
        kind="read", permission="system:read"))
    ctx = type("Context", (), {"durable_workspace_configured": True})()
    api_mode = MCPServer(ctx, project_id="alpha", registry=registry, api_surface=surface)
    assert "native_write" not in {tool["name"] for tool in api_mode._tools()}
    assert "native_read" not in {tool["name"] for tool in api_mode._tools()}
    reply = api_mode.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                             "params": {"name": "native_write", "arguments": {}}})
    assert reply["result"]["isError"] is True
    read_reply = api_mode.handle({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                                  "params": {"name": "native_read", "arguments": {}}})
    assert read_reply["result"]["isError"] is True
    assert called == [] and seen == []
    assert registry.get("api_write_operation").reversibility == "costs_money"

    native_mode = MCPServer(ctx, project_id="alpha", registry=ToolRegistry())
    native_mode.registry.register(registry.get("native_write"))
    native_mode.registry.register(registry.get("native_read"))
    assert "native_write" in {tool["name"] for tool in native_mode._tools()}
    assert "native_read" in {tool["name"] for tool in native_mode._tools()}


def test_connect_requires_bound_explicit_api_key_and_rejects_insecure_remote_origin():
    def send(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=SCHEMA)
        if request.url.path == "/v1/auth/session":
            return httpx.Response(200, json={
                "auth_required": True, "transport_authenticated": True,
                "actor_binding_required": True, "auth_method": "api_key",
                "principal_member_id": "scoped-operator",
            })
        if request.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [{"id": "boss-dogfood"}]})
        return httpx.Response(404)

    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    catalog = ApiSurface.connect("http://127.0.0.1:8000", client=client)
    assert catalog.principal is None
    assert catalog.availability(next(iter(catalog.operations.values()))) == "credential_required"
    scoped = ApiSurface.connect("http://127.0.0.1:8000", api_key="explicit-scope", client=client)
    assert scoped.principal == "scoped-operator"
    try:
        ApiSurface.connect("http://untrusted.internal:8000", api_key="secret", client=client)
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("cleartext remote API origin accepted")


def test_multi_project_principal_cannot_dispatch_even_an_id_only_route():
    seen = []
    schema = {**SCHEMA, "paths": {**SCHEMA["paths"],
                              "/v1/discoveries/{run_id}/promote": {"post": {"parameters": [
                                  {"name": "run_id", "in": "path", "required": True,
                                   "schema": {"type": "string"}},
                              ]}}}}
    def send(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=schema)
        if request.url.path == "/v1/auth/session":
            return httpx.Response(200, json={
                "auth_required": True, "transport_authenticated": True,
                "actor_binding_required": True, "auth_method": "api_key",
                "principal_member_id": "owner",
            })
        if request.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [{"id": "alpha"}, {"id": "beta"}]})
        return httpx.Response(404)

    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    surface = ApiSurface.connect("http://127.0.0.1:8000", api_key="explicit", client=client,
                                 project_id="alpha")
    op = surface.operations[operation_alias("POST", "/v1/discoveries/{run_id}/promote")]
    assert surface.availability(op) == "exclusive_project_membership_required"
    assert surface.invoke(op.alias, project_id="alpha", path={"run_id": "run-1"})["error"] == (
        "exclusive_project_membership_required"
    )
    assert surface.invoke(op.alias, project_id="beta", path={"run_id": "run-1"})["error"] == (
        "project_is_session_bound"
    )
    assert "/v1/discoveries/run-1/promote" not in seen
    ctx = type("Context", (), {"durable_workspace_configured": True})()
    server = MCPServer(ctx, project_id="alpha", registry=ToolRegistry(), api_surface=surface)
    assert {tool["name"] for tool in server._tools()} == {
        "api_find_operations", "api_describe_operation", "api_read_operation",
        "api_write_operation", "api_request_confirmation",
    }
    other = ApiSurface.connect("http://127.0.0.1:8000", api_key="explicit", client=client,
                               project_id="gamma")
    assert other.availability(op) == "project_membership_unverified"
    assert other.invoke(op.alias, project_id="gamma", path={"run_id": "run-1"})["error"] == (
        "project_membership_unverified"
    )


def test_long_lived_session_rechecks_project_membership_and_principal_before_id_only_call():
    seen = []
    scope = {"principal": "mcp_operator", "projects": ["alpha"]}

    def send(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=SCHEMA)
        if request.url.path == "/v1/auth/session":
            return httpx.Response(200, json={
                "auth_required": True, "transport_authenticated": True,
                "actor_binding_required": True, "auth_method": "api_key",
                "principal_member_id": scope["principal"],
            })
        if request.url.path == "/v1/projects":
            return httpx.Response(200, json={
                "items": [{"id": project} for project in scope["projects"]],
            })
        if request.url.path == "/v1/tasks/t1":
            return httpx.Response(200, json={"id": "t1"})
        return httpx.Response(404)

    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    surface = ApiSurface.connect("http://127.0.0.1:8000", api_key="dedicated", client=client,
                                 project_id="alpha")
    alias = operation_alias("GET", "/v1/tasks/{task_id}")
    assert surface.invoke(alias, project_id="alpha", path={"task_id": "t1"})["ok"] is True
    assert seen.count("/v1/tasks/t1") == 1

    scope["projects"] = ["alpha", "beta"]
    assert surface.invoke(alias, project_id="alpha", path={"task_id": "t1"})["error"] == (
        "exclusive_project_membership_required"
    )
    scope["projects"] = ["alpha"]
    scope["principal"] = "different_member"
    assert surface.invoke(alias, project_id="alpha", path={"task_id": "t1"})["error"] == (
        "api_principal_changed"
    )
    assert seen.count("/v1/tasks/t1") == 1


def test_ambient_owner_principal_cannot_authorize_api_bridge():
    def send(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=SCHEMA)
        if request.url.path == "/v1/auth/session":
            return httpx.Response(200, json={
                "auth_required": True, "transport_authenticated": True,
                "actor_binding_required": True, "auth_method": "api_key",
                "principal_member_id": "ceo",
            })
        if request.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [{"id": "alpha"}]})
        return httpx.Response(404)

    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    surface = ApiSurface.connect("http://127.0.0.1:8000", api_key="explicit", client=client,
                                 project_id="alpha")
    op = surface.operations[operation_alias("GET", "/v1/tasks")]
    assert surface.availability(op) == "dedicated_mcp_principal_required"


def test_sensitive_mcp_request_needs_exact_separate_confirmation():
    schema = {"paths": {
        "/v1/members": {"post": {
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "properties": {
                    "actor": {"type": "string"}, "note": {"type": "string"},
                }},
            }}},
        }},
        "/v1/workspace/apps/deploy": {"post": {"summary": "Deploy directly"}},
        "/v1/decisions/{decision_id}/reject": {"post": {}},
    }}
    seen = []

    def send(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/mcp-confirmations":
            return httpx.Response(201, json={"id": "mcpconf_" + "a" * 20,
                                             "status": "pending"})
        return httpx.Response(200, json={"status": "rejected"})

    client = httpx.Client(base_url="http://127.0.0.1:8000",
                          transport=httpx.MockTransport(send))
    surface = ApiSurface(schema, client=client, principal="mcp-member",
                         pinned_project_id="alpha")
    alias = operation_alias("POST", "/v1/members")
    held = operation_alias("POST", "/v1/workspace/apps/deploy")
    reviewed_decision = operation_alias("POST", "/v1/decisions/{decision_id}/reject")
    assert surface.describe(alias)["availability"] == "confirmation_required"
    assert "confirmation_id" in surface.describe(alias)["inputSchema"]["properties"]
    assert surface.invoke(alias, project_id="alpha",
                          body={"note": "reviewed"})["error"] == "confirmation_required"
    assert surface.invoke(held, project_id="alpha")["error"] == "operator_only_unavailable"
    assert surface.invoke(reviewed_decision, project_id="alpha")["error"] == "confirmation_required"
    assert seen == []

    proposed = surface.invoke(alias, project_id="alpha", body={"note": "reviewed"},
                              propose_confirmation=True)
    assert proposed["ok"] and proposed["status"] == 201
    proposal = json.loads(seen[-1].content)
    assert proposal == {
        "project_id": "alpha", "operation": alias, "method": "POST",
        "path": "/v1/members", "query": {},
        "body": {"note": "reviewed", "actor": "mcp-member"},
    }
    assert surface.invoke(alias, project_id="alpha", body={"note": "reviewed"},
                          confirmation_id="mcpconf_" + "a" * 20)["ok"]
    assert seen[-1].headers["X-BOSS-MCP-Confirmation"] == "mcpconf_" + "a" * 20
    assert seen[-1].headers["X-BOSS-MCP-Project"] == "alpha"
    assert seen[-1].headers["X-BOSS-MCP-Operation"] == alias
    assert json.loads(seen[-1].content) == proposal["body"]

    server = MCPServer(
        type("Context", (), {"durable_workspace_configured": False})(),
        project_id="alpha", registry=ToolRegistry(), api_surface=surface,
    )
    reply = server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "api_write_operation", "arguments": {
            "operation": alias,
            "body": {"note": "reviewed"},
            "confirmation_id": "mcpconf_" + "a" * 20,
        }},
    })
    assert reply["result"]["isError"] is False
    assert seen[-1].headers["X-BOSS-MCP-Confirmation"] == "mcpconf_" + "a" * 20


def test_project_local_deletions_are_mapped_to_exact_mcp_confirmation():
    routes = {
        "/v1/meeting-roles/{role_key}": "role_key",
        "/v1/console/v2/org/{agent_id}": "agent_id",
    }
    schema = {"paths": {
        route: {"delete": {"parameters": [
            {"name": key, "in": "path", "required": True,
             "schema": {"type": "string"}},
            {"name": "project_id", "in": "query", "required": True,
             "schema": {"type": "string"}},
            {"name": "actor", "in": "query", "required": False,
             "schema": {"type": "string"}},
        ]}}
        for route, key in routes.items()
    }}
    seen = []

    def send(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/mcp-confirmations":
            return httpx.Response(201, json={"id": "mcpconf_" + "a" * 20})
        return httpx.Response(200, json={"cleared": 1})

    client = httpx.Client(base_url="http://127.0.0.1:8000",
                          transport=httpx.MockTransport(send))
    surface = ApiSurface(schema, client=client, principal="mcp-member",
                         pinned_project_id="alpha")
    for route, key in routes.items():
        alias = operation_alias("DELETE", route)
        assert surface.describe(alias)["availability"] == "confirmation_required"
        assert surface.invoke(alias, project_id="alpha", path={key: "one"})["error"] == (
            "confirmation_required"
        )
        assert surface.invoke(alias, project_id="alpha", path={key: "one"},
                              propose_confirmation=True)["ok"]
        proposal = json.loads(seen[-1].content)
        assert proposal == {
            "project_id": "alpha", "operation": alias, "method": "DELETE",
            "path": route.replace("{" + key + "}", "one"),
            "query": {"project_id": "alpha", "actor": "mcp-member"},
            "body": None,
        }
        assert surface.invoke(alias, project_id="alpha", path={key: "one"},
                              confirmation_id="mcpconf_" + "a" * 20)["ok"]
        dispatched = seen[-1]
        assert dispatched.method == "DELETE"
        assert dispatched.url.path == proposal["path"]
        assert dict(dispatched.url.params) == proposal["query"]
        assert dispatched.headers["X-BOSS-MCP-Operation"] == alias
        assert dispatched.headers["X-BOSS-MCP-Project"] == "alpha"


def test_malformed_auth_and_project_responses_fail_closed_without_raw_payload():
    marker = "FAKE-SECRET-IN-MALFORMED-RESPONSE"
    def make_surface(broken_path: str):
        def send(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/openapi.json":
                return httpx.Response(200, json=SCHEMA)
            if request.url.path == broken_path:
                return httpx.Response(200, text="{" + marker)
            if request.url.path == "/v1/auth/session":
                return httpx.Response(200, json={
                    "auth_required": True, "transport_authenticated": True,
                    "actor_binding_required": True, "auth_method": "api_key",
                    "principal_member_id": "scoped-operator",
                })
            return httpx.Response(200, json={"items": [{"id": "alpha"}]})
        client = httpx.Client(base_url="http://127.0.0.1:8000",
                              transport=httpx.MockTransport(send))
        return ApiSurface.connect("http://127.0.0.1:8000", api_key="dedicated",
                                  project_id="alpha", client=client)

    for broken in ("/v1/auth/session", "/v1/projects"):
        surface = make_surface(broken)
        assert surface.principal is None
        assert marker not in surface.scope_reason
        operation = surface.operations[operation_alias("GET", "/v1/tasks")]
        assert surface.availability(operation) != "callable"


def test_schedule_input_edit_is_typed_session_bound_and_not_a_new_plane():
    """Discovery, typed body, and the write all go through the existing schedule operation."""
    schema = {"paths": {"/v1/delivery-workflows/schedules": {
        "get": {"parameters": [
            {"name": "project_id", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "actor", "in": "query", "required": True, "schema": {"type": "string"}},
        ]},
        "post": {"requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object",
            "required": ["project_id", "workflow_key", "name", "cadence"],
            "properties": {
                "project_id": {"type": "string"},
                "actor": {"type": "string"},
                "workflow_key": {"type": "string"},
                "name": {"type": "string"},
                "cadence": {"type": "string"},
                "timezone": {"type": "string"},
                "input": {"type": "object", "additionalProperties": {"type": "string"}},
                "schedule_id": {"type": "string"},
            },
        }}}}}},
    }}
    seen = []

    def send(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content) if request.content else {})
        return httpx.Response(200, json={"id": "wsc_1", "ok": True})

    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(send))
    surface = ApiSurface(schema, client=client, principal="operator", pinned_project_id="alpha")
    alias = operation_alias("POST", "/v1/delivery-workflows/schedules")
    found = surface.find(query="schedule", limit=5)
    assert any(item["alias"] == alias for item in found["operations"])
    body = surface.describe(alias)["inputSchema"]["properties"]["body"]
    assert body["properties"]["input"]["type"] == "object"
    assert "schedule_id" in body["properties"]
    assert "schedule_id" not in body["required"]
    result = surface.invoke(alias, project_id="alpha", body={
        "workflow_key": "weekly_newsletter",
        "name": "Weekly newsletter — Monday",
        "cadence": "weekly",
        "timezone": "Europe/London",
        "schedule_id": "wsc_1",
        "input": {"sectors": "energy", "to": "reader@example.com"},
    })
    assert result["ok"] is True
    assert seen[-1] == {
        "workflow_key": "weekly_newsletter",
        "name": "Weekly newsletter — Monday",
        "cadence": "weekly",
        "timezone": "Europe/London",
        "schedule_id": "wsc_1",
        "input": {"sectors": "energy", "to": "reader@example.com"},
        "project_id": "alpha",
        "actor": "operator",
    }
    listed = surface.invoke(
        operation_alias("GET", "/v1/delivery-workflows/schedules"), project_id="alpha")
    assert listed["ok"] is True
