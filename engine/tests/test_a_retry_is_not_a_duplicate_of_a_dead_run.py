"""Pressing "Retry build" must not be cancelled as a duplicate of the run it retries.

Five retries of the interview-scorecard build were cancelled within seconds, each with:

    shepherd: identical work is already running as crun_4069691243f842c7865b;
    this run was started afterwards and can only repeat it

crun_4069691243f842c7865b was `failed`, finished hours earlier. Nothing was running. To the
operator the button simply did nothing — the reason existed only in the cancelled row's error
column, which no screen shows.

The cause was an earlier fix, working as designed and not carried all the way through. The
sweep's query was widened from `status IN ('running','queued')` to include recent FAILURES, so
the shepherd could watch them; the keeper selection below it still took the oldest row of each
fingerprint. A failed run is usually the oldest. And a retry has, by construction, the same
fingerprint as the attempt it repeats — so retrying anything that had failed in the last six
hours was impossible.
"""

from __future__ import annotations

from boss_engine.services.shepherd_service import keepers_by_fingerprint

FP = "fp-interview-scorecard"


def _row(run_id: str, status: str) -> dict:
    return {"id": run_id, "status": status}


def test_a_failed_run_is_not_treated_as_work_in_progress():
    rows = [_row("crun_dead", "failed"), _row("crun_retry", "queued")]
    fps = {"crun_dead": FP, "crun_retry": FP}
    assert keepers_by_fingerprint(rows, fps) == {FP: "crun_retry"}


def test_the_retry_is_therefore_not_a_duplicate_of_it():
    """The keeper IS the retry, so duplicate_of resolves to None and nothing is cancelled."""
    rows = [_row("crun_dead", "failed"), _row("crun_retry", "queued")]
    keeper = keepers_by_fingerprint(rows, fps={"crun_dead": FP, "crun_retry": FP})[FP]
    assert keeper == "crun_retry"          # i.e. keeper == the run itself
    assert (keeper if keeper != "crun_retry" else None) is None


def test_completed_and_cancelled_runs_are_history_too():
    for terminal in ("completed", "cancelled", "failed"):
        rows = [_row("crun_old", terminal), _row("crun_new", "running")]
        fps = {"crun_old": FP, "crun_new": FP}
        assert keepers_by_fingerprint(rows, fps) == {FP: "crun_new"}, terminal


def test_a_genuine_duplicate_of_LIVE_work_is_still_caught():
    """The protection this exists for must survive: two live runs, the earlier one keeps."""
    rows = [_row("crun_first", "running"), _row("crun_second", "queued")]
    fps = {"crun_first": FP, "crun_second": FP}
    assert keepers_by_fingerprint(rows, fps) == {FP: "crun_first"}


def test_queued_work_can_keep_against_a_later_queued_duplicate():
    rows = [_row("crun_a", "queued"), _row("crun_b", "queued")]
    assert keepers_by_fingerprint(rows, fps={"crun_a": FP, "crun_b": FP}) == {FP: "crun_a"}


def test_different_work_never_collides():
    rows = [_row("crun_a", "running"), _row("crun_b", "running")]
    fps = {"crun_a": FP, "crun_b": "fp-something-else"}
    assert keepers_by_fingerprint(rows, fps) == {FP: "crun_a", "fp-something-else": "crun_b"}


def test_a_run_with_no_fingerprint_is_never_a_keeper():
    rows = [_row("crun_a", "running")]
    assert keepers_by_fingerprint(rows, fps={"crun_a": None}) == {}


def test_all_terminal_rows_leave_nothing_to_cancel_against():
    rows = [_row("crun_a", "failed"), _row("crun_b", "failed")]
    assert keepers_by_fingerprint(rows, fps={"crun_a": FP, "crun_b": FP}) == {}
