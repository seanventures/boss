"""One rule says when a waiting timer, gate or "Run another workflow" step is over.

`advance_run` (which releases the wait) and `a_wait_is_over` (the product tick's question
"should I call it?") each held a copy of these conditions, one in Python and one in SQL. A wait
kind or a run state added to one and not the other leaves a run `waiting` for ever with no
error — the hang the management journey tests found. Both now read `_wait_outcome`; these
tests hold that rule to every state a gate or a child run can be in.
"""
from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services import workflows
from boss_engine.services.workflows import (
    TERMINAL_RUN_STATES,
    WAIT_BLOCK,
    WAIT_REFUSE,
    WAIT_RELEASE,
    _wait_outcome,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
#: Every state a workflow run can be in (`workflow_run_actions` names the live ones).
ALL_RUN_STATES = sorted(
    TERMINAL_RUN_STATES | {"queued", "validating", "running", "waiting", "paused", "blocked",
                           "compensating"}
)


def outcome(kind, *, run_after=None, decision=None, child=None):
    return _wait_outcome(kind, run_after=run_after, decision_status=decision,
                         child_status=child, now=NOW)


def test_a_timer_ends_when_it_is_due_and_not_before():
    assert outcome("timer", run_after=NOW) == WAIT_RELEASE
    assert outcome("timer", run_after=NOW - timedelta(seconds=1)) == WAIT_RELEASE
    assert outcome("timer", run_after=NOW + timedelta(seconds=1)) is None
    assert outcome("timer", run_after=None) is None


@pytest.mark.parametrize("status, expected", [
    ("approved", WAIT_RELEASE),
    ("rejected", WAIT_REFUSE),
    ("expired", WAIT_REFUSE),
    ("pending", None),
    ("delegated", None),
    (None, None),  # no decision opened yet
])
def test_a_gate_ends_when_a_person_answers_or_its_deadline_passes(status, expected):
    assert outcome("decision", decision=status) == expected


@pytest.mark.parametrize("status", ALL_RUN_STATES)
def test_a_parent_never_waits_on_a_child_run_that_is_over(status):
    got = outcome("subworkflow", child=status)
    if status == "completed":
        assert got == WAIT_RELEASE
    elif status in TERMINAL_RUN_STATES:
        # 'compensated' included: a run state added to TERMINAL_RUN_STATES blocks the parent.
        assert got == WAIT_BLOCK, f"a child {status!r} must not leave its parent waiting"
    else:
        assert got is None, f"a child still {status!r} is not over"


@pytest.mark.parametrize("kind", ["loop", "capability", "agent", "parallel"])
def test_other_waits_are_not_this_rules_to_end(kind):
    assert outcome(kind, run_after=NOW, decision="approved", child="completed") is None


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return _Rows(self.rows)


@pytest.mark.parametrize("step, over", [
    ({"step_kind": "decision", "decision_status": "expired"}, True),
    ({"step_kind": "subworkflow", "child_status": "cancelled"}, True),
    ({"step_kind": "subworkflow", "child_status": "running"}, False),
    ({"step_kind": "timer", "run_after": NOW + timedelta(days=1)}, False),
])
def test_the_ticks_question_is_the_same_rule(step, over, monkeypatch):
    monkeypatch.setattr(workflows, "now_utc", lambda: NOW)
    assert workflows.a_wait_is_over(_Connection([step]), "run-1") is over
