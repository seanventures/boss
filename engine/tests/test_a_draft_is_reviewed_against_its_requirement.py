"""A workflow designed from a requirement is reviewed against it before it publishes
(BOSSAPP-1361).

THE DESIGN. Asked to design API onboarding from a ticket ("every tenant config change is
validated, rejected with a report and a suggested fix when it does not conform, then deployed to
staging, tested, approved by a person, and promoted to production"), Ask BOSS published a design
that never rejected anything and whose contract test checked only /healthz — and said so only
after publishing. The authoring turn now maps every clause of the requirement to a step of the
draft or to an explicit gap, and the tool holds that map to the draft.
"""
from __future__ import annotations

from types import SimpleNamespace

from boss_engine.tools.builtin import build_default_registry, requirement_review

KAN_9 = ("Every tenant config change is validated (format, security, API guidelines), rejected "
         "with a report and a suggested fix when it does not conform, then deployed to staging, "
         "tested, approved by a person, and promoted to production.")
SPEC = {"steps": [{"id": i} for i in
                  ("validate", "staging", "contract_test", "approve", "production")]}


def test_a_clause_the_design_misses_must_be_said_as_a_gap():
    review = requirement_review(KAN_9, [
        {"clause": "validated (format, security, API guidelines)", "step": "validate"},
        {"clause": "rejected with a report and a suggested fix"},
        {"clause": "deployed to staging", "step": "staging"},
    ], SPEC)
    assert review["ok"] is False and "neither a step" in review["error"]

    review = requirement_review(KAN_9, [
        {"clause": "validated (format, security, API guidelines)", "step": "validate"},
        {"clause": "rejected with a report and a suggested fix",
         "gap": "no step stops the run on a non-conforming config"},
        {"clause": "deployed to staging", "step": "staging"},
        {"clause": "tested", "step": "contract_test"},
        {"clause": "approved by a person", "step": "approve"},
        {"clause": "promoted to production", "step": "production"},
    ], SPEC)
    assert review["checked"] is True and len(review["coverage"]) == 6
    assert review["gaps"] == ["rejected with a report and a suggested fix: no step stops the "
                              "run on a non-conforming config"]


def test_a_step_the_draft_does_not_have_is_refused():
    review = requirement_review(KAN_9, [{"clause": "rejected", "step": "reject"}], SPEC)
    assert review["ok"] is False and "'reject'" in review["error"]


def test_no_requirement_no_review():
    assert requirement_review("", None, SPEC) == {"checked": False}


def test_draft_workflow_refuses_a_requirement_without_its_review():
    out = build_default_registry().invoke(
        "draft_workflow", SimpleNamespace(engine=None, settings=None, begin=None),
        project_id="p", actor="usr_someone", operator_confirmed=True,
        args={"workflow_key": "api_onboarding", "name": "API onboarding",
              "steps": [{"id": "staging", "capability": "deploy"}], "requirement": KAN_9})
    assert out["ok"] is False and "coverage" in out["error"], out
