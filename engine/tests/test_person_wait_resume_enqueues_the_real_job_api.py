"""verify_and_resume's resume step must call the real job-creation API.

Code review on an earlier attempt found `verify_and_resume` calling `jobs.enqueue(...)`
on a `JobLeaseService`, which has no `enqueue` method -- the real API is the module-level
`boss_engine.services.jobs.enqueue_job(connection, project_id, kind, payload, ...)`. Every
existing test passed a `MagicMock()` for `jobs`, so the mismatch never surfaced against a
real caller. This test patches `enqueue_job` itself (not the `jobs` mock) so a regression
back to the broken `jobs.enqueue` call is caught: `enqueue_job` would never be invoked and
this test would fail.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestVerifyAndResumeCallsTheRealEnqueueApi(unittest.TestCase):
    def test_resume_enqueues_via_enqueue_job_not_a_jobs_enqueue_method(self):
        from boss_engine.services import person_wait

        engine = MagicMock()
        settings = MagicMock()
        jobs = MagicMock()
        covering_grant = {"grant_id": "grant_1", "scope": {"permissions": ["code.write"]}}

        run_row = {
            "run_id": "run_1", "project_id": "proj_1", "status": person_wait.STATUS_WAITING_ON_PERSON,
            "decision_id": "dec_1",
            "finalize_ctx": {"person_wait": {"scope": {"permissions": ["code.write"]},
                                              "opened_at": "2026-10-29T00:00:00Z"}},
        }

        with patch.object(person_wait, "_load_run", return_value=run_row), \
             patch("boss_engine.identity.grants.find_covering", return_value=covering_grant), \
             patch.object(person_wait.DecisionService, "transition"), \
             patch.object(person_wait, "record_resolution"), \
             patch("boss_engine.services.jobs.enqueue_job") as enqueue_job:
            result = person_wait.verify_and_resume(engine, settings, jobs, run_id="run_1")

        self.assertTrue(result.get("resumed", False))
        enqueue_job.assert_called_once()
        args, kwargs = enqueue_job.call_args
        self.assertEqual(args[0], engine)
        self.assertEqual(args[1], "proj_1")
        self.assertEqual(args[2], "code.task")
        self.assertEqual(args[3], {"run_id": "run_1"})
        # jobs.enqueue (the broken, nonexistent method) must never be called.
        jobs.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
