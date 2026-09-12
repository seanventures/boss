"""Every workflow step has a named SME who answers for it.

"Instead of Ask BOSS about this step we should have a captain assigned who can answer those
questions … let's make sure we are assigning a proper Agent SME for each step."

The role-addressed chat path already existed and is strictly better than asking BOSS in general:
it reads the role's own definition and the positions that role has actually taken on this
project. What it never had was anything saying that a deploy belongs to the Deployment Manager
and a security review to the Security Reviewer.
"""
from boss_engine.services.workflow_capabilities import CAPABILITIES, palette


#: The roles this project actually has registered. A captain naming anything else would address
#: the dock to a role with no definition and no record — worse than asking BOSS in general.
REGISTERED = {
    "adversarial_reviewer", "cao", "chief_architect", "coo", "cto", "deployment_manager",
    "design_critic", "engineering_captain", "finops_sentinel", "infra_steward",
    "integration_validator", "metatron", "model_spend_analyst", "pm", "product_manager",
    "program_manager", "providerops_manager", "qa", "release_manager", "resource_janitor",
    "secrets_steward", "security_reviewer", "software_engineer", "ux_engineer", "vp_agents",
    "vp_product",
}


def test_no_step_is_without_an_owner():
    orphans = sorted(k for k, c in CAPABILITIES.items() if not c.captain)
    assert not orphans, f"these steps have nobody to ask: {orphans}"


def test_every_captain_is_a_registered_role():
    unknown = sorted({c.captain for c in CAPABILITIES.values()} - REGISTERED)
    assert not unknown, f"not roles this workforce has: {unknown}"


def test_the_assignment_is_by_subject_matter():
    """One captain may hold several steps — that says something true about the workforce — but
    the lens has to match the work."""
    assert CAPABILITIES["security_review"].captain == "security_reviewer"
    assert CAPABILITIES["ux_review"].captain == "ux_engineer"
    assert CAPABILITIES["deploy"].captain == "deployment_manager"
    assert CAPABILITIES["cost_check"].captain == "finops_sentinel"
    assert CAPABILITIES["test"].captain == "qa"
    assert CAPABILITIES["monitor"].captain == "infra_steward"


def test_a_repeat_of_the_same_work_keeps_the_same_hands():
    assert CAPABILITIES["redeploy"].captain == CAPABILITIES["deploy"].captain
    assert CAPABILITIES["retry_build"].captain == CAPABILITIES["build"].captain


def test_it_reaches_the_console_through_the_capability():
    """Not a second copy in the console, which would drift the first time a step was added."""
    entries = {c["key"]: c for c in palette()}
    assert entries["deploy"]["captain"] == "deployment_manager"
    assert all(c["captain"] for c in entries.values())
