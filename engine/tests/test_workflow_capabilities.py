"""A workflow step can do the things a delivery workflow is made of.

Before this, a step could call a skill, an integration or an agent group — and none of those
said "research it", "convene the panel", "build it", "ship it", "watch it". The five things a
delivery workflow consists of were exactly the five things a step could not be.

These pin the registry that closes it, the validation an operator sees while building, and the
one rule that configuration is not allowed to switch off.
"""

from __future__ import annotations

import pytest

from boss_engine.services import workflow_capabilities as caps
from boss_engine.services.workflows import WorkflowValidationError, validate_workflow_spec


def spec(steps, entry):
    return {"entry_step": entry, "steps": steps}


def cap_step(step_id, capability, inputs=None, approval=None, nxt=None):
    step = {
        "id": step_id,
        "kind": "capability",
        "binding": {"capability": capability, "inputs": inputs or {}},
        "next": nxt or [],
    }
    if approval is not None:
        step["approval"] = approval
    return step


# ── The registry ───────────────────────────────────────────────────────────────

def test_the_five_capabilities_a_delivery_workflow_needs_all_exist():
    """Research → meeting → build → deploy → monitor, from the original request."""
    for key in ("research", "meeting", "build", "deploy", "monitor"):
        assert key in caps.CAPABILITY_KEYS, key


def test_every_capability_declares_enough_to_be_executed_or_waited_on():
    """A capability with neither a tool nor a gate flag is a step that claims to do something
    and does nothing — the exact failure the whole registry exists to prevent."""
    for key, capability in caps.CAPABILITIES.items():
        assert capability.label and capability.summary, key
        assert capability.risk_tier >= 0, key
        if capability.tool is None and not capability.is_gate:
            # Allowed, but it must SAY it is a record rather than work. Asserted in the
            # execution test below.
            assert capability.produces or capability.optional_inputs, key


def test_the_tools_capabilities_dispatch_to_actually_exist():
    """The registry names tools; if one is missing the step fails at run time, deep inside a
    customer's build, with nothing useful to read."""
    from boss_engine.tools.builtin import build_default_registry

    available = {s.name for s in build_default_registry().specs()}
    for key, capability in caps.CAPABILITIES.items():
        if capability.tool:
            assert capability.tool in available, f"{key} -> missing tool {capability.tool}"


def test_the_palette_gives_a_builder_everything_it_needs():
    palette = caps.palette()
    assert len(palette) == len(caps.CAPABILITIES)
    entry = next(p for p in palette if p["key"] == "deploy")
    for field in ("label", "summary", "risk_tier", "required_inputs", "produces",
                  "requires_project", "typical_minutes", "is_gate", "approval_forced_manual"):
        assert field in entry, field
    # Deploy is above the floor, so the builder must show approval as fixed, not as a choice.
    assert entry["approval_forced_manual"] is True


# ── The risk floor ─────────────────────────────────────────────────────────────

def test_a_risky_capability_is_manual_no_matter_what_the_workflow_asks():
    """Deploying to production is not something a configuration file gets to decide was fine.

    This is the one rule in the whole feature that a customer cannot configure away.
    """
    assert caps.approval_mode_for("deploy", "auto") == "manual"
    assert caps.approval_mode_for("deploy", None) == "manual"
    assert caps.approval_mode_for("security_review", "auto") == "manual"


def test_an_ordinary_capability_honours_what_was_asked():
    assert caps.approval_mode_for("research", "auto") == "auto"
    assert caps.approval_mode_for("research", "manual") == "manual"
    assert caps.approval_mode_for("research", None) == "auto"


def test_an_unknown_approval_mode_is_refused():
    with pytest.raises(ValueError):
        caps.approval_mode_for("research", "sometimes")


def test_the_floor_is_a_property_of_the_capability_not_of_the_workflow():
    """Anything that changes the world in a way an operator would want to have agreed to first
    sits at or above the floor. If this list shrinks, someone has lowered a gate."""
    forced = {k for k, c in caps.CAPABILITIES.items()
              if c.risk_tier >= caps.MANUAL_APPROVAL_RISK_FLOOR}
    assert {"deploy", "spec_review", "ux_review", "security_review"} <= forced


# ── Validation the customer reads ──────────────────────────────────────────────

def test_a_well_formed_workflow_is_accepted_and_records_effective_approval():
    """The STORED plan says what will happen. A plan reading "auto" beside a step that will
    stop and wait is a plan nobody can trust."""
    out = validate_workflow_spec(spec([
        cap_step("m", "meeting", {"directive": "Build X"}, nxt=["d"]),
        cap_step("d", "deploy", {"run_id": "r"}),
    ], "m"))
    modes = {s["id"]: s["approval"]["mode"] for s in out["steps"]}
    assert modes == {"m": "auto", "d": "manual"}


def test_asking_for_auto_on_a_gated_step_is_refused_with_the_reason():
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow_spec(spec(
            [cap_step("d", "deploy", {"run_id": "r"}, approval={"mode": "auto"})], "d"))
    message = str(caught.value)
    assert "risk tier 2" in message
    assert "requires a person" in message


def test_a_missing_input_is_named_at_publish_time():
    """Not at 2am in the middle of a run."""
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow_spec(spec([cap_step("b", "build", {"title": "X"})], "b"))
    assert "needs 'instructions'" in str(caught.value)


def test_an_input_the_capability_does_not_take_is_named_rather_than_dropped():
    """A typo'd input silently ignored is a step that does something subtly different from
    what the customer wrote down."""
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow_spec(spec(
            [cap_step("b", "build", {"title": "X", "instructions": "do", "bogus": 1})], "b"))
    assert "does not take: bogus" in str(caught.value)


def test_an_unknown_capability_lists_the_ones_that_exist():
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow_spec(spec([cap_step("x", "teleport")], "x"))
    message = str(caught.value)
    assert "unknown capability 'teleport'" in message
    assert "build" in message and "deploy" in message


def test_every_problem_is_reported_at_once():
    """One error at a time makes a customer fix a workflow five times to publish it once."""
    problems = caps.validate_step(
        {"id": "b", "kind": "capability",
         "binding": {"capability": "build", "inputs": {"nope": 1}}})
    assert len(problems) >= 2
    assert any("title" in p for p in problems)
    assert any("nope" in p for p in problems)


# ── What the plan tells you before it runs ─────────────────────────────────────

def test_a_workflow_declares_what_the_project_must_already_have():
    """Said once, while the customer is looking at the workflow — not on the morning the build
    step finally runs and cannot find a repository."""
    needs = caps.project_requirements(spec([
        cap_step("b", "build", {"title": "X", "instructions": "do"}),
        cap_step("d", "deploy", {"run_id": "r"}),
    ], "b"))
    assert needs == {"repository", "deployment_target"}


def test_a_plan_can_be_read_for_how_long_it_takes():
    total = caps.estimate_minutes(spec([
        cap_step("r", "research", {"seed": "s"}),
        cap_step("m", "meeting", {"directive": "d"}),
    ], "r"))
    assert total == caps.get("research").typical_minutes + caps.get("meeting").typical_minutes


def test_the_existing_kinds_are_untouched():
    """The capability kind is ADDITIVE. BOSS's thirty-three definitions use these, and a
    regression here breaks the pipeline that governs every build in the system."""
    from boss_engine.services.workflows import WORKFLOW_KINDS

    assert {"task", "skill", "integration", "agent_group", "decision", "timer",
            "subworkflow", "condition", "parallel", "join", "bounded_loop"} <= WORKFLOW_KINDS
    assert "capability" in WORKFLOW_KINDS


def test_a_plain_task_step_still_validates_without_a_capability():
    out = validate_workflow_spec(spec([{"id": "t", "kind": "task", "binding": {}, "next": []}], "t"))
    assert out["steps"][0]["kind"] == "task"
    assert "approval" not in out["steps"][0]
