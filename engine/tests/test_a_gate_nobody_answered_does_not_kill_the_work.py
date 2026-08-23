"""A deadline nobody met must not also destroy the work.

Measured on the live project: **127 of 165 decisions (77%) expired**, 22 were approved, 10 were
pending, 6 rejected — and every one of the 165 had `default_action: hold`.

"Hold" means DO NOTHING. So expiring a hold-default decision does not apply a safe default; it
removes the operator's ability to answer and strands the run forever. The refusal even said "the
safe default was applied", which was false, and the expiry event reported
`safe_default_applied: hold`, which reads as "handled safely" when the truth is "nobody answered
and the work is stuck".

This file also locks down a regression I introduced EARLIER THE SAME DAY. Including `blocked` in
`sweep_abandoned_runs` was justified as "approving a decision on a run whose job is gone resumes
nothing". That premise was wrong: blocking on a plan gate ENDS the job by design (the handler
returns `done=True`), and approving CREATES a new `code.task` job. So a run waiting for a person
looked exactly like an orphan and was swept away after 20 minutes — killing the very thing the
operator was about to approve.
"""

import inspect
import re
from pathlib import Path

from boss_engine.services import decisions, shepherd_service
from boss_engine.services.shepherd_service import (
    _TABLES_WITH_DECISIONS,
    _answerable_decision_clause,
)


# --------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------- #
def _resolve_src() -> str:
    return inspect.getsource(decisions.DecisionService.record_outcome) \
        if hasattr(decisions.DecisionService, "record_outcome") else _find_resolver()


def _find_resolver() -> str:
    for name, obj in inspect.getmembers(decisions.DecisionService, inspect.isfunction):
        src = inspect.getsource(obj)
        if "InvalidDecisionTransitionError" in src and "approve" in src:
            return src
    raise AssertionError("no decision-resolution method found")


def test_an_expired_hold_default_decision_can_still_be_answered():
    src = _find_resolver()
    assert 'reopenable = safe_default == "hold"' in src
    assert 'current_status == "expired" and reopenable' in src


def test_reopening_is_recorded_rather_than_silent():
    """A decision that changes state without an event is one nobody can audit."""
    src = _find_resolver()
    assert '"decision.reopened"' in src


def test_a_real_safe_default_still_refuses():
    """If the default was anything other than hold, expiry DID apply something, and silently
    reversing it would be a worse bug than the one being fixed."""
    src = _find_resolver()
    assert "and not reopenable" in src


def test_the_refusal_message_names_the_default_it_applied():
    """It used to claim 'the safe default was applied' even when the default was hold and
    nothing had been applied."""
    src = _find_resolver()
    assert "({safe_default}) was applied" in src


def test_the_expiry_event_does_not_claim_a_hold_was_applied():
    src = inspect.getsource(decisions.DecisionService.expire_overdue)
    assert "still_answerable" in src
    assert 'None if (row["default_action"] or "hold") == "hold"' in src


def test_resuming_after_approval_is_interactive_work():
    """The operator clicked approve and is watching. This enqueued at a bare 46 — the
    REQUESTED band, behind batch work. Same inversion as I6, one layer further in."""
    src = inspect.getsource(decisions)
    assert "priority=JOB_PRIORITY_INTERACTIVE" in src
    assert "priority=46" not in src


# --------------------------------------------------------------------------- #
# The regression: a run waiting on a person is not an orphan
# --------------------------------------------------------------------------- #
def test_a_run_waiting_on_an_answerable_decision_is_not_swept():
    clause = _answerable_decision_clause("code_task_runs")
    assert "decisions d" in clause
    assert "d.id = r.decision_id" in clause
    for state in ("pending", "delegated", "expired"):
        assert state in clause, f"{state} decisions are still answerable and must spare the run"


def test_expired_counts_as_answerable_because_reopening_exists():
    """These two fixes only work together: reopening an expired decision is pointless if the
    sweep has already cancelled the run it would have resumed."""
    assert "expired" in _answerable_decision_clause("meeting_runs")


def test_a_table_without_a_decision_column_gets_no_clause():
    """discovery_runs has no decision_id. The sweep swallows per-table errors so one missing
    table cannot stop the rest — so a bad column reference would have SILENTLY DISABLED that
    table's sweep instead of failing loudly."""
    assert _answerable_decision_clause("discovery_runs") == ""


def test_the_table_list_matches_the_actual_schema():
    """An explicit set drifts the moment someone adds the column. Cross-check the schema."""
    schema = Path("db/schema.sql").read_text()
    for table, _id, _working in shepherd_service._RUN_TABLES:
        block = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?{table} \((.*?)\n\);", schema, re.S)
        if not block:
            continue
        has_column = bool(re.search(r"^\s*decision_id\s", block.group(1), re.M))
        assert has_column == (table in _TABLES_WITH_DECISIONS), (
            f"{table}: schema says decision_id={has_column}, "
            f"_TABLES_WITH_DECISIONS says {table in _TABLES_WITH_DECISIONS}")


def test_the_sweep_uses_the_clause_rather_than_inlining_it():
    src = inspect.getsource(shepherd_service.sweep_abandoned_runs)
    assert "_answerable_decision_clause(table)" in src
