"""A workflow with a bank's outside-agent step can start (2026-10-31). Its captain is
{"external_agent": "ext_…"}, which readiness read as a role key and refused."""
from __future__ import annotations

from boss_engine.services import workflow_readiness


def test_an_outside_agent_captain_is_not_a_role_the_project_needs():
    spec = {"steps": [{"id": "bank_review", "kind": "external_agent",
                       "captain": {"external_agent": "ext_123"},
                       "contract": {"writes": [{"kind": "agent_decision", "required": True}]},
                       "next": []}]}
    keys = {item["key"] for item in workflow_readiness.requirements(spec)}
    assert not any(k.startswith("role:") for k in keys), keys
