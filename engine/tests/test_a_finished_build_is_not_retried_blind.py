"""A build that finished cleanly is not retried blind.

The Factory offered "Retry build" on completed runs, and retry_code_task refused nothing, so one
click spent a new governed attempt ($4-15 by code_task_service's own estimate) re-doing work
that had passed. A completed run with failing advisory gates is still a legitimate repair, and
extend_code_task passes the change it wants as fix_note — both must keep working.
"""
from __future__ import annotations

import pytest

from boss_engine.services.code_task_service import RetryRefused, refuse_finished_retry


def test_a_clean_finished_build_is_refused_with_what_to_do():
    with pytest.raises(RetryRefused) as refused:
        refuse_finished_retry({"status": "completed", "finalize_ctx": {}})
    assert "Change this app" in str(refused.value)


@pytest.mark.parametrize("status", ["failed", "blocked", "cancelled", "deadlettered"])
def test_a_build_that_did_not_finish_can_be_retried(status):
    refuse_finished_retry({"status": status, "finalize_ctx": {}})


def test_a_finished_build_with_failing_gates_is_a_repair_and_allowed():
    refuse_finished_retry({"status": "completed",
                           "finalize_ctx": {"advisory_failing_gates": ["tests_strong_enough"]}})


def test_extending_a_finished_build_says_what_to_change_and_is_allowed():
    refuse_finished_retry({"status": "completed", "finalize_ctx": {}},
                          fix_note="add a CSV export")


def test_the_refusal_is_a_value_error_so_existing_callers_still_catch_it():
    assert issubclass(RetryRefused, ValueError)
