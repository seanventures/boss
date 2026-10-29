"""Unit tests for `_run_code_task_once`'s `except AuthorityRefused` branch (L2b follow-up).

A fresh no-grant refusal (no `authority_gap` on the run) pauses the run via
`person_wait.enter_waiting_on_person` and reports `waiting_on_person`. A refusal on a run
that already carries a named `authority_gap` (the lineage-carry-authority case) is restored
to the pre-existing `_fail(..., status="blocked", ...)` behavior and must NOT call
`person_wait.enter_waiting_on_person`. Everything DB-shaped is mocked so this runs without
`BOSS_IDENTITY_FULL_TEST_DATABASE_URL`.
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from boss_engine.services import code_task_service, person_wait


@contextmanager
def _noop_ctx(*_args, **_kwargs):
    yield MagicMock()


class TestAuthorityRefusedPausesOrBlocks(unittest.TestCase):
    def _run(self, *, finalize_ctx):
        engine = MagicMock()
        settings = MagicMock()
        jobs = MagicMock()
        run_id = "run_1"
        run_row = {
            "status": "running",
            "project_id": "proj_1",
            "task_id": "task_1",
            "reservation_id": "res_1",
            "finalize_ctx": finalize_ctx,
            "get": None,
        }
        # Give the dict a real .get so load_code_task_run's result behaves like the real row.
        exc = code_task_service.AuthorityRefused("no_grant")

        with patch("boss_engine.services.owned_process.owner_scope", _noop_ctx), \
                patch("boss_engine.services.sandbox_identity.bind", _noop_ctx), \
                patch("boss_engine.app_links.run_env.for_run", _noop_ctx), \
                patch("boss_engine.services.transient_retry.reporting", _noop_ctx), \
                patch("boss_engine.services.owned_process.terminate_owner"), \
                patch.object(code_task_service, "_run_code_task", side_effect=exc), \
                patch.object(code_task_service, "_transaction", _noop_ctx), \
                patch.object(code_task_service, "load_code_task_run", return_value=run_row), \
                patch.object(code_task_service, "_refused_agent_name", return_value=None), \
                patch.object(code_task_service, "_fail") as fail_mock, \
                patch.object(code_task_service, "get_engine_ports",
                             return_value=MagicMock(budget=MagicMock())), \
                patch.object(person_wait, "enter_waiting_on_person") as enter_mock:
            result = code_task_service._run_code_task_once(engine, settings, jobs, run_id)

        return result, fail_mock, enter_mock

    def test_no_gap_pauses_on_a_person(self):
        result, fail_mock, enter_mock = self._run(finalize_ctx={})

        self.assertEqual(result.get("status"), "waiting_on_person")
        enter_mock.assert_called_once()
        fail_mock.assert_not_called()

    def test_gap_present_blocks_and_names_the_grant(self):
        result, fail_mock, enter_mock = self._run(
            finalize_ctx={"authority_gap": "repo:write on acme/widgets"})

        self.assertEqual(result.get("status"), "blocked")
        self.assertIn("repo:write on acme/widgets", result.get("message", ""))
        enter_mock.assert_not_called()
        fail_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
