"""Cancel must be an instruction, not a race.

Cancelling a meeting set `mrun_31a5f9bf` to `cancelled` — and the worker then enqueued a NEW
`meeting.step` job for it. It had to be cancelled repeatedly, in a loop, before it stayed clean.

Every phase enqueues its successor when it finishes and nothing consulted the run's status
before doing so. `cancel_queued_job` closes the run and the job THAT EXISTS; it cannot close one
that has not been created yet. On a paid path this is spend continuing after an operator said
stop.

The guard lives in `enqueue_job` — the single function every enqueue passes through — keyed on
a field these payloads already carry. Wiring it into each caller is how the same rule ends up
expressed in seven places and satisfied in five (S3).
"""

import sqlalchemy as sa
import pytest

from boss_engine.services.jobs import (
    _RUN_TABLE_FOR_KIND,
    _TERMINAL_RUN_STATES,
    enqueue_job,
    stopped_run_reason,
)


@pytest.fixture()
def conn():
    """A throwaway SQLite database shaped like the columns this guard reads.

    Deliberately real SQL rather than a mock: the defect was a query that was never made, and a
    mock asserting "we called execute" would pass just as happily against the broken version.
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE meeting_runs (id TEXT PRIMARY KEY, status TEXT)"))
        c.execute(sa.text("CREATE TABLE code_task_runs (id TEXT PRIMARY KEY, status TEXT)"))
        c.execute(sa.text(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,"
            " work_item_id TEXT, tracker_binding_id TEXT, allocation_id TEXT, kind TEXT,"
            " priority INT, priority_score REAL, workload_class TEXT, resource_units INT,"
            " status TEXT, payload TEXT, data_boundary TEXT, max_attempts INT, attempt INT,"
            " run_after TEXT, dedupe_key TEXT)"))
        yield c


def _enqueue(c, kind, run_id):
    return enqueue_job(c, "p1", kind, {"run_id": run_id}, data_boundary="internal")


@pytest.mark.parametrize("state", _TERMINAL_RUN_STATES)
def test_no_step_is_queued_for_a_stopped_meeting(conn, state):
    conn.execute(sa.text("INSERT INTO meeting_runs VALUES ('m1', :s)"), {"s": state})
    out = _enqueue(conn, "meeting.step", "m1")
    assert out["status"] == "skipped"
    assert out["id"] is None
    assert conn.execute(sa.text("SELECT count(*) FROM jobs")).scalar() == 0


def test_a_live_meeting_still_gets_its_next_step(conn):
    """The guard must not stop a meeting that is simply mid-flight — that would be a deadlock,
    not a fix. Asserted on the RULE: the INSERT below it is Postgres-only."""
    conn.execute(sa.text("INSERT INTO meeting_runs VALUES ('m1', 'running')"))
    assert stopped_run_reason(conn, "meeting.step", {"run_id": "m1"}) is None


def test_code_tasks_are_covered_by_the_same_rule(conn):
    conn.execute(sa.text("INSERT INTO code_task_runs VALUES ('c1', 'cancelled')"))
    assert stopped_run_reason(conn, "code.task", {"run_id": "c1"})


def test_a_run_that_does_not_exist_yet_is_not_blocked(conn):
    """The run row is sometimes written after its first job. Absent must mean 'proceed', or the
    ordinary path breaks."""
    assert stopped_run_reason(conn, "meeting.step", {"run_id": "never_seen"}) is None


def test_a_kind_with_no_run_table_is_untouched(conn):
    """Most job kinds have nothing to do with runs; they must not pay for this check."""
    conn.execute(sa.text("INSERT INTO meeting_runs VALUES ('m1', 'cancelled')"))
    assert stopped_run_reason(conn, "canary.noop", {"run_id": "m1"}) is None


def test_a_payload_with_no_run_id_is_untouched(conn):
    assert stopped_run_reason(conn, "meeting.step", {}) is None


def test_a_missing_table_does_not_break_enqueue(conn):
    """Editions differ. A table this deployment does not have must degrade to 'proceed', never
    to a failed enqueue."""
    conn.execute(sa.text("DROP TABLE meeting_runs"))
    assert stopped_run_reason(conn, "meeting.step", {"run_id": "m1"}) is None


def test_the_skip_says_why(conn):
    """A silent no-op is indistinguishable from a lost job."""
    conn.execute(sa.text("INSERT INTO meeting_runs VALUES ('m1', 'cancelled')"))
    out = _enqueue(conn, "meeting.step", "m1")
    assert "cancelled" in out["skipped_reason"] and "m1" in out["skipped_reason"]


def test_every_run_driven_kind_is_mapped():
    """A run-driven kind missing from the map is a phase that can still outlive its cancel."""
    assert {"meeting.step", "code.task"} <= set(_RUN_TABLE_FOR_KIND)


def test_the_guard_runs_before_the_insert():
    """Checking after the INSERT would leave the job queued and merely report it."""
    import inspect
    src = inspect.getsource(enqueue_job)
    assert src.index("stopped_run_reason(") < src.index("INSERT INTO jobs")
