"""A failing run must not take every other run down with it — really, not just in intent.

`tick()` wraps each run in `try/except` and its comment says "one run must not stop the rest".
Against a DATABASE error that is not achievable by catching: Postgres aborts the whole
transaction on the first failed statement, and every statement afterwards raises
InFailedSqlTransaction no matter what Python caught.

WHAT IT LOOKED LIKE. On 2026-09-27 every tick against the live install returned HTTP 500 naming
an innocent SELECT inside `sweep_gate_timeouts`. No delivery workflow advanced at all. Runs sat
at `ready` indefinitely with nothing recorded against them, because the code that would have
recorded a failure was itself running in the aborted transaction — so the one workflow the
operator was waiting on looked like it was simply queued, for twenty minutes, while every tick
died on the same poisoned connection.

A SAVEPOINT per run is what makes the isolation real.
"""
import os

import pytest
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")


@needs_db
def test_a_failed_statement_poisons_everything_after_it_without_a_savepoint():
    """The behaviour the product was relying on `except` to contain. Pinned so the reason for
    the savepoints below cannot be mistaken for over-engineering."""
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        try:
            connection.execute(text("SELECT * FROM a_table_that_does_not_exist"))
        except Exception:  # noqa: BLE001 — caught, exactly as tick() catches it
            pass
        # Caught. Transaction still aborted. A perfectly good query now fails.
        with pytest.raises(Exception) as caught:
            connection.execute(text("SELECT 1"))
        assert "InFailedSqlTransaction" in type(caught.value).__name__ or \
               "current transaction is aborted" in str(caught.value)


@needs_db
def test_a_savepoint_contains_it_and_the_connection_stays_usable():
    """What tick() does now: the failure rolls back to the savepoint and work continues."""
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        savepoint = connection.begin_nested()
        try:
            connection.execute(text("SELECT * FROM a_table_that_does_not_exist"))
        except Exception:  # noqa: BLE001
            savepoint.rollback()

        # THE WHOLE POINT: the next run in the loop gets a working connection.
        assert connection.execute(text("SELECT 1")).scalar() == 1


@needs_db
def test_work_committed_before_the_failure_survives():
    """A run that succeeded earlier in the same tick must not be rolled back by a later one."""
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("CREATE TEMP TABLE tick_probe (n int)"))

        good = connection.begin_nested()
        connection.execute(text("INSERT INTO tick_probe VALUES (1)"))
        good.commit()

        bad = connection.begin_nested()
        try:
            connection.execute(text("INSERT INTO tick_probe VALUES ('not a number')"))
        except Exception:  # noqa: BLE001
            bad.rollback()

        assert connection.execute(text("SELECT count(*) FROM tick_probe")).scalar() == 1


def test_the_runner_wraps_each_run_in_a_savepoint():
    """Both loops: the revival pass and the main pass. Either one poisoning the transaction
    stops every workflow on the install, so neither may rely on `except` alone."""
    import inspect

    from boss_engine.services import workflow_capability_runner as runner

    source = inspect.getsource(runner.tick)
    # FOUR, and each one earns its place:
    #   the revival loop  — a revival must not break the tick
    #   the run loop      — one run must not stop the rest
    #   each gate         — opening a gate WRITES a decision, the only write in that phase
    #   each step         — so a step that aborts the transaction can still be RECORDED failed
    assert source.count("connection.begin_nested()") == 4, (
        "the revival loop, the run loop, each gate and each step all need their own savepoint")
    assert source.count(".rollback()") == 4
    assert source.count(".commit()") == 4


def test_a_failure_says_which_phase_of_the_run_it_was_in():
    """"Skipped run X: InFailedSqlTransaction on SELECT ..." names a query that was fine. The
    phase is what tells an operator — or me — where to look."""
    import inspect

    from boss_engine.services import workflow_capability_runner as runner

    source = inspect.getsource(runner.tick)
    for label in ('phase = "read the spec"',
                  'phase = "resume awaited work"',
                  'phase = "resume answered gates"',
                  'phase = "claim a step to run"'):
        assert label in source, f"missing phase label: {label}"
    # And the per-step ones name the step, because "open the approval gate" is not actionable
    # without knowing which.
    assert 'f"decide whether {row[\'step_id\']} needs approval"' in source
    assert 'f"execute {claimed[\'step_id\']}"' in source
    # The phase travels into what the tick returns, not only the log.
    assert '"phase": phase,' in source


def test_a_step_that_aborts_the_transaction_can_still_be_recorded_as_failed():
    """THE ONE THAT HID EVERYTHING ELSE.

    `fail_step` opens with SELECT ... FOR UPDATE. When a step's own work aborts the transaction,
    that SELECT raises InFailedSqlTransaction and takes the real exception with it — so the step
    stays `running` with no error against it and the run just stops, silently. The video
    workflow sat on step one for half an hour that way, reporting nothing.

    Rolling back to a savepoint before the handlers run is what makes recording possible.
    """
    import inspect

    from boss_engine.services import workflow_capability_runner as runner

    source = inspect.getsource(runner.tick)
    assert "step_point = connection.begin_nested()" in source
    # BaseException, not Exception: a step killed by a timeout or a cancellation leaves the
    # connection just as unusable, and its failure is just as worth recording.
    assert "except BaseException:" in source
    body = source[source.index("step_point = connection.begin_nested()"):]
    rollback = body.index("step_point.rollback()")
    fail = body.index("workflows.fail_step(")
    assert rollback < fail, "the rollback has to come before anything tries to record"


@needs_db
def test_recording_works_again_after_a_rollback_to_the_savepoint():
    """The behaviour the code above relies on, checked against a real database."""
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("CREATE TEMP TABLE step_probe (id int primary key)"))
        point = connection.begin_nested()
        try:
            connection.execute(text("SELECT * FROM no_such_table_at_all"))
        except Exception:  # noqa: BLE001
            point.rollback()
        # This is `fail_step` doing its work after a step blew up.
        connection.execute(text("INSERT INTO step_probe VALUES (1)"))
        assert connection.execute(text("SELECT count(*) FROM step_probe")).scalar() == 1


def test_an_executor_that_fails_in_the_database_can_still_be_recorded():
    """The deepest layer of the same masking, and the one that erased the actual cause.

    `SkillRuntimeService.invoke` runs the executor in a retry loop that catches every exception
    into `last_error` "for the record". An executor does its own database work — recording an
    artifact, writing a receipt — and when that fails Postgres aborts the transaction. Catching
    does not undo that: the retry then ran against a dead connection, the budget settlement
    reported a poisoned transaction instead of the real problem, and `_record` — the INSERT that
    would have WRITTEN `last_error` down — could not run either.

    A skill failed for a specific, knowable reason and left no trace anywhere: no invocation
    row, no error on the step, a workflow that simply stopped.
    """
    import inspect

    from boss_engine.services.skill_runtime import SkillRuntimeService

    source = inspect.getsource(SkillRuntimeService.invoke)
    assert "attempt_point = connection.begin_nested()" in source
    body = source[source.index("attempt_point = connection.begin_nested()"):]
    # The rollback must come before `last_error` is set, because everything after the abort —
    # including recording the message — needs a working connection.
    assert body.index("attempt_point.rollback()") < body.index("last_error = str(exc)")
    assert "attempt_point.commit()" in source
