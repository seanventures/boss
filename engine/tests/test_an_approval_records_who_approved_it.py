"""An approval record must name the kind of approver it actually had.

BOSS auto-approves the PRD gate when BOSS_MEETING_PRD_AUTO_APPROVE is on. The code that does
it is explicit about the deal it is making:

    "This approves the decision rather than skipping it: the row exists, the PRD stays
     readable on the card, and the audit trail records `auto_approval` as the actor, so a
     reader can always tell which approvals a human made."

The decision row kept that promise. `_resume_answered_gates` then broke it, writing
`decided_by: "person"` onto the step -- unconditionally, for every approved decision -- and the
step is the record the run card shows and an auditor reads.

Seen on a live run (wfr_a1ac0748e42742d38256):

    decisions.approved_by            = auto_approval
    workflow_step_runs.evidence      = [{"kind": "approval", "decided_by": "person"}]

Nobody approved that spec. For a product whose claim is that every change traces to whoever
approved it, an approval naming the wrong kind of approver is the worst defect available --
it does not merely fail to prove the thing, it asserts the opposite.
"""

from __future__ import annotations

import re
from pathlib import Path

from boss_engine.services.decisions import SYSTEM_ACTOR

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "boss_engine/services/workflow_capability_runner.py"
).read_text(encoding="utf-8")


def _resume_answered_gates_source() -> str:
    """The body of the one function this is about, so an unrelated 'person' elsewhere in a
    4,000-line module cannot fail this test or, worse, make it pass."""
    start = SOURCE.index("def _resume_answered_gates(")
    end = SOURCE.index("\ndef ", start + 1)
    return SOURCE[start:end]


def test_the_approver_is_read_from_the_decision_not_assumed():
    body = _resume_answered_gates_source()
    assert "d.approved_by" in body, (
        "the gate must SELECT who approved the decision; without it the only thing left to "
        "write is a guess"
    )
    assert 'row["decision_approved_by"]' in body


def test_no_hardcoded_person_survives_in_the_gate():
    """The specific literal that caused it. If this reappears, the audit trail is lying
    again."""
    body = _resume_answered_gates_source()
    assert '"decided_by": "person"' not in body
    assert '"approved_by": "person"' not in body


def test_an_automatic_approval_is_marked_as_automatic():
    """A reader scanning evidence should not have to know that `auto_approval` is a machine.
    The record carries the fact outright."""
    body = _resume_answered_gates_source()
    assert "decided_automatically" in body
    assert '"automatic": decided_automatically' in body
    assert '"kind": "auto_approval" if decided_automatically else "approval"' in body


def test_automatic_is_decided_by_comparing_against_the_one_constant():
    """`auto_approval` is defined once, in decisions.py. Comparing against a re-typed literal
    is how the two drift apart."""
    body = _resume_answered_gates_source()
    assert "DECISION_SYSTEM_ACTOR" in body
    assert 'approved_by == "auto_approval"' not in body, "compare against the constant"
    assert SYSTEM_ACTOR == "auto_approval"


def test_a_missing_approver_is_called_unknown_not_a_person():
    """A decision row with no approver is a data problem. Reporting it as a person turns a
    data problem into a false attestation."""
    body = _resume_answered_gates_source()
    assert '"unknown"' in body
    assert re.search(r'approved_by\s*=\s*str\(row\["decision_approved_by"\]', body)


def test_the_timeout_path_was_already_honest_and_stays_that_way():
    """The SLA auto-approval always recorded `policy` with the SLA beside it. That is the
    standard the gate path now meets; this pins it so a later tidy-up does not level them down
    to a shared, vaguer record."""
    assert '"kind": "auto_approval",' in SOURCE
    assert '"decided_by": "policy",' in SOURCE
    assert '"sla_hours": approval.get("sla_hours"),' in SOURCE
