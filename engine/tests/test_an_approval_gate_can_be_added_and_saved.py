"""Dropping an "Approval gate" on the canvas and saving was refused, with nothing to fix.

`validate_workflow_spec` required `question` on a decision step. The palette's `configure` list for
that block offered one field: `approver_role`. So the builder could not collect a question, the
save was refused every time, and the operator reported the step as simply not working — after
building the workflow they wanted and losing it on save.

A field the validator demands has to be a field the palette offers. This pins both ends.
"""
import pytest

from boss_engine.services import workflows
from boss_engine.services.workflow_capabilities import (
    CONTROL_BLOCKS, HUMAN_ROLES, REGISTERED_ROLES, validate_captain,
)


def _gate_block() -> dict:
    return next(b for b in CONTROL_BLOCKS if b["kind"] == "decision")


def _spec(gate: dict) -> dict:
    return {
        "entry_step": "work",
        "steps": [
            {"id": "work", "kind": "capability",
             "binding": {"agent_work": True,
                         "inputs": {"instructions": {"from_input": "instructions"}}},
             "captain": "video_editor", "approval": {"mode": "auto"}, "next": ["gate"]},
            {**gate, "id": "gate", "kind": "decision", "next": []},
        ],
    }


def test_the_palette_offers_every_field_the_validator_demands():
    """The defect, stated once: the two lists disagreed."""
    fields = {c["field"] for c in _gate_block()["configure"]}
    assert "question" in fields, (
        "the validator requires a question and the palette never asked for one, so the step "
        "could be placed and never saved")
    assert "guidance" in fields
    assert "approver_role" in fields


def test_a_gate_with_a_question_and_guidance_saves():
    spec = _spec({
        "question": "Approve this short before it is published?",
        "guidance": "Watch it end to end. Check the framing and that the badge is still there.",
        "approval": {"mode": "manual", "approver_role": "human"},
    })
    normalised = workflows.validate_workflow_spec(spec)
    gate = next(s for s in normalised["steps"] if s["id"] == "gate")
    assert gate["question"] == "Approve this short before it is published?"
    assert "badge is still there" in gate["guidance"]


def test_a_gate_with_no_question_is_still_saveable():
    """It is WANTED, not required. Refusing the save taught the operator nothing — the gate
    gets a composed question when it opens, from the workflow and the step it follows."""
    normalised = workflows.validate_workflow_spec(_spec({"approval": {"mode": "manual"}}))
    gate = next(s for s in normalised["steps"] if s["id"] == "gate")
    assert gate["question"] == ""
    assert gate["guidance"] == ""


def test_a_gate_defaults_to_a_person_approving():
    """The only control on an approval step used to be a list of AI roles, so naming who
    approves meant naming a machine — on the step whose purpose is that one does not decide."""
    assert _gate_block()["defaults"]["approver_role"] == "human"
    assert "human" in REGISTERED_ROLES
    assert HUMAN_ROLES == {"human"}
    # REWRITTEN: the step is now a gate (`kind: decision`). `human` was accepted on ANY step,
    # and a build step naming it then ran as actor 'human'; only a gate may name a person.
    assert validate_captain({"id": "gate", "kind": "decision", "captain": "human"}) == []
    assert validate_captain({"id": "gate", "kind": "decision", "captain": "human"},
                            roster=["video_editor"]) == [], "no project has a role row for a person"


def test_naming_a_role_that_does_not_exist_is_still_refused():
    """Adding `human` must not turn the captain check into a rubber stamp.

    REWRITTEN to pass the project's agents: the check is against the PROJECT, not a constant."""
    assert validate_captain({"id": "gate", "kind": "decision", "captain": "the_ceos_dog"},
                            roster=["video_editor", "media_publisher"])
