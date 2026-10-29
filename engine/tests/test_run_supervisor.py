"""Tests for the L2a run supervisor (engine.boss_engine.services.run_supervisor).

Written before the implementation exists: run_supervisor / run_supervisor_types are imported
by their intended module path per the technical plan.
"""
from __future__ import annotations

import unittest
from unittest import mock

from boss_engine.services import run_supervisor
from boss_engine.services.run_supervisor_types import (
    ACTIONS,
    CHANGE_APPROACH,
    DEGRADED,
    REACH_OUT,
    REFUSAL_ALLOWED,
    STOP_REPORT,
)


def _signal(**overrides):
    base = dict(
        tripped=True,
        reason="repeated_failure",
        forced=False,
        caused=("gate:unit_tests",),
        environment=(),
        refusals=(),
        repeated_signature="gate:unit_tests",
        rounds_used=2,
        spend_usd_micros=100_000,
        elapsed_seconds=60,
    )
    base.update(overrides)
    return run_supervisor.ProgressSignal(**base) if hasattr(run_supervisor, "ProgressSignal") \
        else _make_signal(**base)


def _make_signal(**kwargs):
    from boss_engine.services.run_supervisor_types import ProgressSignal
    return ProgressSignal(**kwargs)


class RunSupervisorTests(unittest.TestCase):
    def test_p0_two_consecutive_rounds_same_work_failure_trip_change_approach(self):
        """P0 -> AC1: repeating the same WORK gate signature for 2 rounds trips the signal
        and the orchestrator returns change_approach (not an unredirected 3rd round)."""
        history = [
            {"round": 1, "faults": {"gate:unit_tests": "WORK"}},
            {"round": 2, "faults": {"gate:unit_tests": "WORK"}},
        ]
        signal = run_supervisor.evaluate_signal(
            faults={"gate:unit_tests": "WORK"},
            rounds_used=2,
            max_repair_rounds=3,
            history=history,
            spend_usd_micros=100_000,
            started_at=None,
            stall_minutes=20,
        )
        self.assertTrue(signal.tripped)
        self.assertEqual(signal.reason, "repeated_failure")

        decision = run_supervisor.decide(
            mock.MagicMock(), mock.MagicMock(), "proj-1",
            run_id="run-1", signal=signal, history=history,
            adapter=mock.MagicMock(return_value={"action": CHANGE_APPROACH}),
        )
        self.assertEqual(decision["action"], CHANGE_APPROACH)

    def test_p1_harness_only_failures_never_enter_another_repair_round(self):
        """P1 -> AC2: a round whose failures classify ONLY as HARNESS (environment) never
        proceeds to another repair round; the signal is forced and caused-failures is empty."""
        signal = run_supervisor.evaluate_signal(
            faults={"gate:flaky_infra": "HARNESS"},
            rounds_used=1,
            max_repair_rounds=3,
            history=[{"round": 1, "faults": {"gate:flaky_infra": "HARNESS"}}],
            spend_usd_micros=0,
            started_at=None,
            stall_minutes=20,
        )
        self.assertEqual(signal.caused, ())
        self.assertTrue(signal.forced or signal.tripped)

        decision = run_supervisor.decide(
            mock.MagicMock(), mock.MagicMock(), "proj-1",
            run_id="run-2", signal=signal, history=[],
            adapter=mock.MagicMock(return_value={"action": STOP_REPORT, "summary": "environment-only failures"}),
        )
        self.assertIn(decision["action"], (STOP_REPORT, "platform_fix"))

    def test_p1_person_only_failures_restricted_to_refusal_allowlist(self):
        """P1 -> AC3: a run whose only failures classify as PERSON is restricted to
        {reach_out, stop_report} even if the (stubbed) judgement call asks for something else,
        and the invariant overrides it before anything executes."""
        signal = _make_signal(
            tripped=True,
            reason="refusal",
            forced=True,
            caused=(),
            environment=(),
            refusals=("needs_secret:api_key",),
            repeated_signature=None,
            rounds_used=1,
            spend_usd_micros=0,
            elapsed_seconds=10,
        )
        self.assertEqual(signal.allowed_actions, REFUSAL_ALLOWED)

        # Judgement (stubbed) wrongly tries to pick an action outside the allow-list.
        bad_adapter = mock.MagicMock(return_value={"action": CHANGE_APPROACH})
        decision = run_supervisor.decide(
            mock.MagicMock(), mock.MagicMock(), "proj-1",
            run_id="run-3", signal=signal, history=[],
            adapter=bad_adapter,
        )
        self.assertIn(decision["action"], REFUSAL_ALLOWED)

    def test_p0_judgement_endpoint_failure_fails_closed_never_reach_out(self):
        """P0 -> AC4/AC7: an exception from the judgement adapter never propagates and never
        silently continues or defaults to reach_out; it resolves to stop_report (or degraded)."""
        signal = _make_signal(
            tripped=True, reason="stalled", forced=False,
            caused=("gate:unit_tests",), environment=(), refusals=(),
            repeated_signature="gate:unit_tests", rounds_used=2,
            spend_usd_micros=50_000, elapsed_seconds=1500,
        )
        raising_adapter = mock.MagicMock(side_effect=RuntimeError("judgement endpoint down"))
        decision = run_supervisor.decide(
            mock.MagicMock(), mock.MagicMock(), "proj-1",
            run_id="run-4", signal=signal, history=[],
            adapter=raising_adapter,
        )
        self.assertIn(decision["action"], (STOP_REPORT, DEGRADED))
        self.assertNotEqual(decision["action"], REACH_OUT)

    def test_p0_round_cap_invariant_forces_judgement_before_fourth_round(self):
        """P0 -> AC5/AC6: no failure set exceeds 3 repair rounds without supervisor judgement
        having run; rounds_used >= 3 forces the signal regardless of its `tripped` value."""
        signal = run_supervisor.evaluate_signal(
            faults={"gate:unit_tests": "WORK"},
            rounds_used=3,
            max_repair_rounds=3,
            history=[{"round": i, "faults": {"gate:other": "WORK"}} for i in (1, 2)],
            spend_usd_micros=10_000,
            started_at=None,
            stall_minutes=20,
        )
        self.assertTrue(signal.forced)

    def test_p0_elapsed_time_with_no_caused_failure_trips_stalled_and_changes_course(self):
        """P0 -> AC1 (time-based variant): no WORK failure to repeat, but wall-clock time has
        passed stall_minutes with nothing caused -- the signal trips as 'stalled' and the
        orchestrator still returns a course-changing action rather than idling another round."""
        signal = run_supervisor.evaluate_signal(
            faults={},
            rounds_used=1,
            max_repair_rounds=3,
            history=[{"round": 1, "faults": {}}],
            spend_usd_micros=0,
            started_at=None,
            stall_minutes=15,
            elapsed_seconds=20 * 60,
        )
        self.assertTrue(signal.tripped)
        self.assertEqual(signal.reason, "stalled")
        self.assertFalse(signal.caused)

        decision = run_supervisor.decide(
            mock.MagicMock(), mock.MagicMock(), "proj-1",
            run_id="run-6", signal=signal, history=[],
            adapter=mock.MagicMock(return_value={"action": CHANGE_APPROACH}),
        )
        self.assertEqual(decision["action"], CHANGE_APPROACH)

    def test_p1_elapsed_time_under_stall_minutes_does_not_trip(self):
        """A run that has not yet exceeded stall_minutes, with no repeated/caused failure and
        rounds under the cap, makes genuine progress and must not be forced into judgement."""
        signal = run_supervisor.evaluate_signal(
            faults={},
            rounds_used=1,
            max_repair_rounds=3,
            history=[],
            spend_usd_micros=0,
            started_at=None,
            stall_minutes=15,
            elapsed_seconds=5 * 60,
        )
        self.assertFalse(signal.tripped)
        self.assertFalse(signal.forced)

    def test_p1_decision_recorded_exactly_once_with_pending_outcome(self):
        """P1 -> AC7: a tripped decision writes exactly one decision record on the run and
        exactly one lesson candidate with worked=None, verified by reading both back."""
        connection = mock.MagicMock()
        connection.get_supervisor_history.return_value = []
        recorded: list = []
        connection.append_supervisor_decision.side_effect = (
            lambda run_id, record: recorded.append((run_id, record)))
        signal = _make_signal(
            tripped=True, reason="repeated_failure", forced=False,
            caused=("gate:unit_tests",), environment=(), refusals=(),
            repeated_signature="gate:unit_tests", rounds_used=2,
            spend_usd_micros=20_000, elapsed_seconds=90,
        )
        decision = {"action": CHANGE_APPROACH, "note": "isolate gate:unit_tests"}

        with mock.patch("boss_engine.services.knowledge.learning_writeback.record") as lesson:
            run_supervisor.record_decision(
                connection, run_id="run-5", project_id="proj-1",
                signal=signal, decision=decision, outcome="pending",
            )
            run_supervisor.write_lesson(
                connection, project_id="proj-1", run_id="run-5",
                signal=signal, decision=decision,
            )
            lesson.assert_called_once()

        # Read the decision back off the connection, instead of mocking record_decision itself.
        self.assertEqual(len(recorded), 1)
        run_id, record = recorded[0]
        self.assertEqual(run_id, "run-5")
        self.assertEqual(record["outcome"], "pending")
        self.assertEqual(record["decision"]["action"], CHANGE_APPROACH)

    def test_p1_second_pending_reach_out_is_not_appended(self):
        """record_decision keeps at most one open reach_out per run: if a prior reach_out
        decision is still pending, a second reach_out decision is dropped rather than
        appended, so a stalled run never accumulates duplicate asks."""
        connection = mock.MagicMock()
        connection.get_supervisor_history.return_value = [
            {"decision": {"action": REACH_OUT}, "outcome": "pending"},
        ]
        recorded: list = []
        connection.append_supervisor_decision.side_effect = (
            lambda run_id, record: recorded.append((run_id, record)))
        signal = _make_signal(
            tripped=True, reason="refusal", forced=True,
            caused=(), environment=(), refusals=("needs_secret:api_key",),
            repeated_signature=None, rounds_used=1,
            spend_usd_micros=0, elapsed_seconds=10,
        )
        run_supervisor.record_decision(
            connection, run_id="run-7", project_id="proj-1",
            signal=signal, decision={"action": REACH_OUT, "question": "which key?"},
            outcome="pending",
        )
        self.assertEqual(recorded, [])

    def test_p1_signal_judgement_action_flags_default_enabled_and_read_project_setting(self):
        """The three independently toggle-able flags (signal_enabled/judgement_enabled/
        action_enabled) default to enabled, and each reads its own named setting through
        project_settings.effective rather than hardcoding a value."""
        settings = mock.MagicMock()
        connection = mock.MagicMock()

        with mock.patch(
            "boss_engine.services.project_settings.effective", return_value=1,
        ) as effective:
            self.assertTrue(run_supervisor.signal_enabled(connection, settings, "proj-1"))
            self.assertTrue(run_supervisor.judgement_enabled(connection, settings, "proj-1"))
            self.assertTrue(run_supervisor.action_enabled(connection, settings, "proj-1"))

        used_settings = {call.args[3] for call in effective.call_args_list}
        self.assertEqual(
            used_settings,
            {"run_supervisor_signal", "run_supervisor_judgement", "run_supervisor_action"},
        )

        with mock.patch(
            "boss_engine.services.project_settings.effective", return_value=0,
        ):
            self.assertFalse(run_supervisor.signal_enabled(connection, settings, "proj-1"))

    def test_p1_flag_read_failure_defaults_to_enabled_not_raise(self):
        """A project_settings read that raises must never bubble up and never silently
        disable the supervisor; it falls back to the settings object's own attribute (or 1)."""
        connection = mock.MagicMock()
        settings = mock.MagicMock(run_supervisor_signal=1)
        with mock.patch(
            "boss_engine.services.project_settings.effective",
            side_effect=RuntimeError("settings store down"),
        ):
            self.assertTrue(run_supervisor.signal_enabled(connection, settings, "proj-1"))


if __name__ == "__main__":
    unittest.main()
