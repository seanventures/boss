"""Built-in read-only probe steps (contract_test, smoke_test) run under integration_validator
(observability:read, delegable), while non-delegable steps fall back to run_actor (BOSSAPP-1411).
"""
from __future__ import annotations

from boss_engine.services import step_agents


def _step(capability, captain="integration_validator"):
    return {"id": "s", "kind": "capability", "captain": captain,
            "binding": {"capability": capability, "inputs": {}}}


def test_contract_and_smoke_tests_use_delegable_read_permission():
    assert step_agents.person_only_permission(_step("contract_test")) is None
    assert step_agents.person_only_permission(_step("smoke_test")) is None
    assert step_agents.person_only_permission(_step("config_deploy")) is None
    assert step_agents.person_only_permission(
        {"kind": "capability", "binding": {"agent_work": True, "capability": "contract_test"}}) is None


def test_a_named_agent_on_a_read_probe_step_runs_as_the_named_agent():
    record = step_agents.resolve(None, None, "p", _step("contract_test"), actor="usr_owner")
    assert record["agent"] == "integration_validator" and record["how"] != "run_actor"
    smoke = step_agents.resolve(None, None, "p", _step("smoke_test"), actor="usr_owner")
    assert smoke["agent"] == "integration_validator" and smoke["how"] != "run_actor"
