"""`person_wait.poll_due` was only ever reachable by calling it directly -- nothing scheduled
it, so a run paused in `waiting_on_person` stayed paused until someone pressed a button by hand
(L2b follow-up review, PR #17). The resume sweep now rides inside `monitor_scheduler.tick` -- the
same periodic tick the worker already calls every minute via `POST /v1/monitoring/tick`. These
pin two things: (1) `poll_due` enumerates waiting runs and resumes each via `verify_and_resume`,
and (2) `monitor_scheduler.tick` actually schedules that sweep and surfaces its count.

This lives in `engine/tests/` (not `apps/api/tests/`) on purpose: `boss.verify.yaml` declares no
`apps/api` suite, so a test there is never run or counted by the gate -- the covering test for an
engine behaviour belongs in the engine suite.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestPollDueResumesWaitingRuns(unittest.TestCase):
    """poll_due finds every waiting run and resumes the ones whose grant now covers them."""

    def test_a_waiting_run_whose_grant_now_exists_is_resumed(self):
        from boss_engine.services import person_wait

        engine = MagicMock()
        # One run is currently waiting on a person.
        engine.execute.return_value.mappings.return_value.all.return_value = [
            {"run_id": "run_abc"}
        ]

        with patch.object(person_wait, "verify_and_resume",
                          return_value={"resumed": True, "grant_id": "grant_1"}) as resume:
            resumed = person_wait.poll_due(engine, MagicMock())

        self.assertEqual(resumed, 1)
        resume.assert_called_once()
        self.assertEqual(resume.call_args.kwargs["run_id"], "run_abc")

    def test_a_run_whose_grant_is_still_missing_is_not_counted(self):
        from boss_engine.services import person_wait

        engine = MagicMock()
        engine.execute.return_value.mappings.return_value.all.return_value = [
            {"run_id": "run_abc"}
        ]

        with patch.object(person_wait, "verify_and_resume", return_value={"resumed": False}):
            resumed = person_wait.poll_due(engine, MagicMock())

        self.assertEqual(resumed, 0)


class TestMonitorTickSchedulesTheSweep(unittest.TestCase):
    """The resume sweep is wired into the monitoring tick the worker already drives."""

    def test_tick_runs_poll_due_and_reports_the_resumed_count(self):
        from boss_engine.services import monitor_scheduler, person_wait

        # claim() is the one un-guarded iteration in tick(); an empty claim keeps the mock tick
        # harmless. Every other step is wrapped in tick's own `guard(...)`, which swallows the
        # MagicMock engine's quirks, so only the person-wait sweep produces a real result here.
        with patch.object(monitor_scheduler, "claim", return_value=[]), \
             patch.object(person_wait, "poll_due", return_value=1) as poll:
            out = monitor_scheduler.tick(MagicMock(), MagicMock())

        poll.assert_called_once()
        self.assertEqual(out["person_wait_resumed"], 1)


if __name__ == "__main__":
    unittest.main()
