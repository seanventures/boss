from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services import agent_loop
from apps.api.app.services.entitlements import EntitlementBrokerService
from apps.api.app.services.mcp_registry import list_mcp_servers, register_mcp_server
from apps.api.app.services.skill_packaging import (
    CapabilityBundleError,
    CapabilityBundleService,
)
from apps.api.app.services.skill_registry import resolve_for_task
from apps.api.app.services.skill_runtime import SkillRuntimeService
from apps.api.app.services.specpack import SpecPackService


def _custom_skill(skill_id: str = "local.release.notes") -> dict:
    return {
        "id": skill_id,
        "kind": "local",
        "system": False,
        "load": "on_demand",
        "risk_tier": 0,
        "description": "Draft local release notes.",
        "mock_mode": False,
        "input_schema": {
            "type": "object",
            "properties": {"changes": {"type": "array"}},
        },
        "output_schema": {
            "type": "object",
            "properties": {"notes": {"type": "string"}},
        },
        "cost": {"estimator": "fixed", "category": "tool"},
        "retry": {"max_attempts": 1, "backoff": "none"},
        "permissions": {
            "allowed_callers": ["agent_loop"],
            "required_permissions": ["local_execute"],
        },
        "data_boundary": "internal",
        "policy": {"requires_receipt": False},
    }


def _usage() -> dict:
    return {
        "model": "test-agent",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }


class SequenceAdapter:
    def __init__(self, calls: list[tuple[str, dict]]):
        self.calls = calls
        self.index = 0
        self.seen_tools: list[list[str]] = []

    def create_chat_completion(self, messages, tools=None):
        names = [item["function"]["name"] for item in tools or []]
        self.seen_tools.append(names)
        if self.index >= len(self.calls):
            raw = {"role": "assistant", "content": "done"}
            return {"raw": raw, "content": "done", "tool_calls": []}, _usage()
        target, arguments = self.calls[self.index]
        assert target in names
        self.index += 1
        raw = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_{self.index}",
                    "type": "function",
                    "function": {
                        "name": target,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }
        return (
            {
                "raw": raw,
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{self.index}",
                        "name": target,
                        "arguments": json.dumps(arguments),
                    }
                ],
            },
            _usage(),
        )


def test_signed_bundle_verifies_and_unsigned_bundle_requires_override(engine):
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    settings = get_settings().model_copy(
        update={
            "skill_signing_private_key": base64.b64encode(private_raw).decode(),
            "skill_signing_key_id": "release-key",
            "skill_trust_keys": json.dumps(
                {"release-key": base64.b64encode(public_raw).decode()}
            ),
        }
    )
    service = CapabilityBundleService(settings)
    skill = _custom_skill()
    exported = service.export_bundle(
        kind="skill", identifier=skill["id"], payload=skill
    )
    verified = service.verify_bundle(exported, expected_kind="skill")
    assert verified["provenance"]["trusted"] is True
    assert verified["payload"] == skill
    assert json.loads(exported)["payload"] == skill
    with engine.begin() as connection:
        imported = service.import_skill(
            connection,
            settings.default_project_id,
            exported,
            allow_untrusted=False,
            enabled=True,
            on_demand=None,
            actor="ceo",
        )
    assert imported["provenance"]["trusted"] is True
    tampered_signature = json.loads(exported)
    tampered_signature["signature"]["value"] = base64.b64encode(b"0" * 64).decode()
    try:
        service.verify_bundle(
            json.dumps(tampered_signature), expected_kind="skill"
        )
    except CapabilityBundleError as error:
        assert "signature verification failed" in str(error)
    else:
        raise AssertionError("invalid trusted-key signature was accepted")

    unsigned_service = CapabilityBundleService(get_settings())
    unsigned = unsigned_service.export_bundle(
        kind="skill", identifier=skill["id"], payload=skill
    )
    try:
        unsigned_service.verify_bundle(unsigned, expected_kind="skill")
    except CapabilityBundleError as error:
        assert "allow_untrusted=true" in str(error)
    else:
        raise AssertionError("unsigned bundle was accepted without explicit override")
    assert unsigned_service.verify_bundle(
        unsigned, allow_untrusted=True, expected_kind="skill"
    )["provenance"]["trusted"] is False


def test_skill_bundle_import_round_trip_and_trust_filter(engine):
    settings = get_settings()
    service = CapabilityBundleService(settings)
    skill = _custom_skill()
    bundle = service.export_bundle(
        kind="skill",
        identifier=skill["id"],
        payload=skill,
        publisher={"id": "local-test"},
    )
    with engine.begin() as connection:
        imported = service.import_skill(
            connection,
            settings.default_project_id,
            bundle,
            allow_untrusted=True,
            enabled=True,
            on_demand=None,
            actor="ceo",
        )
        catalog = EntitlementBrokerService(settings).list_skills(
            settings.default_project_id, connection=connection
        )
        exported = service.export_skill(
            connection, settings.default_project_id, skill["id"]
        )
        trusted_only = EntitlementBrokerService(
            settings.model_copy(update={"exclude_untrusted_skills": True})
        ).list_skills(settings.default_project_id, connection=connection)
        event = connection.execute(
            text(
                "SELECT data FROM events WHERE event_type = 'skill.imported' "
                "AND entity_id = :entity_id ORDER BY created_at DESC LIMIT 1"
            ),
            {"entity_id": imported["id"]},
        ).scalar_one()
        invocation = SkillRuntimeService(settings).invoke(
            connection,
            settings.default_project_id,
            skill["id"],
            "agent_loop",
            {"changes": []},
            mock=True,
        )

    assert imported["provenance"]["trusted"] is False
    assert next(item for item in catalog if item["id"] == skill["id"])[
        "on_demand"
    ] is True
    assert json.loads(exported)["payload"] == skill
    assert skill["id"] not in {item["id"] for item in trusted_only}
    assert event["trusted"] is False
    assert invocation["status"] == "succeeded"


def test_mcp_registry_exposes_provenance_and_tool_risk_overrides(engine):
    settings = get_settings()
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM mcp_servers WHERE project_id = :project_id "
                "AND name = 'test-provenance-server'"
            ),
            {"project_id": settings.default_project_id},
        )
        server = register_mcp_server(
            connection,
            settings.default_project_id,
            "test-provenance-server",
            "http://127.0.0.1:9999/mcp",
            risk_tier=1,
            publisher={"id": "acme"},
            provenance={"source": "bundle", "trusted": True},
            tool_risk_overrides={"destroy": 3},
            actor="test",
        )
        listed = list_mcp_servers(connection, settings.default_project_id)

    assert server["publisher"] == {"id": "acme"}
    assert server["provenance"]["trusted"] is True
    assert server["tool_risk_overrides"] == {"destroy": 3}
    assert next(item for item in listed if item["id"] == server["id"])[
        "tool_risk_overrides"
    ]["destroy"] == 3
    builtin = next(item for item in listed if item["name"] == "boss-devops-reference")
    assert builtin["publisher"]["id"] == "boss"
    assert builtin["provenance"] == {
        "source": "builtin",
        "version": "1",
        "trusted": True,
    }


def test_high_risk_mcp_tool_override_is_deferred(engine, monkeypatch):
    settings = get_settings().model_copy(update={"agent_autonomous_risk_max": 1})
    adapter = SequenceAdapter([("mcp_ops_destroy", {"resource": "test"})])
    server = {
        "id": "mcp_ops",
        "name": "ops",
        "url": "http://ops",
        "risk_tier": 0,
        "data_boundary": "internal",
        "publisher": {"id": "test"},
        "provenance": {"trusted": True},
        "tool_risk_overrides": {"destroy": 3},
    }
    monkeypatch.setattr(agent_loop, "make_chat_adapter", lambda _settings: adapter)
    monkeypatch.setattr(
        agent_loop, "list_mcp_servers", lambda *_args, **_kwargs: [server]
    )
    monkeypatch.setattr(
        agent_loop.MCPClient,
        "list_tools",
        lambda *_args, **_kwargs: [
            {
                "name": "destroy",
                "description": "Destroy a resource",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
    )

    with engine.begin() as connection:
        result = agent_loop.run_agent_loop(
            connection,
            settings,
            settings.default_project_id,
            "system",
            "Destroy the test resource",
            max_steps=1,
        )
        run = agent_loop.load_agent_run(connection, result["run_id"])

    assert result["status"] == "suspended"
    assert run["pending"]["kind"] == "mcp"
    assert run["pending"]["tool_name"] == "destroy"


def test_mcp_tool_runs_through_skill_gauntlet_with_receipt(engine, monkeypatch):
    settings = get_settings()
    server = {
        "id": "mcp_receipt",
        "name": "receipt-test",
        "url": "http://receipt-test",
        "risk_tier": 1,
        "data_boundary": "internal",
        "publisher": {"id": "test"},
        "provenance": {"trusted": True},
        "tool_risk_overrides": {},
    }
    tool = {
        "name": "inspect",
        "description": "Inspect a resource",
        "inputSchema": {"type": "object", "properties": {}},
    }
    monkeypatch.setattr(
        agent_loop.MCPClient,
        "call_tool",
        lambda *_args, **_kwargs: {"value": "ok"},
    )
    with engine.begin() as connection:
        result = agent_loop._invoke_mcp_skill(
            connection,
            settings,
            settings.default_project_id,
            server,
            tool,
            {"resource": "demo"},
            run_id="run_mcp_receipt",
            step=1,
            actor="agent_loop",
        )
        receipt = connection.execute(
            text(
                "SELECT status, effect_kind FROM receipts "
                "WHERE id = :receipt_id"
            ),
            {"receipt_id": result["result"]["receipt_id"]},
        ).mappings().one()

    assert result["status"] == "succeeded"
    assert result["skill_id"] == "mcp.mcp_receipt.inspect"
    assert result["attempts"] == 1
    assert result["actual_cost_usd_micros"] == 0
    assert result["cost_receipt_id"]
    assert receipt == {"status": "succeeded", "effect_kind": "mcp.tool.call"}


def test_on_demand_skill_lazily_attaches_for_remainder_of_run(engine, monkeypatch):
    settings = get_settings().model_copy(update={"agent_autonomous_risk_max": 3})
    adapter = SequenceAdapter(
        [
            (
                "load_on_demand_skill",
                {"skill_id": "github.create_pr_sandbox"},
            ),
            (
                "skill_github_create_pr_sandbox",
                {"repo": "boss", "title": "test", "body": "test"},
            ),
        ]
    )
    invoked: list[str] = []
    monkeypatch.setattr(agent_loop, "make_chat_adapter", lambda _settings: adapter)
    monkeypatch.setattr(agent_loop, "list_mcp_servers", lambda *_args, **_kwargs: [])

    def fake_invoke(_service, _connection, _project_id, skill_id, *_args, **_kwargs):
        invoked.append(skill_id)
        return {"status": "succeeded", "result": {"ok": True}}

    monkeypatch.setattr(agent_loop.SkillRuntimeService, "invoke", fake_invoke)
    with engine.begin() as connection:
        result = agent_loop.run_agent_loop(
            connection,
            settings,
            settings.default_project_id,
            "system",
            "Prepare a sandbox PR",
            max_steps=3,
            finalize_ctx={
                "job_kind": "openai.plan",
                "module_id": "codex_software_engineer_v1",
            },
        )

    assert result["status"] == "completed"
    assert invoked == ["github.create_pr_sandbox"]
    assert "skill_github_create_pr_sandbox" not in adapter.seen_tools[0]
    assert "skill_github_create_pr_sandbox" in adapter.seen_tools[1]


def test_role_change_is_rechecked_and_violation_recorded_on_hot_path(
    engine, monkeypatch
):
    settings = get_settings().model_copy(
        update={
            "boss_edition": "enterprise_self_hosted",
            "agent_autonomous_risk_max": 3,
        }
    )
    adapter = SequenceAdapter(
        [
            (
                "skill_deployment_health_check",
                {"deployment_run_id": "dep_test"},
            )
        ]
    )
    original_completion = adapter.create_chat_completion

    with engine.begin() as connection:
        def mutate_then_complete(messages, tools=None):
            if adapter.index == 0:
                row = connection.execute(
                    text(
                        "SELECT id, spec FROM roles WHERE project_id = :project_id "
                        "AND role_key = 'deployment_manager'"
                    ),
                    {"project_id": settings.default_project_id},
                ).mappings().one()
                spec = dict(row["spec"] or {})
                permissions = dict(spec.get("permissions") or {})
                permissions["allowed_skills"] = []
                permissions["forbidden_skills"] = ["deployment.health.check"]
                spec["permissions"] = permissions
                connection.execute(
                    text("UPDATE roles SET spec = CAST(:spec AS JSONB) WHERE id = :id"),
                    {"id": row["id"], "spec": json.dumps(spec)},
                )
            return original_completion(messages, tools)

        monkeypatch.setattr(
            adapter, "create_chat_completion", mutate_then_complete
        )
        monkeypatch.setattr(
            agent_loop, "make_chat_adapter", lambda _settings: adapter
        )
        monkeypatch.setattr(
            agent_loop, "list_mcp_servers", lambda *_args, **_kwargs: []
        )
        result = agent_loop.run_agent_loop(
            connection,
            settings,
            settings.default_project_id,
            "system",
            "Check deployment health",
            max_steps=2,
            finalize_ctx={
                "job_kind": "agent.deployment_manager.deployment_review",
                "module_id": "deployment_manager_v1",
            },
        )
        violations = connection.execute(
            text(
                "SELECT count(*) FROM events WHERE project_id = :project_id "
                "AND event_type = 'role.scope_violation'"
            ),
            {"project_id": settings.default_project_id},
        ).scalar_one()

    assert result["status"] == "completed"
    assert violations == 1
    assert '"status": "denied"' in result["transcript"][0]["result_preview"]


def test_enable_disable_changes_next_task_resolution(client, engine):
    settings = get_settings()
    payload = {"project_id": settings.default_project_id, "actor": "ceo"}
    enabled = client.post(
        "/v1/skills/github.create_pr_sandbox/enable",
        json={**payload, "on_demand": True},
    )
    assert enabled.status_code == 200, enabled.text
    with engine.begin() as connection:
        before = resolve_for_task(
            connection,
            settings,
            settings.default_project_id,
            {
                "job_kind": "openai.plan",
                "agent_module_id": "codex_software_engineer_v1",
            },
        )
    assert "github.create_pr_sandbox" in {
        item["id"] for item in before["on_demand"]
    }

    disabled = client.post(
        "/v1/skills/github.create_pr_sandbox/disable", json=payload
    )
    assert disabled.status_code == 200, disabled.text
    with engine.begin() as connection:
        after = resolve_for_task(
            connection,
            settings,
            settings.default_project_id,
            {
                "job_kind": "openai.plan",
                "agent_module_id": "codex_software_engineer_v1",
            },
        )
    assert "github.create_pr_sandbox" not in {
        item["id"] for item in after["eager"] + after["on_demand"]
    }


def test_local_skill_and_mcp_bundle_routes_work_without_hosted_login(client):
    settings = get_settings()
    service = CapabilityBundleService(settings)
    skill = _custom_skill("local.api.bundle")
    skill_bundle = service.export_bundle(
        kind="skill", identifier=skill["id"], payload=skill
    )
    imported_skill = client.post(
        "/v1/skills/import",
        json={
            "project_id": settings.default_project_id,
            "bundle": skill_bundle,
            "allow_untrusted": True,
            "actor": "ceo",
        },
    )
    assert imported_skill.status_code == 201, imported_skill.text
    exported_skill = client.get(
        f"/v1/skills/{skill['id']}/export",
        params={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert exported_skill.status_code == 200, exported_skill.text
    assert exported_skill.json()["payload"] == skill

    mcp_payload = {
        "name": "test-api-bundle-mcp",
        "url": "http://127.0.0.1:9998/mcp",
        "transport": "http",
        "risk_tier": 1,
        "data_boundary": "internal",
        "description": "Test bundle server",
        "tool_risk_overrides": {"write": 2},
    }
    mcp_bundle = service.export_bundle(
        kind="mcp", identifier=mcp_payload["name"], payload=mcp_payload
    )
    imported_mcp = client.post(
        "/v1/mcp/servers/import",
        json={
            "project_id": settings.default_project_id,
            "bundle": mcp_bundle,
            "allow_untrusted": True,
            "actor": "ceo",
        },
    )
    assert imported_mcp.status_code == 201, imported_mcp.text
    exported_mcp = client.get(
        f"/v1/mcp/servers/{imported_mcp.json()['id']}/export",
        params={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert exported_mcp.status_code == 200, exported_mcp.text
    assert exported_mcp.json()["payload"] == mcp_payload


def test_rolespec_load_reports_stale_and_legacy_skill_references():
    role = SpecPackService(get_settings()).load_role_spec("cto")
    diagnostics = role["skill_reference_diagnostics"]
    assert diagnostics
    assert {item["status"] for item in diagnostics} <= {
        "unresolved",
        "legacy_alias",
    }
