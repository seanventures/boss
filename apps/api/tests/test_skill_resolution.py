from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services import agent_loop
from apps.api.app.services.entitlements import EntitlementBrokerService
from apps.api.app.services.skill_registry import register_skill, resolve_for_task


def _usage() -> dict:
    return {
        "model": "test-agent",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }


class CallingAdapter:
    def __init__(self, target: str, arguments: dict):
        self.target = target
        self.arguments = arguments
        self.calls = 0
        self.seen_tools: list[dict] = []

    def create_chat_completion(self, messages, tools=None):
        self.seen_tools = tools or self.seen_tools
        if self.calls == 0:
            names = {
                item["function"]["name"]
                for item in self.seen_tools
            }
            assert self.target in names
            self.calls += 1
            raw = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_skill",
                        "type": "function",
                        "function": {
                            "name": self.target,
                            "arguments": json.dumps(self.arguments),
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
                            "id": "call_skill",
                            "name": self.target,
                            "arguments": json.dumps(self.arguments),
                        }
                    ],
                },
                _usage(),
            )
        self.calls += 1
        raw = {"role": "assistant", "content": "done"}
        return ({"raw": raw, "content": "done", "tool_calls": []}, _usage())


def test_project_skill_catalog_is_edition_filtered_and_stateful(client, engine):
    settings = get_settings()
    project_id = settings.default_project_id

    global_response = client.get("/v1/skills")
    assert global_response.status_code == 200
    global_ids = {item["id"] for item in global_response.json()["items"]}
    assert "deployment.health.check" in global_ids
    # An enterprise-only skill. (deployment.health.check is in Community since the Kit's Watch
    # health ability — skills-mcp spec E12 — so it no longer proves the edition filter.)
    assert "providerops.drift.report" in global_ids

    with engine.begin() as connection:
        register_skill(
            connection,
            project_id,
            "github.create_pr_sandbox",
            source="tenant",
            enabled=True,
            on_demand=True,
            actor="test",
        )

    response = client.get("/v1/skills", params={"project_id": project_id})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    project_ids = {item["id"] for item in items}
    assert "github.create_pr_sandbox" in project_ids
    assert "providerops.drift.report" not in project_ids
    sandbox = next(
        item for item in items if item["id"] == "github.create_pr_sandbox"
    )
    assert sandbox["source"] == "tenant"
    assert sandbox["system"] is False
    assert sandbox["enabled"] is True
    assert sandbox["on_demand"] is True

    enterprise = EntitlementBrokerService(
        settings.model_copy(update={"boss_edition": "enterprise_self_hosted"})
    )
    with engine.begin() as connection:
        enterprise_ids = {
            item["id"]
            for item in enterprise.list_skills(project_id, connection=connection)
        }
    assert "deployment.health.check" in enterprise_ids


def test_task_resolver_unions_module_and_system_skills_and_keeps_on_demand_discoverable(
    engine,
):
    settings = get_settings()
    project_id = settings.default_project_id
    with engine.begin() as connection:
        register_skill(
            connection,
            project_id,
            "github.merge_pr_real",
            source="tenant",
            enabled=True,
            actor="test",
        )
        community = resolve_for_task(
            connection,
            settings,
            project_id,
            {
                "job_kind": "openai.plan",
                "agent_module_id": "codex_software_engineer_v1",
            },
        )
        enterprise = resolve_for_task(
            connection,
            settings.model_copy(update={"boss_edition": "enterprise_self_hosted"}),
            project_id,
            {
                "job_kind": "agent.deployment_manager.deployment_review",
                "agent_module_id": "deployment_manager_v1",
            },
        )
        cto = resolve_for_task(
            connection,
            settings,
            project_id,
            {
                "job_kind": "agent.cto.implementation_plan",
                "agent_module_id": "cto_planner_v1",
            },
        )

    assert {item["id"] for item in community["eager"]} == {
        "model.generate_code"
    }
    assert community["eager"][0]["caller"] == "software_factory"
    assert {item["id"] for item in community["on_demand"]} == {
        "github.create_pr_sandbox"
    }
    assert "github.merge_pr_real" not in {
        item["id"] for item in cto["eager"] + cto["on_demand"]
    }
    enterprise_by_id = {
        item["id"]: item
        for item in enterprise["eager"]
    }
    assert "deployment.health.check" in enterprise_by_id
    assert "system" in enterprise_by_id["deployment.health.check"]["resolved_from"]
    assert "deployment.run.plan" in enterprise_by_id
    assert "module" in enterprise_by_id["deployment.run.plan"]["resolved_from"]


def test_agent_loop_injects_system_skill_through_runtime_without_namespace_collisions(
    engine,
    monkeypatch,
):
    settings = get_settings().model_copy(
        update={"boss_edition": "enterprise_self_hosted"}
    )
    adapter = CallingAdapter(
        "skill_deployment_health_check",
        {"deployment_run_id": "dep_test"},
    )
    invocations: list[dict] = []

    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: adapter)
    monkeypatch.setattr(
        agent_loop,
        "list_mcp_servers",
        lambda *_args, **_kwargs: [
            {"id": "mcp_one", "name": "same", "url": "http://one"},
            {"id": "mcp_two", "name": "same", "url": "http://two"},
        ],
    )
    monkeypatch.setattr(
        agent_loop.MCPClient,
        "list_tools",
        lambda *_args, **_kwargs: [
            {
                "name": "inspect",
                "description": "Inspect",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
    )

    def fake_invoke(
        _service,
        _connection,
        project_id,
        skill_id,
        caller,
        inputs,
        **kwargs,
    ):
        invocations.append(
            {
                "project_id": project_id,
                "skill_id": skill_id,
                "caller": caller,
                "inputs": inputs,
                **kwargs,
            }
        )
        return {"status": "succeeded", "result": {"health_status": "healthy"}}

    monkeypatch.setattr(agent_loop.SkillRuntimeService, "invoke", fake_invoke)

    with engine.begin() as connection:
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

    assert result["status"] == "completed"
    assert invocations[0]["skill_id"] == "deployment.health.check"
    assert invocations[0]["caller"] == "deployment_manager"
    assert invocations[0]["mock"] is False
    assert invocations[0]["executor_context"].created_by_job_id is None
    names = [item["function"]["name"] for item in adapter.seen_tools]
    assert len(names) == len(set(names))
    assert "skill_deployment_health_check" in names
    assert len([name for name in names if name.startswith("mcp_same_inspect")]) == 2
    assert "get_project_status" in names


def test_agent_loop_defers_high_risk_skill_to_blocking_decision(engine, monkeypatch):
    settings = get_settings().model_copy(
        update={
            "boss_edition": "enterprise_self_hosted",
            "agent_autonomous_risk_max": 1,
        }
    )
    adapter = CallingAdapter(
        "skill_deployment_run_plan",
        {
            "deployment_target_id": "target_test",
            "release_packet_artifact_id": "artifact_test",
            "environment": "staging",
        },
    )
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: adapter)
    monkeypatch.setattr(
        agent_loop,
        "list_mcp_servers",
        lambda *_args, **_kwargs: [],
    )

    with engine.begin() as connection:
        result = agent_loop.run_agent_loop(
            connection,
            settings,
            settings.default_project_id,
            "system",
            "Plan the deployment",
            max_steps=1,
            finalize_ctx={
                "job_kind": "agent.deployment_manager.deployment_review",
                "module_id": "deployment_manager_v1",
            },
        )
        run = agent_loop.load_agent_run(connection, result["run_id"])

    assert result["status"] == "suspended"
    assert run["status"] == "awaiting_approval"
    assert run["pending"]["kind"] == "skill"
    assert run["pending"]["skill_id"] == "deployment.run.plan"
    assert run["pending"]["caller"] == "deployment_manager"


def test_resume_agent_run_requires_approved_decision(engine, monkeypatch):
    """Concrete trigger: agent defers a high-risk skill → Decision stays pending →
    POST /v1/agent-runs/{id}/resume (or direct resume_agent_run) must NOT execute the
    tool. Only an approved Decision unlocks resume — otherwise the approval gate is
    a no-op for anyone who knows the run_id."""
    settings = get_settings().model_copy(
        update={
            "boss_edition": "enterprise_self_hosted",
            "agent_autonomous_risk_max": 1,
        }
    )
    adapter = CallingAdapter(
        "skill_deployment_run_plan",
        {
            "deployment_target_id": "target_test",
            "release_packet_artifact_id": "artifact_test",
            "environment": "staging",
        },
    )
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: adapter)
    monkeypatch.setattr(
        agent_loop,
        "list_mcp_servers",
        lambda *_args, **_kwargs: [],
    )
    invocations: list[dict] = []

    def fake_invoke(self, connection, project_id, skill_id, *args, **kwargs):
        invocations.append({"skill_id": skill_id, "project_id": project_id})
        return {"status": "ok", "skill_id": skill_id, "mock": True}

    monkeypatch.setattr(agent_loop.SkillRuntimeService, "invoke", fake_invoke)

    with engine.begin() as connection:
        suspended = agent_loop.run_agent_loop(
            connection,
            settings,
            settings.default_project_id,
            "system",
            "Plan the deployment",
            max_steps=1,
            finalize_ctx={
                "job_kind": "agent.deployment_manager.deployment_review",
                "module_id": "deployment_manager_v1",
            },
        )
        run_id = suspended["run_id"]
        decision_id = suspended["decision_id"]

        denied = agent_loop.resume_agent_run(connection, settings, run_id)
        assert denied["status"] == "denied"
        assert denied["decision_id"] == decision_id
        assert "not approved" in denied["reason"]
        assert invocations == []

        still = agent_loop.load_agent_run(connection, run_id)
        assert still["status"] == "awaiting_approval"
        assert still["pending"]["skill_id"] == "deployment.run.plan"

        connection.execute(
            text(
                "UPDATE decisions SET status = 'approved', approved_by = 'ceo', "
                "approved_at = now() WHERE id = :id"
            ),
            {"id": decision_id},
        )
        # After approval, resume continues the agent loop; CallingAdapter's second
        # create_chat_completion returns a final "done" so the run can complete.
        resumed = agent_loop.resume_agent_run(connection, settings, run_id)

    assert any(item["skill_id"] == "deployment.run.plan" for item in invocations)
    assert resumed.get("status") in {"completed", "suspended", "running"}
