"""The build page's round row must show whether the round reused its lineage sandbox,
how long provisioning took, and which stages it skipped. [acceptance criterion 8]
"""
from __future__ import annotations

import unittest

from boss_engine.services import build_detail


class BuildPageSandboxRoundP1(unittest.TestCase):
    """P1: build_detail.kept_stations surfaces the recorded sandbox_round facts for a
    reused round, distinct from a round that started fresh."""

    def test_kept_stations_includes_reused_sandbox_round(self):
        fctx = {
            "sandbox_round": {
                "reused": True,
                "provisioning_seconds": 4,
                "stages_skipped": ["design", "spec", "plan"],
            },
        }

        kept = build_detail.kept_stations(fctx)

        self.assertIn("sandbox", kept)
        sandbox = kept["sandbox"]
        self.assertTrue(sandbox["reused"])
        self.assertEqual(sandbox["provisioning_seconds"], 4)
        self.assertEqual(sandbox["stages_skipped"], ["design", "spec", "plan"])


if __name__ == "__main__":
    unittest.main()
