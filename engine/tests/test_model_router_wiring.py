"""Every boundary reaches its model through the router (docs/product/MODEL_ROUTER.md).

A guard, read from the source: the governed pipeline routes stages only through the router's
StageRouter (no second copy of route_for_step / materialize_route), and every call site that
knows what its work is names its purpose — so a Smart route can pick its tier and "Why this
model" has a record. A new call site that forgets is caught here.
"""
from __future__ import annotations

import inspect
import re

from boss_engine.services import (
    agent_loop, discovery_service, governed_code_task_service, intent_router, meeting_service,
    skill_executors,
)


def test_the_build_pipeline_routes_only_through_the_router():
    source = inspect.getsource(governed_code_task_service)
    assert "StageRouter(" in source
    for legacy in ("materialize_route(", "route_for_step(", "apply_budget_pressure("):
        assert legacy not in source, f"{legacy} called outside model_router.stages"


def test_every_escalation_is_a_signal_on_the_ledger():
    source = inspect.getsource(governed_code_task_service)
    assert 'stage_router.signal("implement", "tests_failed")' in source
    assert "signal=signal" in source           # no_output / final_attempt at the boundary
    assert "settings.code_agent_strong_engine if escalate" not in source


def test_call_sites_that_know_their_work_name_it():
    for module in (agent_loop, meeting_service):
        calls = re.findall(r"project_chat_adapter\([^)]*\)", inspect.getsource(module),
                           re.DOTALL)
        assert calls and all("purpose" in c for c in calls), module.__name__
    skills = inspect.getsource(skill_executors)
    assert len(re.findall(r"project_chat_adapter\(", skills)) == skills.count(
        "purpose=_route_purpose(") == 4
    assert "purpose=purpose" in inspect.getsource(
        intent_router.IntentRouter._use_project_chat_model)
    assert "_research_model(" in inspect.getsource(discovery_service)
