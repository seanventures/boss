"""L4 — reachability and integrity review: catches hollow and gate-gaming PRs.

Exercises ``boss_engine.services.pr_integrity_review.compute_integrity_evidence``, the
deterministic evidence computation fed to the review seat (see [L:kgn_e497f6d8d1444619ba11]
on why a review stage must look past a green gate). Each scenario below is modeled on one of
the real PRs that slipped past review while hollow or gate-gamed.

``compute_integrity_evidence(changed_files, existing_files=None)`` takes the diff as
``{path: new_text}`` (``None`` text means the path was deleted) plus the rest of the repo as
``{path: text}`` for cross-reference, and returns an ``IntegrityEvidence`` with list fields:
``unreachable``, ``test_only``, ``unenacted``, ``integrity``, ``policy`` — each entry a dict
with at least ``path`` and ``reason``.
"""
import unittest

from boss_engine.services.pr_integrity_review import compute_integrity_evidence


class TestPrIntegrityReview(unittest.TestCase):
    def test_clean_change_passes_with_no_findings(self):
        # P0 — a small, properly wired change: a new helper called from an existing route.
        changed_files = {
            "app/pricing.py": (
                "def apply_discount(total, pct):\n"
                "    return total - (total * pct / 100)\n"
            ),
            "app/routes.py": (
                "from app.pricing import apply_discount\n\n"
                "def checkout(total, pct):\n"
                "    return apply_discount(total, pct)\n"
            ),
        }
        existing_files = {"app/routes.py": changed_files["app/routes.py"]}

        evidence = compute_integrity_evidence(changed_files, existing_files)

        self.assertEqual(evidence.unreachable, [])
        self.assertEqual(evidence.test_only, [])
        self.assertEqual(evidence.unenacted, [])
        self.assertEqual(evidence.integrity, [])
        self.assertEqual(evidence.policy, [])

    def test_unreferenced_new_function_is_flagged_unreachable(self):
        # P1 (reachability) — PRs #9 and #15: a new module nothing on a real entry path calls.
        changed_files = {
            "app/risk_scoring.py": (
                "def score_transaction(tx):\n"
                "    return 0.5\n"
            ),
        }
        existing_files = {
            "app/routes.py": "def checkout(total):\n    return total\n",
        }

        evidence = compute_integrity_evidence(changed_files, existing_files)

        self.assertTrue(
            any(f["path"] == "app/risk_scoring.py" for f in evidence.unreachable),
            evidence.unreachable,
        )

    def test_notimplementederror_stub_is_flagged_test_only(self):
        # P1 (test-only implementation) — PR #17: production raises NotImplementedError while
        # a test monkeypatches a working double defined in the same PR.
        changed_files = {
            "app/fraud_check.py": (
                "def check_fraud(order):\n"
                "    raise NotImplementedError\n"
            ),
            "tests/test_fraud_check.py": (
                "from unittest.mock import patch\n"
                "import app.fraud_check as fraud_check\n\n"
                "def test_check_fraud():\n"
                "    with patch.object(fraud_check, 'check_fraud', return_value=True):\n"
                "        assert fraud_check.check_fraud(None) is True\n"
            ),
        }

        evidence = compute_integrity_evidence(changed_files)

        self.assertTrue(
            any(f["path"] == "app/fraud_check.py" for f in evidence.test_only),
            evidence.test_only,
        )

    def test_recorded_decision_never_read_is_flagged_unenacted(self):
        # P1 (recorded but not enacted) — PR #16: a decision is written but the control flow
        # that follows never reads it back, so the decision cannot change anything.
        changed_files = {
            "app/approvals.py": (
                "def record_decision(order, approved):\n"
                "    order['decision'] = approved\n"
                "    return order\n"
            ),
            "app/fulfillment.py": (
                "def ship(order):\n"
                "    record_decision(order, approved=True)\n"
                "    return dispatch(order)\n"
            ),
        }
        existing_files = {
            "app/fulfillment.py": changed_files["app/fulfillment.py"],
        }

        evidence = compute_integrity_evidence(changed_files, existing_files)

        self.assertTrue(
            any(f["path"] == "app/approvals.py" for f in evidence.unenacted),
            evidence.unenacted,
        )

    def test_deleted_refusal_rule_is_flagged_integrity_violation(self):
        # P1 (integrity) — PR #12: a product refusal path is quietly deleted in non-test code.
        changed_files = {
            "app/export_guard.py": (
                "def guard_export(user, dataset):\n"
                "    return dataset\n"
            ),
        }
        existing_files = {
            "app/export_guard.py": (
                "def guard_export(user, dataset):\n"
                "    if not user.is_verified:\n"
                "        raise PermissionError('export refused: unverified user')\n"
                "    return dataset\n"
            ),
        }

        evidence = compute_integrity_evidence(changed_files, existing_files)

        self.assertTrue(
            any(f["path"] == "app/export_guard.py" for f in evidence.integrity),
            evidence.integrity,
        )

    def test_stray_lockfile_and_shell_true_are_flagged(self):
        # P1 (policy / sandbox escape) — PR #15 v2: a root lockfile for a package manager the
        # repo doesn't use, plus control-plane code that shells out into the workspace.
        changed_files = {
            "yarn.lock": "# stray lockfile, repo uses npm\n",
            "app/control_plane.py": (
                "import subprocess\n\n"
                "def run_in_workspace(cmd, workspace_path):\n"
                "    return subprocess.run(cmd, shell=True, cwd=workspace_path)\n"
            ),
        }
        existing_files = {
            "package-lock.json": "{}\n",
        }

        evidence = compute_integrity_evidence(changed_files, existing_files)

        flagged_paths = {f["path"] for f in evidence.policy}
        self.assertIn("yarn.lock", flagged_paths)
        self.assertIn("app/control_plane.py", flagged_paths)


if __name__ == "__main__":
    unittest.main()
