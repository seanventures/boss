"""MCP API operation discovery and authority boundary."""

from __future__ import annotations

import json

import httpx

from boss_engine.mcp import MCPServer
from boss_engine.mcp.api_surface import ApiSurface, operation_alias
from boss_engine.tools.registry import ToolRegistry, ToolSpec


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
                          project_id="alpha", path={"task_id": "t1"})["error"] == (
        "operator_only_unavailable"
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


def test_destructive_and_approval_routes_are_discoverable_but_never_dispatched():
    schema = {"paths": {
        "/v1/activity/retention/prune": {"post": {}},
        "/v1/deployment-hosts/{host_id}/key": {"post": {}},
        "/v1/delivery/deploy": {"post": {}},
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
        assert described["availability"] in {"operator_only_unavailable", "excluded"}
        result = surface.invoke(alias, project_id="alpha")
        assert result["ok"] is False
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
    assert len(compact._tools()) == 4
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
        kind="write", reversibility="safe",
    ))
    registry.register(ToolSpec(
        "native_read", "native DB read", {"type": "object", "properties": {}},
        lambda ctx, *, project_id, actor, args: (called.append(project_id) or {"ok": True}),
        kind="read",
    ))
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


def test_multi_project_principal_is_pinned_to_selected_accessible_project():
    def send(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=SCHEMA)
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
    op = surface.operations[operation_alias("GET", "/v1/tasks")]
    assert surface.availability(op) == "callable"
    assert surface.invoke(op.alias, project_id="alpha")["ok"] is False  # mock 404
    assert surface.invoke(op.alias, project_id="beta")["error"] == "project_is_session_bound"
    other = ApiSurface.connect("http://127.0.0.1:8000", api_key="explicit", client=client,
                               project_id="gamma")
    assert other.availability(op) == "project_membership_unverified"
    assert other.invoke(op.alias, project_id="gamma")["error"] == "project_membership_unverified"


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
