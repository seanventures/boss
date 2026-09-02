"""A person pressing Approve must start the work that was waiting on them.

Written after a run in which the operator read the PRD, approved the spec, and BOSS marked
`build`, `preview` and `deploy` as `path_not_reached` and reported the delivery COMPLETED —
8 of 8 steps, nothing built. The gate flipped its own row to `succeeded` and then called
`advance_run`, which only ever looks at rows still in `waiting`; the successors were never
unlocked because nothing followed the edges out of the step that had just passed.

The second case here is the one that made that bug invisible for so long: an async capability
whose id could not be found reported success instead of failing, so a panel that had just been
convened was recorded as finished in nought seconds.
"""

from __future__ import annotations

import pytest

from boss_engine.services import workflow_capability_runner as runner


class TestTheIdOfTheWorkThatWasStarted:
    """`_awaited_handle` has to cope with tools that disagree about shape."""

    def test_finds_an_id_at_the_top_level(self):
        # create_discovery's shape. This one always worked.
        handle = runner._awaited_handle("research", {"run_id": "drun_abc"})
        assert handle == {"kind": "discovery", "id": "drun_abc"}

    def test_finds_an_id_nested_in_its_own_envelope(self):
        # request_meeting's actual shape, and the whole bug: the id is one level down, so the
        # top-level lookup found nothing and the step completed the instant it started.
        handle = runner._awaited_handle(
            "meeting",
            {"tool": "request_meeting", "status": "running",
             "meeting": {"job_id": "job_1", "run_id": "mrun_abc", "task_id": "tsk_1"}},
        )
        assert handle == {"kind": "meeting", "id": "mrun_abc"}

    def test_prefers_the_top_level_over_the_envelope(self):
        handle = runner._awaited_handle(
            "meeting",
            {"meeting_run_id": "mrun_top", "meeting": {"run_id": "mrun_nested"}},
        )
        assert handle == {"kind": "meeting", "id": "mrun_top"}

    def test_does_not_go_hunting_through_unrelated_envelopes(self):
        # A blind search for any nested run_id would have parked the meeting step against the
        # discovery it was handed as input, and waited for the wrong object to finish.
        handle = runner._awaited_handle(
            "meeting", {"research": {"run_id": "drun_input"}, "context": {"id": "other"}},
        )
        assert handle is None

    @pytest.mark.parametrize("junk", [{}, {"run_id": ""}, {"run_id": "   "}, {"run_id": 7}])
    def test_nothing_usable_is_no_handle(self, junk):
        assert runner._awaited_handle("meeting", junk) is None


class TestTheGateResolverUnlocksSuccessors:
    """The approval path must call the same successor logic every other path calls."""

    def test_it_calls_after_success_and_not_only_advance_run(self):
        # A source assertion, deliberately: the failure was that ONE call was missing next to
        # the write, and the shape of the fix is "these two appear together".
        import inspect

        body = inspect.getsource(runner._resume_answered_gates)
        assert "_after_success" in body, (
            "the gate marks its step succeeded itself, so it must follow the edges out of that "
            "step; advance_run only inspects rows still in 'waiting' and will find nothing"
        )
        assert body.index("status='succeeded'") < body.index("_after_success"), (
            "successors are unlocked after the step is recorded as passed, not before"
        )

    def test_the_advance_is_guarded_so_two_ticks_cannot_double_fire(self):
        import inspect

        body = inspect.getsource(runner._resume_answered_gates)
        assert "if done:" in body, (
            "two sweeps racing on one approved gate must not both advance the run"
        )
