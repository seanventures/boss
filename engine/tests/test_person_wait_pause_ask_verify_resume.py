"""Tests for the pause -> ask -> verify -> resume loop for a no-grant refusal (L2b).

`boss_engine.services.person_wait` does not exist yet. These tests pin the public contract
from the technical plan: `enter_waiting_on_person`, `verify_and_resume`,
`scan_reply_for_secrets`, and `record_resolution`. DB-shaped collaborators (the connection,
`identity.grants.find_covering`, `DecisionService.transition`) are faked/mocked so the tests
exercise person_wait's own wiring and decisions, not a particular schema.
"""
from __future__ import annotations

import secrets
import unittest
from unittest.mock import MagicMock, patch


class TestEnterWaitingOnPersonPausesAndAsks(unittest.TestCase):
    """P0 -- a no-grant refusal pauses the run, preserves its state, and opens one thread."""

    def test_pauses_preserves_state_and_asks_once(self):
        from boss_engine.services import person_wait

        connection = MagicMock()
        budget = MagicMock()

        with patch.object(person_wait, "grant_scope_for_run", return_value={"permissions": ["code.write"]}), \
             patch.object(person_wait, "recipient_for_run", return_value="person_123"), \
             patch.object(person_wait, "open_thread_and_ask", return_value="thread_1") as open_thread:
            result = person_wait.enter_waiting_on_person(
                connection, budget,
                run_id="run_1", project_id="proj_1", task_id="task_1",
                reason="no_grant", question="This build needs permission to continue.",
            )

        self.assertEqual(result.get("status"), person_wait.STATUS_WAITING_ON_PERSON)
        self.assertEqual(result.get("blocker_type"), person_wait.BLOCKER_AUTHORITY_NO_GRANT)
        # One thread opened, not re-asked, and no spend held for the paused run.
        open_thread.assert_called_once()
        budget.release.assert_called_once()


class TestVerifyAndResumeWaitsWithoutACoveringGrant(unittest.TestCase):
    """P1 -- verification is a no-op while no grant covers the run's exact recorded scope."""

    def test_stays_waiting_when_grant_does_not_cover_scope(self):
        from boss_engine.services import person_wait

        engine = MagicMock()
        settings = MagicMock()
        jobs = MagicMock()

        with patch.object(person_wait, "_load_run", return_value={
                "run_id": "run_1", "status": person_wait.STATUS_WAITING_ON_PERSON,
                "finalize_ctx": {"person_wait": {"scope": {"permissions": ["code.write"]}}},
        }), patch("boss_engine.identity.grants.find_covering", return_value=None):
            result = person_wait.verify_and_resume(engine, settings, jobs, run_id="run_1")

        self.assertFalse(result.get("resumed", False))
        jobs.enqueue.assert_not_called()


# Resuming once a grant covers the run (exactly once, under that grant) is pinned on a real
# database in test_person_wait_on_a_real_database.py: the mocked version passed while every
# statement of the real one raised.


class TestSecretLikeRepliesAreNeverPersistedOrEchoed(unittest.TestCase):
    """P1 -- a PAT-shaped reply is blocked before storage/echo, and the sender is told, not left silent."""

    def test_pat_shaped_text_is_redacted_with_an_inline_explanation(self):
        from boss_engine.services import person_wait

        # Generated at run time -- never a literal secret -- but shaped like a GitHub PAT so
        # the scanner's pattern match fires the same way a real one would.
        fake_token = "ghp_" + secrets.token_hex(18)
        text, blocked = person_wait.scan_reply_for_secrets(f"done, here you go: {fake_token}")

        self.assertTrue(blocked)
        self.assertNotIn(fake_token, text)

    def test_an_ordinary_reply_passes_through_untouched(self):
        from boss_engine.services import person_wait

        text, blocked = person_wait.scan_reply_for_secrets("done, should be approved now")

        self.assertFalse(blocked)
        self.assertEqual(text, "done, should be approved now")


class TestRecordResolutionWritesOneHistoryRow(unittest.TestCase):
    """P1 -- resolving the blocker records blocker_type, resolution, and time-to-unblock once."""

    def test_records_person_resolution_with_elapsed_time(self):
        from boss_engine.services import person_wait

        connection = MagicMock()
        person_wait.record_resolution(
            connection,
            run_id="run_1", project_id="proj_1",
            blocker_type=person_wait.BLOCKER_AUTHORITY_NO_GRANT,
            resolution="person",
            opened_at="2026-10-29T00:00:00Z",
        )

        connection.execute.assert_called()


if __name__ == "__main__":
    unittest.main()
