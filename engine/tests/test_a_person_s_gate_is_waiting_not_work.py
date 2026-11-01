"""A run whose closure ran after the approval counted the whole wait as working time (10.8 min
'working' for 3.3 min of work, 2026-11-01). The wait at a person's gate is waiting."""
from datetime import datetime, timedelta, timezone

from boss_engine.services.run_retrospective import gate_wait_seconds

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_the_wait_between_the_last_work_and_the_answer_is_not_work():
    rows = [{"step_kind": "capability", "completed_at": at(3)},
            {"step_kind": "decision", "completed_at": at(10)},
            {"step_kind": "capability", "completed_at": at(10.2)}]
    work_ends = [at(3), at(10.2)]
    assert gate_wait_seconds(rows, work_ends, T0, at(10.2)) == 7 * 60


def test_a_gate_still_waiting_after_the_last_work_is_left_to_the_waiting_tail():
    rows = [{"step_kind": "capability", "completed_at": at(3)},
            {"step_kind": "decision", "completed_at": None}]
    assert gate_wait_seconds(rows, [at(3)], T0, at(3)) == 0.0
