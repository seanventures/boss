"""L4 wiring: the review stage must actually hand the integrity evidence to the reviewer.

Covers the two helpers governed_code_task_service wires into every review seat's prompt:
``gather_existing_files`` (a bounded repo read for cross-referencing a diff's new functions)
and ``render_evidence_clause`` (the evidence rendered into the prompt the seat judges).
"""
import os
import tempfile
import unittest

from boss_engine.services.pr_integrity_review import (
    IntegrityEvidence,
    compute_integrity_evidence,
    gather_existing_files,
    render_evidence_clause,
)


class TestRenderEvidenceClause(unittest.TestCase):
    def test_no_findings_renders_nothing(self):
        self.assertEqual(render_evidence_clause(IntegrityEvidence()), "")

    def test_findings_are_rendered_as_evidence_for_the_seat_to_judge_not_a_verdict(self):
        evidence = IntegrityEvidence(
            unreachable=[{"path": "app/risk_scoring.py", "reason": "no caller"}],
            policy=[{"path": "yarn.lock", "reason": "stray lockfile"}],
        )
        clause = render_evidence_clause(evidence)
        self.assertIn("app/risk_scoring.py", clause)
        self.assertIn("no caller", clause)
        self.assertIn("yarn.lock", clause)
        self.assertIn("judge whether it is a REAL defect", clause)
        self.assertNotIn("VERDICT:", clause)


class TestGatherExistingFiles(unittest.TestCase):
    def test_reads_other_source_files_but_skips_changed_and_test_paths_and_vendor_dirs(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "app"))
            os.makedirs(os.path.join(root, "node_modules", "pkg"))
            with open(os.path.join(root, "app", "routes.py"), "w") as fh:
                fh.write("def checkout():\n    return apply_discount(1)\n")
            with open(os.path.join(root, "app", "test_routes.py"), "w") as fh:
                fh.write("def test_checkout():\n    pass\n")
            with open(os.path.join(root, "app", "changed.py"), "w") as fh:
                fh.write("def new_thing():\n    pass\n")
            with open(os.path.join(root, "node_modules", "pkg", "index.py"), "w") as fh:
                fh.write("def vendored():\n    pass\n")

            existing = gather_existing_files(root, changed_paths=["app/changed.py"])

            self.assertIn("app/routes.py", existing)
            self.assertIn("apply_discount", existing["app/routes.py"])
            self.assertNotIn("app/changed.py", existing)
            self.assertNotIn("app/test_routes.py", existing)
            self.assertTrue(all("node_modules" not in p for p in existing))

    def test_feeds_compute_integrity_evidence_for_cross_file_reachability(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "app"))
            with open(os.path.join(root, "app", "routes.py"), "w") as fh:
                fh.write("from app.pricing import apply_discount\n\n"
                          "def checkout(total, pct):\n    return apply_discount(total, pct)\n")

            changed_files = {
                "app/pricing.py": "def apply_discount(total, pct):\n"
                                   "    return total - (total * pct / 100)\n",
            }
            existing = gather_existing_files(root, changed_paths=list(changed_files))
            evidence = compute_integrity_evidence(changed_files, existing)

            self.assertEqual(evidence.unreachable, [])


if __name__ == "__main__":
    unittest.main()
