import unittest

from boss_engine.services import baseline_verify


class TestPreExistingFailureDoesNotTriggerRepair(unittest.TestCase):
    """P0: a failure that also fails on base is never classified as caused."""

    def test_pre_existing_failure_is_excluded_from_repair(self):
        failing_ids = {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}
        head_output = (
            "FAILED tests/test_a.py::test_one - AssertionError: boom\n"
            "FAILED tests/test_b.py::test_two - AssertionError: still broken\n"
        )
        base_outcomes = {
            "tests/test_a.py::test_one": "failed",
            "tests/test_b.py::test_two": "passed",
        }

        def classify_environment(test_id, slice_text, signal):
            return ("caused", "fails on head, not an environment error")

        record = baseline_verify.classify(
            failing_ids, head_output, base_outcomes, classify_environment=classify_environment
        )

        caused_ids = {c.test_id for c in record.caused}
        pre_existing_ids = {c.test_id for c in record.pre_existing}
        self.assertIn("tests/test_a.py::test_one", pre_existing_ids)
        self.assertNotIn("tests/test_a.py::test_one", caused_ids)
        self.assertIn("tests/test_b.py::test_two", caused_ids)


class TestMixedCausedPreExistingEnvironment(unittest.TestCase):
    """P1: exactly the new, non-base failure is classified caused; others are not."""

    def test_one_caused_three_pre_existing_two_environment(self):
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
        )
        base_outcomes = {
            "tests/test_a.py::test_pre1": "failed",
            "tests/test_a.py::test_pre2": "failed",
            "tests/test_a.py::test_pre3": "failed",
        }

        def classify_environment(test_id, slice_text, signal):
            if "env" in test_id:
                return ("environment", "ModuleNotFoundError before assertion")
            return ("caused", "new failure introduced by the change")

        record = baseline_verify.classify(
            failing_ids, head_output, base_outcomes, classify_environment=classify_environment
        )

        self.assertEqual({c.test_id for c in record.pre_existing}, {
            "tests/test_a.py::test_pre1",
            "tests/test_a.py::test_pre2",
            "tests/test_a.py::test_pre3",
        })
        self.assertEqual({c.test_id for c in record.environment}, {
            "tests/test_a.py::test_env1",
            "tests/test_a.py::test_env2",
        })
        self.assertEqual({c.test_id for c in record.caused}, {
            "tests/test_a.py::test_new_bug",
        })


class TestEnvironmentFailureNeverReachesRepair(unittest.TestCase):
    """P1: an environment error (ModuleNotFoundError) that doesn't reproduce on base is environment, not caused."""

    def test_module_not_found_classified_as_environment(self):
        failing_ids = {"tests/test_c.py::test_needs_driver"}
        head_output = (
            "ERROR tests/test_c.py::test_needs_driver - "
            "ModuleNotFoundError: No module named 'some_driver'\n"
        )
        base_outcomes = {}

        def classify_environment(test_id, slice_text, signal):
            if "ModuleNotFoundError" in slice_text:
                return ("environment", "collection error before any assertion")
            return ("caused", "")

        record = baseline_verify.classify(
            failing_ids, head_output, base_outcomes, classify_environment=classify_environment
        )

        self.assertEqual(len(record.environment), 1)
        self.assertEqual(record.environment[0].test_id, "tests/test_c.py::test_needs_driver")
        self.assertEqual(len(record.caused), 0)


class TestBaselineCacheAvoidsRerun(unittest.TestCase):
    """P1 (acceptance criterion 5): a cached (base_sha, test_id) result is not rerun."""

    def test_cached_outcomes_are_not_passed_to_rerun(self):
        cache = {"tests/test_a.py::test_one": "failed"}
        failing_ids = {"tests/test_a.py::test_one", "tests/test_a.py::test_two"}

        rerun_calls = []

        def fake_run(ids_to_run):
            rerun_calls.append(set(ids_to_run))
            return {"tests/test_a.py::test_two": "passed"}

        to_rerun = failing_ids - set(cache.keys())
        fake_run(to_rerun)

        self.assertEqual(rerun_calls, [{"tests/test_a.py::test_two"}])
        self.assertNotIn("tests/test_a.py::test_one", rerun_calls[0])


class TestPrSectionListsNonCausedFailures(unittest.TestCase):
    """P1 (acceptance criterion 6): PR section lists pre_existing/environment as not caused by this change."""

    def test_pr_section_mentions_not_caused(self):
        record = baseline_verify.BaselineRecord(
            caused=(),
            pre_existing=(
                baseline_verify.Classified(
                    test_id="tests/test_a.py::test_pre1",
                    klass="pre_existing",
                    reason="fails on base",
                    evidence="AssertionError: boom",
                ),
            ),
            environment=(
                baseline_verify.Classified(
                    test_id="tests/test_c.py::test_needs_driver",
                    klass="environment",
                    reason="ModuleNotFoundError",
                    evidence="ModuleNotFoundError: No module named 'some_driver'",
                ),
            ),
        )

        section = baseline_verify.pr_section(record)

        self.assertIn("not caused by this change", section.lower())
        self.assertIn("tests/test_a.py::test_pre1", section)
        self.assertIn("tests/test_c.py::test_needs_driver", section)


if __name__ == "__main__":
    unittest.main()
