"""A build's plan can be approved by the person's delegate — but never one that touches identity,
security, secrets or permissions, and never a deploy.

Operator, 2026-11-03: "I'm tired of approving.. I'd have approved at least 20-25 tasks, please do it
yourself". Plan approvals ("Approve the plan for …", action resume_code_task) were kind `general`
and so never delegable: every build in a sensitive area waited for the one person who could press
Approve. They are now the delegable kind `build_plan`; the hard limits still hold.
"""
from __future__ import annotations

import pytest

from boss_engine.identity import approval_delegations as ad


def _plan(sensitive=("deploy",), **rec):
    return {"project_id": "boss-platform", "risk_tier": 3,
            "recommendation": {"action": "resume_code_task", "run_id": "crun_x",
                               "plan": "**Files to add** …", "sensitive": list(sensitive), **rec}}


def test_build_plans_are_a_delegable_kind_with_a_plain_label():
    assert "build_plan" in ad.DELEGABLE_KINDS
    assert "Build plans" in ad.KIND_LABELS["build_plan"]


@pytest.mark.parametrize("sensitive", [("deploy",), ("payment",), ("infra",), ()])
def test_a_plan_is_a_build_plan_and_not_hard_limited(sensitive):
    row = _plan(sensitive)
    assert ad.kind_for(row) == "build_plan"
    assert ad.hard_limit(None, row) is None


@pytest.mark.parametrize("area", ["identity", "security", "secrets", "credential", "permission", "auth"])
def test_a_plan_touching_identity_or_security_stays_the_persons(area):
    limit = ad.hard_limit(None, _plan(("deploy", area)))
    assert limit is not None and limit[0] == "governance_never_delegated"
    assert area in limit[1]


def test_a_resume_without_a_plan_is_not_a_build_plan():
    row = {"project_id": "p", "recommendation": {"action": "resume_code_task", "run_id": "crun_x"}}
    assert ad.kind_for(row) is None


def test_a_deploy_is_still_never_delegated():
    row = {"project_id": "p", "recommendation": {"action": "deploy_app", "capability": "deploy"}}
    assert ad.hard_limit(None, row)[0] == "deploy_never_delegated"
