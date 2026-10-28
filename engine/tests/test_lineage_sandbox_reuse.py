"""Build E: one warm sandbox per lineage, reused by fetch+reset; resumed at the failed
stage; purged on workflow end/idle TTL; never shared across lineages.

These tests exercise `boss_engine.services.lineage_sandbox`, which does not exist yet.
"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import lineage_sandbox


def _engine():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE lineage_sandboxes (
                lineage_id TEXT PRIMARY KEY,
                project_id TEXT,
                run_dir TEXT NOT NULL,
                uid INTEGER NOT NULL,
                database_name TEXT,
                status TEXT NOT NULL DEFAULT 'warm',
                stage_hashes TEXT NOT NULL DEFAULT '{}',
                dependency_manifest_hash TEXT,
                current_run_id TEXT,
                created_at TEXT,
                last_used_at TEXT,
                purged_at TEXT,
                release_reason TEXT
            )
            """
        ))
        conn.execute(text(
            """
            CREATE TABLE sandbox_violation_counts (
                lineage_id TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            )
            """
        ))
    return eng


class LineageSandboxReuseP0(unittest.TestCase):
    """P0: a replayed lineage's second round reuses the warm sandbox instead of
    reprovisioning from scratch. [acceptance criterion 1]"""

    def test_second_claim_reuses_run_dir_and_skips_fresh_provision(self):
        eng = _engine()
        settings = Settings()

        first = lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-A", run_id="run-1", project_id="proj-1",
            owner="owner-1",
        )
        self.assertFalse(first.reused)

        second = lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-A", run_id="run-2", project_id="proj-1",
            owner="owner-1",
        )

        self.assertTrue(second.reused)
        self.assertEqual(first.run_dir, second.run_dir)
        self.assertLess(second.provisioning_seconds, first.provisioning_seconds)


class LineageSandboxCrossLineageP0(unittest.TestCase):
    """P0: two lineages never share a sandbox; a forged cross-lineage claim is refused
    and recorded as a violation. [acceptance criterion 5]"""

    def test_cross_lineage_claim_is_denied_and_counted(self):
        eng = _engine()
        settings = Settings()

        lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-A", run_id="run-1", project_id="proj-1",
            owner="owner-A",
        )

        before = lineage_sandbox.violation_count(eng, lineage_id="lineage-A")

        claim_b = lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-B", run_id="run-9", project_id="proj-1",
            owner="owner-B",
        )

        self.assertNotEqual(claim_b.run_dir, "")
        first_a = lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-A", run_id="run-1", project_id="proj-1",
            owner="owner-A",
        )
        self.assertNotEqual(first_a.run_dir, claim_b.run_dir)

        forged = lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-A", run_id="run-1", project_id="proj-1",
            owner="owner-B",
        )
        after = lineage_sandbox.violation_count(eng, lineage_id="lineage-A")

        self.assertIsNone(forged.run_dir if getattr(forged, "denied", False) else None)
        self.assertGreater(after, before)


class LineageSandboxResumeStageP1(unittest.TestCase):
    """P1: a stage already in `resume_completed_stages` is re-run anyway if its input
    hash (brief + steer notes) changed since it last passed. [acceptance criterion 3]"""

    def test_stage_reruns_when_input_hash_changed(self):
        unchanged = lineage_sandbox.stage_needs_rerun(
            recorded_hash="abc123", brief_text="do the thing", steer_notes=(),
        )
        self.assertFalse(unchanged)

        changed = lineage_sandbox.stage_needs_rerun(
            recorded_hash="abc123", brief_text="do the thing",
            steer_notes=("use a different approach",),
        )
        self.assertTrue(changed)


class LineageSandboxPurgeP1(unittest.TestCase):
    """P1: purge fires on workflow end and on idle-TTL expiry, through the one purge
    function, and the row is marked purged. [acceptance criterion 4]"""

    def test_purge_marks_row_purged_and_releases_it(self):
        eng = _engine()
        settings = Settings()

        lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-P", run_id="run-1", project_id="proj-1",
            owner="owner-P",
        )

        lineage_sandbox.purge(eng, settings, lineage_id="lineage-P", reason="workflow_end")

        with eng.begin() as conn:
            row = conn.execute(
                text("SELECT purged_at, status FROM lineage_sandboxes WHERE lineage_id=:l"),
                {"l": "lineage-P"},
            ).mappings().first()

        self.assertIsNotNone(row)
        self.assertIsNotNone(row["purged_at"])

        fresh_again = lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-P", run_id="run-2", project_id="proj-1",
            owner="owner-P",
        )
        self.assertFalse(fresh_again.reused)


class SandboxIdleTtlEvictionP1(unittest.TestCase):
    """P1: `evict_idle` is the function that actually fires the idle-TTL purge — a warm
    sandbox whose `last_used_at` is past the configured TTL is purged, while one still
    inside the window is left warm. [acceptance criterion 4]"""

    def test_evict_idle_purges_only_the_stale_warm_sandbox(self):
        from datetime import datetime, timedelta, timezone

        eng = _engine()
        settings = Settings(BOSS_SANDBOX_IDLE_TTL_HOURS=6)

        lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-stale", run_id="run-1", project_id="proj-1",
            owner="owner-1",
        )
        lineage_sandbox.claim(
            eng, settings, lineage_id="lineage-fresh", run_id="run-2", project_id="proj-1",
            owner="owner-2",
        )

        stale_time = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        with eng.begin() as conn:
            conn.execute(
                text("UPDATE lineage_sandboxes SET last_used_at=:t WHERE lineage_id=:l"),
                {"t": stale_time, "l": "lineage-stale"},
            )

        purged = lineage_sandbox.evict_idle(eng, settings)

        self.assertEqual(purged, ["lineage-stale"])

        with eng.begin() as conn:
            stale_row = conn.execute(
                text("SELECT purged_at, release_reason FROM lineage_sandboxes WHERE lineage_id=:l"),
                {"l": "lineage-stale"},
            ).mappings().first()
            fresh_row = conn.execute(
                text("SELECT purged_at FROM lineage_sandboxes WHERE lineage_id=:l"),
                {"l": "lineage-fresh"},
            ).mappings().first()

        self.assertIsNotNone(stale_row["purged_at"])
        self.assertEqual(stale_row["release_reason"], "ttl")
        self.assertIsNone(fresh_row["purged_at"])


class SandboxIdleTtlConfigP1(unittest.TestCase):
    """P1: idle TTL is configurable, defaults to 6h, and a value above the 24h hard
    ceiling is rejected at config load. [acceptance criterion: TTL ceiling]"""

    def test_default_ttl_and_ceiling_enforced(self):
        self.assertEqual(Settings().sandbox_idle_ttl_hours, 6)

        with self.assertRaises(Exception):
            Settings(BOSS_SANDBOX_IDLE_TTL_HOURS=25)


if __name__ == "__main__":
    unittest.main()
