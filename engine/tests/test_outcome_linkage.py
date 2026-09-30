"""Every attempt at one outcome points back to the same beginning.

The console could only ever show three unrelated lists -- discoveries, meetings, runs --
because nothing joined them. `code_task_runs.task_id` is null on every path that matters, so a
panel and the build it specced had no relationship in the data at all, and an operator had to
infer from titles that a discovery, a meeting and four runs were one piece of work.

`origin` records the beginning, and is INHERITED by retries and follow-ups. Without that
inheritance the fourth attempt at an app is an orphan, and one outcome reads as four.
"""

from __future__ import annotations

import inspect

from boss_engine.services import code_task_service as cts
from boss_engine.services import decisions, prd_decomposition


def test_a_prd_driven_build_records_the_meeting_it_came_from():
    source = inspect.getsource(decisions.DecisionService._start_code_task_after_prd)
    assert '"kind": "meeting"' in source
    assert "origin=origin_ctx" in source


def test_the_build_stores_that_origin():
    source = inspect.getsource(prd_decomposition.start_web_app_build)
    assert "origin" in inspect.signature(prd_decomposition.start_web_app_build).parameters
    assert '"origin": origin' in source


def test_a_retry_carries_the_origin_forward():
    """A retry is the same outcome. Losing the link makes it a new one on screen."""
    source = inspect.getsource(cts.retry_code_task)
    assert 'fctx_prev["origin"]' in source


def test_a_follow_up_carries_the_origin_forward():
    source = inspect.getsource(cts.start_followup_task)
    assert 'fctx["origin"]' in source


def test_lineage_and_origin_answer_different_questions():
    """root_run_id says "which attempts are the same work"; origin says "where did the work
    begin". A card needs both: one groups the runs, the other reaches the panel and dossier."""
    follow_up = inspect.getsource(cts.start_followup_task)
    assert "root_run_id" in follow_up and "origin" in follow_up
