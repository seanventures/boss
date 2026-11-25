"""A tool call naming an argument the tool does not take is refused — never a success that
silently did less (BOSSAPP-1361).

THE RUN. Ask BOSS staged "fix both targets' health checks" as update_deployment_target with
`health_check_path`; the tool takes `health_path`. The argument was dropped, the tool found
nothing to change, and the button reported SUCCEEDED (with `ignored_args` nobody read). The
health checks stayed wrong while the conversation said they were fixed.
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from boss_engine.tools.registry import ToolRegistry

from boss_engine.tools.builtin import build_default_registry


def test_update_deployment_target_with_a_wrong_argument_name_runs_nothing():
    ran = []
    reg = ToolRegistry()
    reg.register(dataclasses.replace(
        build_default_registry().get("update_deployment_target"),
        handler=lambda *a, **k: ran.append(1) or {"status": "unchanged"}))
    out = reg.invoke("update_deployment_target", SimpleNamespace(engine=None, settings=None),
                     project_id="p", actor="usr_someone", operator_confirmed=True,
                     args={"target_id": "deptgt_x", "health_check_path": "/healthz"})
    assert not ran
    assert out["ok"] is False and out["error"] == "unknown_arguments"
    assert out["unknown"] == ["health_check_path"]
    assert "health_path" in out["takes"] and "nothing ran" in out["message"]


def test_every_tool_refuses_the_same_way():
    """One rule at the door, not per tool: any registered tool given a made-up argument."""
    reg = build_default_registry()
    for name in ("list_skills", "describe_workflow", "draft_workflow", "start_workflow"):
        spec = reg.get(name)
        if spec.handler_checks_extra_args:
            continue    # its valid arguments include the target's own schema (start_workflow)
        out = reg.invoke(name, SimpleNamespace(engine=None, settings=None), project_id="p",
                         actor="usr_someone", operator_confirmed=True,
                         args={"definitely_not_an_argument": 1})
        assert out.get("error") == "unknown_arguments", (name, out)


def test_an_agent_steps_tool_calls_refuse_the_same_way(monkeypatch):
    """The agent-step doors (a baseline bundle, repo.read) call handlers directly, not through
    registry.invoke: the same rule holds there, before the handler runs."""
    import pytest

    from boss_engine.services import agent_baseline, kit_connections, skill_executors
    from boss_engine.services.agent_baseline import BUNDLE_TOOL_KEY

    context = SimpleNamespace(settings=None, project_id="p", actor="agent",
                              connection=SimpleNamespace(engine=None))
    with pytest.raises(ValueError, match="does not take 'filter_by'"):
        agent_baseline.run_bundled_tool(context, "boss.fundamentals",
                                        {BUNDLE_TOOL_KEY: "list_skills", "filter_by": "x"})
    monkeypatch.setattr(kit_connections, "github_state", lambda *a, **k: {"repo": "acme/app"})
    with pytest.raises(ValueError, match="does not take 'branch_name'"):
        skill_executors.execute_repo_read(context, {"tool": "read_code", "path": "README.md",
                                                    "branch_name": "main"})
