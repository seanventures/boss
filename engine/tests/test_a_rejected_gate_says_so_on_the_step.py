"""Pressing Reject blocked the run and left the gate saying "waiting".

`_block_run` updates `workflow_runs` and nothing else. So a rejected decision step kept
`status='waiting'` for ever, with no record that anybody had answered — the operator pressed
Reject, watched the gate go on asking the same question, and had no way to tell their decision
had landed. The run was blocked the whole time.

ALSO PINNED HERE: a duration that is always zero. `now()` in Postgres is the TRANSACTION's start
time and constant for its whole duration, so a step claimed and completed inside one tick got
the same value for `started_at` and `completed_at`. Every step in the console reported no time
at all.
"""
import inspect

from boss_engine.services import workflows


def test_a_rejected_gate_marks_the_step_as_well_as_the_run():
    source = inspect.getsource(workflows.advance_run) if hasattr(workflows, "advance_run") else ""
    if "_refuse_step(" not in source:
        source = inspect.getsource(workflows)
    assert "_refuse_step(" in source, "a rejection must be recorded on the step that asked"


def test_the_refusal_records_who_decided_and_what_they_said():
    body = inspect.getsource(workflows._refuse_step)
    assert "status='failed'" in body, "a step that did not pass must not stay 'waiting'"
    assert "completed_at=clock_timestamp()" in body
    assert "decision_id" in body and "outcome" in body, (
        "the answer has to be on the step, or nothing explains why the run stopped")
    # Only a waiting step: a rejection arriving twice must not overwrite a later attempt.
    assert "AND status='waiting'" in body


def test_a_duration_uses_the_wall_clock_not_the_transaction_clock():
    """`now()` is fixed for a transaction. A step claimed and finished in one tick measured 0s,
    so the run view had nothing to show."""
    source = inspect.getsource(workflows)
    assert "started_at=COALESCE(started_at, clock_timestamp())" in source
    assert "completed_at=now()" not in source, (
        "completed_at must not use the transaction clock, or every duration is zero")
    assert source.count("completed_at=clock_timestamp()") >= 10


def test_the_lease_still_uses_the_transaction_clock():
    """A lease is about the transaction's view of time; only durations need the wall clock.
    Changing both would have been a shotgun, not a fix."""
    claim = inspect.getsource(workflows.claim_step)
    assert "heartbeat_at=now()" in claim
