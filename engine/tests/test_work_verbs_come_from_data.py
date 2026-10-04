"""The verb every agent surface says comes from data, never from a label (spec §4.5).

Live's lanes, Activity's river, presence lines and the workforce card all say what an agent is
doing with one word from `agent_acts.work_verb`. It reads the act's status, kind, build stage
and its step's capability family (workflow_capabilities.Capability.family). It never reads the
label: a label is a name somebody typed, and a build called "deploy the fix" is still coding.
"""
from __future__ import annotations

import pytest

from boss_engine.services import agent_acts
from boss_engine.services.agent_acts import STAGE_PHASES, work_verb
from boss_engine.services.workflow_capabilities import CAPABILITIES

TABLE = {
    "spec": "Planning", "plan": "Planning", "review_plan": "Planning", "standards": "Planning",
    "design": "Designing",
    "implement": "Coding", "repair": "Coding", "solo": "Coding", "guest": "Coding",
    "review_code": "Reviewing", "consistency_check": "Reviewing", "security_check": "Reviewing",
    "write_tests": "Testing", "check_tests": "Testing", "run_all_tests": "Testing",
}


@pytest.mark.parametrize("stage", sorted(STAGE_PHASES))
def test_each_build_stage_says_its_verb(stage):
    act = {"kind": "stage", "status": "active", "step_id": stage}
    assert work_verb(act) == TABLE[stage]


def test_every_pipeline_stage_is_in_the_table():
    assert set(STAGE_PHASES) == set(TABLE)


def test_an_unknown_stage_or_kind_is_just_working():
    assert work_verb({"kind": "stage", "status": "active", "step_id": "quality"}) == "Working"
    assert work_verb({"kind": "handoff", "status": "active"}) == "Working"
    assert work_verb({"kind": "step", "status": "active"}) == "Working"


def test_the_verb_never_comes_from_the_label():
    act = {"kind": "stage", "status": "active", "step_id": "implement",
           "label": "Deploy the release · Ship it #182"}
    assert work_verb(act) == "Coding"
    step = {"kind": "step", "status": "active", "label": "Deploy · Ship it"}
    assert work_verb(step) == "Working"
    assert work_verb(step, capability="report") == "Working"


@pytest.mark.parametrize("capability,verb", [
    ("deploy", "Deploying"), ("preview", "Deploying"), ("redeploy", "Deploying"),
    ("teardown_preview", "Deploying"), ("social_publish", "Publishing"),
    ("video_edit", "Publishing"), ("monitor", "Monitoring"), ("research", "Researching"),
])
def test_a_step_says_its_capability_family(capability, verb):
    assert CAPABILITIES[capability].family
    assert work_verb({"kind": "step", "status": "active"}, capability=capability) == verb


def test_a_capability_with_no_family_is_working():
    plain = [k for k, c in CAPABILITIES.items() if not c.family]
    assert plain, "every capability has a family; this test would prove nothing"
    for key in plain:
        assert work_verb({"kind": "step", "status": "active"}, capability=key) == "Working"
    assert work_verb({"kind": "step", "status": "active"}, capability="no_such") == "Working"


def test_status_and_kind_come_first():
    assert work_verb({"kind": "stage", "status": "awaiting_input",
                      "step_id": "implement"}) == "Needs you"
    assert work_verb({"kind": "step", "status": "error"}, capability="deploy") == "Stuck"
    assert work_verb({"kind": "step", "status": "stale"}) == "Stuck"
    assert work_verb({"kind": "approval", "status": "complete"}) == "Approved"
    assert work_verb({"kind": "seat", "status": "active"}) == "In a panel"
    assert work_verb({"kind": "chat", "status": "active"}) == "Talking"
    assert work_verb({"kind": "run", "status": "active"}, run_status="blocked") == "Blocked"
    assert work_verb({"kind": "step", "status": "active"}, capability="deploy",
                     waiting=True) == "Waiting"


def test_every_verb_it_can_say_is_listed():
    seen = {work_verb({"kind": "stage", "status": "active", "step_id": s}) for s in STAGE_PHASES}
    seen |= {work_verb({"kind": "step", "status": "active"}, capability=c) for c in CAPABILITIES}
    assert seen <= agent_acts.VERBS
