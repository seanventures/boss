"""Every run silently lost the duration of its final phase.

A phase is only ever timed by the phase that FOLLOWS it — `_phase` banks the previous one's
elapsed as it sets the next. The run then ends through
`_fenced_status(..., "phase='done'")`, which is raw SQL that bypasses `_phase` entirely. So
`opening_pr` — pushing the branch, opening the PR, evaluating gates — contributed nothing to
`phase_timings`, on every run there has ever been.

Measured on crun_3659274905154: phases summed to 13.6m against 16.3m of wall clock. The missing
2.7m sat between `code_task.gate_advisory` and `code_task.pr_opened` with no phase, no turns and
no log line — 17% of the run, and from the console indistinguishable from the stalls this whole
investigation was about.
"""

from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g


def _source_of_inner(name: str) -> str:
    """The body of a closure defined inside `run_governed_code_task`."""
    src = inspect.getsource(g)
    start = src.index(f"    def {name}(")
    nxt = src.index("\n    def ", start + 1)
    return src[start:nxt]


def test_there_is_a_way_to_bank_the_current_phase():
    assert "def _close_phase(" in inspect.getsource(g)


def test_it_appends_a_timing_entry():
    body = _source_of_inner("_close_phase")
    assert "'phase_timings'" in body
    assert "phase_started_at" in body
    assert "EXTRACT(EPOCH FROM (" in body


def test_it_does_not_write_the_phase_column():
    """The terminal write may be refused because ANOTHER execution already finished this run.
    Overwriting its verdict is exactly what the terminal-write guard exists to prevent; adding
    a timing entry is safe either way. `_phase` would have rewritten `phase` and defeated it."""
    body = _source_of_inner("_close_phase")
    assert "phase = :ph" not in body
    assert '"ph":' not in body


def test_it_never_runs_inside_the_terminal_write_transaction():
    """THE REGRESSION THIS SHIPPED WITH, and it hung a run for 26 minutes.

    `_fenced_status` opens its OWN connection and updates `code_task_runs`. The first version
    of `_close_phase` took a connection and was called inside the same `with _transaction(...)`
    block, so the outer transaction held a row lock on the run, the inner connection waited for
    it, and the outer could not commit until the inner returned. Self-deadlock, no error, no
    timeout — the pull request had already been created and its receipt written.

    Nothing in that block had ever written to `code_task_runs` before, which is exactly why the
    nesting looked harmless.
    """
    src = inspect.getsource(g)

    # It takes no connection: it cannot be handed one from an enclosing transaction.
    assert "def _close_phase() -> None:" in src
    assert "def _close_phase(c" not in src

    # And it opens its own, so the lock is released before the terminal write asks for it.
    body = _source_of_inner("_close_phase")
    assert "with _transaction(engine) as c:" in body

    # Every call site is OUTSIDE a transaction block: none may pass a connection.
    assert "_close_phase(c)" not in src


def test_it_is_called_before_the_transaction_that_writes_the_status():
    """Order matters as much as nesting: banked after the status write, the phase would already
    read 'done' and the entry would name the wrong phase."""
    src = inspect.getsource(g)
    for anchor in ('"phase=\'done\', pull_request_url=:u', '{"attempted": "completed_no_pr"}'):
        window = src[max(0, src.index(anchor) - 1200):src.index(anchor)]
        assert "_close_phase()" in window, anchor
        # ...and the call comes before the `with` that performs the write.
        assert window.rindex("_close_phase()") < window.rindex("with _transaction(engine) as c:")


def test_it_does_not_start_a_new_clock():
    """Setting `phase_started_at` again would leave a dangling open phase on a finished run,
    which the next reader would time from and report as an enormous final phase."""
    body = _source_of_inner("_close_phase")
    assert "'phase_started_at', to_jsonb(now())" not in body


def test_it_can_never_cost_a_finished_run_its_pull_request():
    """It runs in the same transaction as the terminal write. A timing entry that could raise
    would turn 'the PR was opened' into 'the run failed at the last step'."""
    body = _source_of_inner("_close_phase")
    assert "except Exception" in body


def test_both_terminal_paths_bank_their_phase():
    """A run ends either by opening a PR or by completing with the PR suppressed. Instrumenting
    one and not the other would leave half the runs still lying about their duration."""
    src = inspect.getsource(g)
    pr_path = src[src.index('"phase=\'done\', pull_request_url=:u') - 900:
                  src.index('"phase=\'done\', pull_request_url=:u')]
    assert "_close_phase()" in pr_path, "the PR path does not bank its last phase"

    no_pr = src[src.index('{"attempted": "completed_no_pr"}') - 1400:
                src.index('{"attempted": "completed_no_pr"}')]
    assert "_close_phase()" in no_pr, "the PR-suppressed path does not bank its last phase"


def test_it_is_banked_before_the_status_write_not_after():
    """`_fenced_status` sets phase='done'. Closing afterwards would time the phase 'done'
    against `opening_pr`'s clock and record the wrong name."""
    src = inspect.getsource(g)
    anchor = src.index('"phase=\'done\', pull_request_url=:u')
    window = src[anchor - 900:anchor]
    assert "_close_phase()" in window
    after = src[anchor:anchor + 600]
    assert "_close_phase()" not in after
