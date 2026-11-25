"""A BUILT-IN STEP NO AGENT MAY RUN RUNS AS THE PERSON WHO STARTED THE RUN (laptop 2026-11-21).

Northgate's API onboarding named integration_validator — the catalogue's own captain for it —
on its Contract test step. contract_test (and smoke_test's http_probe) need observability:manage,
which is not delegable: every attempt was refused "only a person can hold, never an agent",
Shepherd retried it twice and filed a self-fix. Naming an agent there can only fail the step.
"""
from __future__ import annotations

from boss_engine.services import step_agents


def _step(capability, captain="integration_validator"):
    return {"id": "s", "kind": "capability", "captain": captain,
            "binding": {"capability": capability, "inputs": {}}}


def test_contract_and_smoke_tests_need_a_person_only_permission():
    assert step_agents.person_only_permission(_step("contract_test"))[1] == "observability:manage"
    assert step_agents.person_only_permission(_step("smoke_test"))[1] == "observability:manage"
    assert step_agents.person_only_permission(_step("config_deploy")) is None
    assert step_agents.person_only_permission(
        {"kind": "capability", "binding": {"agent_work": True, "capability": "contract_test"}}) is None


def test_a_named_agent_on_a_person_only_step_hands_it_to_the_run_s_person():
    record = step_agents.resolve(None, None, "p", _step("contract_test"), actor="usr_owner")
    assert record["agent"] == "usr_owner" and record["how"] == "run_actor"
    assert "only a person can hold" in record["why"] and "integration_validator" in record["why"]
