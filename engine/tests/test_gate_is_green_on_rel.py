"""Independent checks that the always-red gate on rel/rc10.10-kg is green.

Every BOSS build reruns the engine's enumeration guards and a couple of behavioural suites;
several of them fail on the base branch with no change applied, which makes every build chase
failures it did not cause. These tests assert — through the public symbols the fix must change,
not by re-reading the guards' own prose — the fixed end state for the critical items. They are
written before the fix exists and must go red now and green once it lands.

Scope note (BOSS caps the suite): the two work-orchestration quota cases (criterion 7) and the
monorepo suite-selection case (criterion 8) need the real Postgres `engine` fixture and the
api/worker conftest machinery, so they stay covered by their existing suites rather than being
duplicated here with a hand-rolled schema.
"""
from __future__ import annotations

import inspect
import unittest


class DeleteGuardKnowsEveryStepAgentField(unittest.TestCase):
    """Criterion 1 — `default_captain` in services/monitoring_pack.py is read as an agent-ish
    field but was unknown to the delete guard. It is a soft-default mark on the captain (exactly
    like the step_agents / workflow_catalogue / two_pass readers already listed), so the fix adds
    it to the exemption with that reason rather than treating it as a second name."""

    def test_monitoring_pack_default_captain_is_accounted_for(self):
        from boss_engine.services import agents
        from tests.test_every_step_field_naming_an_agent_guards_its_delete import (
            NOT_AN_AGENT_ON_A_STEP,
        )

        key = (("default_captain",), "services/monitoring_pack.py")
        covered = {path for path, _, _ in agents.STEP_AGENT_FIELDS}
        self.assertTrue(
            key in NOT_AN_AGENT_ON_A_STEP or ("default_captain",) in covered,
            "monitoring_pack.py reads default_captain but the delete guard neither covers nor "
            "exempts it — an agent named there could be deleted from under the step.",
        )


class EveryStartDoorChecksReadiness(unittest.TestCase):
    """Criterion 2 — services/workflow_test_runs starts a run without the readiness check. After
    the fix no start_run caller in that file is unchecked: it calls one of the readiness checks,
    or the door is listed in EXEMPT with its reason."""

    def test_no_workflow_test_runs_door_skips_the_readiness_check(self):
        from tests.test_every_template_says_what_it_needs import (
            EXEMPT,
            _CHECKS,
            _start_run_callers,
        )

        unchecked = [
            f"{rel}:{name}"
            for rel, name, body in _start_run_callers()
            if rel.endswith("services/workflow_test_runs.py")
            and (rel, name) not in EXEMPT
            and not any(check in body for check in _CHECKS)
        ]
        self.assertEqual(
            unchecked, [], f"these start a run without asking the project is ready: {unchecked}"
        )


class UnattendedDoorsAreCurrent(unittest.TestCase):
    """Criterion 3 — event_dispatch.dispatch_event no longer starts runs, so it must leave the
    unattended-door set, which otherwise asserts a refusal path that door no longer has."""

    def test_dispatch_event_is_no_longer_an_unattended_start_door(self):
        from tests.test_every_template_says_what_it_needs import _UNATTENDED, _start_run_callers

        door = ("engine/boss_engine/services/event_dispatch.py", "dispatch_event")
        starters = {(rel, name) for rel, name, _ in _start_run_callers()}
        self.assertNotIn(
            door, _UNATTENDED, "dispatch_event is listed as an unattended start door but no "
            "longer starts runs; drop it from _UNATTENDED."
        )
        if door in _UNATTENDED:  # pragma: no cover - guarded by the assert above
            self.assertIn(door, starters)


class TheToolGateComesBeforeTheWriteGate(unittest.TestCase):
    """Criterion 4 — the source `_run_tool` inspects moved, so the pinned substring is gone. The
    property that must survive is real and order-sensitive: the self-executable / destructive gate
    is checked BEFORE the allow_writes gate, so an explicit instruction (which sets allow_writes)
    cannot route a destructive tool past it."""

    def test_self_executable_gate_precedes_the_write_gate(self):
        from boss_engine.agent.engine_agent import EngineAgent

        body = inspect.getsource(EngineAgent._run_tool)
        self.assertIn("self_executable", body)
        self.assertIn("allow_writes", body)
        self.assertLess(
            body.index("self_executable"),
            body.index("allow_writes"),
            "the destructive/self-executable gate must be checked before the allow_writes gate",
        )


class EverySchedulerEntryPointIsDecided(unittest.TestCase):
    """Criterion 5 — services/lineage_sandbox.py::claim is a new scheduler-shaped entry point.
    The fix decides it: GUARDED (it skips archived projects) or EXEMPT (it starts no project
    work), with a reason — never left undecided."""

    def test_lineage_sandbox_claim_is_guarded_or_exempt(self):
        from tests.test_every_scheduler_skips_archived_projects import EXEMPT, GUARDED

        key = "services/lineage_sandbox.py::claim"
        self.assertIn(
            key, set(GUARDED) | set(EXEMPT),
            "lineage_sandbox.py::claim is a new scheduler entry point and must be classified "
            "GUARDED or EXEMPT with a reason.",
        )


class InMemoryPersistencePreservesRetryLineage(unittest.TestCase):
    """Criterion 6 — retrying a failed run on the OSS in-memory adapter must not need raw SQL.
    A child attempt is minted in the same lineage, carrying the fix note, with no database."""

    def _settings(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            openai_model="user-model", target_github_owner="owner", target_github_repo="repo"
        )

    def test_a_retry_mints_a_child_attempt_without_a_database(self):
        from boss_engine.ports import (
            EnginePorts,
            configure_engine_ports,
            get_engine_ports,
        )
        from boss_engine.services import code_task_service

        previous = get_engine_ports()
        ports = EnginePorts.oss_defaults(self._settings())
        configure_engine_ports(ports)
        try:
            ports.persistence.save_run(
                "run-parent",
                {
                    "project_id": "project", "task_id": "task", "status": "failed",
                    "phase": "done", "title": "Fix a bug",
                    "spec": {
                        "instructions": "Fix it", "acceptance_criteria": ["works"],
                        "test_commands": ["pytest"], "owner": "owner", "repo": "repo",
                        "base_branch": "main",
                    },
                    "state": {"review_summary": "Address the edge case."}, "usage": {},
                    "finalize_ctx": {"attempt": 1}, "reservation_id": None,
                    "pull_request_url": None, "pull_request_number": None,
                    "decision_id": None, "error": "edge case failed",
                },
            )

            class _Jobs:
                def __init__(self):
                    self.created = []

                def create_job(self, _connection, **payload):
                    self.created.append(payload)

            jobs = _Jobs()
            result = code_task_service.retry_code_task(
                None, self._settings(), jobs, "run-parent", fix_note="Handle empty input."
            )
            child = ports.persistence.get_run(result["run_id"])

            self.assertEqual(result["attempt"], 2)
            self.assertIsNotNone(child)
            self.assertEqual(child["finalize_ctx"]["parent_run_id"], "run-parent")
            self.assertEqual(child["finalize_ctx"]["root_run_id"], "run-parent")
            self.assertEqual(child["finalize_ctx"]["attempt"], 2)
            self.assertIn("Handle empty input.", child["spec"]["instructions"])
            self.assertEqual(jobs.created[0]["payload"]["run_id"], result["run_id"])
        finally:
            configure_engine_ports(previous)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
