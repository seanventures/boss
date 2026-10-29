"""An empty or unusable base rerun is no evidence that a test passed on the base (boss-1, 2026-10-29).

base_outcomes_from_output said "passed" for every id when the base rerun produced nothing (the
worktree could not be made), and the gate cached that per base SHA: 69 rows, all "passed", so every
later build labelled every failure "caused" and repair rounds chased noise."""
from __future__ import annotations

from boss_engine.services import baseline_verify as bv


def test_an_empty_base_output_is_unknown_not_passed():
    ids = ["engine/tests/test_a.py::test_one", "engine/tests/test_b.py::test_two"]
    assert bv.base_outcomes_from_output(ids, "") == {i: "unknown" for i in ids}
    assert bv.base_outcomes_from_output(ids, "fatal: not a git repository") == {i: "unknown" for i in ids}


def test_a_completed_base_run_still_reads_failed_and_passed():
    ids = ["engine/tests/test_a.py::test_one", "engine/tests/test_b.py::test_two"]
    out = ("FAILED engine/tests/test_a.py::test_one - boom\n"
           "1 failed, 1 passed in 0.12s\n")
    assert bv.base_outcomes_from_output(ids, out) == {
        "engine/tests/test_a.py::test_one": "failed", "engine/tests/test_b.py::test_two": "passed"}


def test_unknown_outcomes_are_never_cached():
    from pathlib import Path

    from boss_engine.services import governed_code_task_service as g

    cache: dict = {}
    g.classify_against_base(
        project_id="p", root=Path("."), base_branch="HEAD", test_cmd="python3 -m pytest -q {tests}",
        fail_output="FAILED engine/tests/test_a.py::test_one - boom\n1 failed in 0.1s\n",
        cache=cache, rerun=lambda sha, ids: "")
    assert cache == {}
