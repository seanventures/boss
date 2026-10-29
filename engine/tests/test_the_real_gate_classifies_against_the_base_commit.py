"""L1 follow-up: the production entry point (classify_against_base), not just the pure
`classify` helper, reruns failing tests against the base commit and decides on caused
failures only. A scripted-engine battle: 1 caused + 3 pre-existing + 2 environment failures
in one verify output must yield exactly one caused failure to repair.
"""
import unittest
from pathlib import Path

from boss_engine.services import governed_code_task_service as svc


class TestTheRealGateClassifiesAgainstTheBaseCommit(unittest.TestCase):
    """Drives governed_code_task_service.classify_against_base end to end: it resolves the
    base commit (real git, via _base_commit_sha), then reruns only the uncached ids and
    classifies every failing id against that base result — exactly like the verify gate's
    repair-decision point does."""

    def test_one_caused_three_pre_existing_two_environment_scripted_battle(self):
        root = Path(__file__).resolve().parents[2]
        failing_ids = {
            "tests/test_a.py::test_pre1",
            "tests/test_a.py::test_pre2",
            "tests/test_a.py::test_pre3",
            "tests/test_a.py::test_env1",
            "tests/test_a.py::test_env2",
            "tests/test_a.py::test_new_bug",
        }
        head_output = "".join(
            f"FAILED {tid} - some failure\n" for tid in failing_ids
            if "env" not in tid
        ) + (
            "ERROR tests/test_a.py::test_env1 - ModuleNotFoundError: No module named 'x'\n"
            "ERROR tests/test_a.py::test_env2 - ModuleNotFoundError: No module named 'y'\n"
        )
        # The scripted base run: only the three pre-existing ids fail there too.
        base_run_output = (
            "FAILED tests/test_a.py::test_pre1 - some failure\n"
            "FAILED tests/test_a.py::test_pre2 - some failure\n"
            "FAILED tests/test_a.py::test_pre3 - some failure\n"
            "3 failed, 3 passed in 0.42s\n"   # a real run's summary: the evidence tests ran
        )
        rerun_calls = []

        def scripted_rerun(base_sha, ids_to_run):
            rerun_calls.append(set(ids_to_run))
            return base_run_output

        cache: dict = {}
        record = svc.classify_against_base(
            project_id="proj-battle", root=root, base_branch="HEAD",
            test_cmd="pytest -q", fail_output=head_output, cache=cache,
            rerun=scripted_rerun,
        )

        self.assertIsNotNone(record)
        self.assertEqual({c.test_id for c in record.caused}, {"tests/test_a.py::test_new_bug"})
        self.assertEqual({c.test_id for c in record.pre_existing}, {
            "tests/test_a.py::test_pre1", "tests/test_a.py::test_pre2",
            "tests/test_a.py::test_pre3",
        })
        self.assertEqual({c.test_id for c in record.environment}, {
            "tests/test_a.py::test_env1", "tests/test_a.py::test_env2",
        })
        # Exactly one repair round's worth of failure: the gate the verify step reads.
        self.assertEqual(len(record.caused), 1)
        # Every id was rerun exactly once against the base commit — the cache now holds it.
        self.assertEqual(rerun_calls, [failing_ids])
        self.assertEqual(len(cache), len(failing_ids))

        # The cache (acceptance criterion 5): a second call with the same ids reruns nothing.
        record_again = svc.classify_against_base(
            project_id="proj-battle", root=root, base_branch="HEAD",
            test_cmd="pytest -q", fail_output=head_output, cache=cache,
            rerun=scripted_rerun,
        )
        self.assertEqual(len(rerun_calls), 1)
        self.assertEqual({c.test_id for c in record_again.caused}, {"tests/test_a.py::test_new_bug"})


if __name__ == "__main__":
    unittest.main()
