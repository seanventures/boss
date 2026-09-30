"""IntentRouter classify/deterministic routing for agent processes.

Public seam: IntentRouter.classify_message (and the capability it maps to).
A process/workflow/handoff authoring ask must not become a generic code task
or a multi-phase command. A narrow build stays create_code_task.
"""

from __future__ import annotations

from boss_engine.config import Settings
from boss_engine.services.capability_manifest import build_manifest
from boss_engine.services.intent_router import IntentRouter, _TOOL_SCHEMAS


def _classify(message: str):
    return IntentRouter(Settings()).classify_message(message)


class _EmptyResult:
    def mappings(self):
        return self

    def all(self):
        return []


class _EmptyConnection:
    def execute(self, *args, **kwargs):
        return _EmptyResult()


def test_process_authoring_classifies_before_multi_phase_and_build():
    name, arguments = _classify(
        "create a process where PRD is reviewed by architect then captain builds"
    )
    assert name == "create_agent_process"
    assert "text" in arguments
    assert "reviewed by architect" in arguments["text"]


def test_narrow_build_stays_a_code_task():
    name, arguments = _classify("build a settings page")
    assert name == "create_code_task"
    assert arguments["instructions"] == "build a settings page"


def test_generic_first_then_plan_is_not_a_process():
    name, _arguments = _classify(
        "First research auth providers and then implement the login page"
    )
    assert name == "propose_command"


def test_start_agent_process_classifies_from_prd():
    name, arguments = _classify(
        "start a process with this PRD: Users capture webhooks and replay them."
    )
    assert name == "start_agent_process"
    assert "prd" in arguments
    assert "webhook" in arguments["prd"].lower()


def test_handoff_workflow_authoring_is_create_agent_process():
    name, arguments = _classify(
        "author a workflow with handoffs: architect reviews the PRD, then captain builds"
    )
    assert name == "create_agent_process"
    assert arguments.get("text")


def test_agent_process_tools_are_registered():
    names = {item["function"]["name"] for item in _TOOL_SCHEMAS}
    assert "create_agent_process" in names
    assert "start_agent_process" in names
    create = next(
        item["function"] for item in _TOOL_SCHEMAS if item["function"]["name"] == "create_agent_process"
    )
    start = next(
        item["function"] for item in _TOOL_SCHEMAS if item["function"]["name"] == "start_agent_process"
    )
    assert "text" in create["parameters"]["properties"]
    assert "name" in create["parameters"]["properties"]
    assert "prd" in start["parameters"]["properties"]
    assert "process_key" in start["parameters"]["properties"]
    assert "simulated" in start["parameters"]["properties"]


def test_agent_process_capability_is_enabled_initiator():
    manifest = build_manifest(_EmptyConnection(), Settings(), "boss-dogfood")
    capability = manifest.get("agent_process")
    assert capability is not None
    assert capability.enabled is True
    assert capability.initiator_tool in {"create_agent_process", "start_agent_process"}
    assert manifest.enabled("agent_process")
