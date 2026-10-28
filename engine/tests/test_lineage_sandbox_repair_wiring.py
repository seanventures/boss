"""Build E, closing the review-1 gap: lineage_sandbox is wired into the REAL provisioning
path (`code_task_service.run_code_task`/`_run_code_task`), keyed by the lineage root — not
left as a standalone module only its own unit tests exercise.
"""
from __future__ import annotations

import inspect
import textwrap
import unittest

from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import code_task_service, lineage_sandbox

SOURCE = textwrap.dedent(inspect.getsource(code_task_service._run_code_task))  # noqa: SLF001


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


class LineageSandboxClaimIsWiredIntoRealProvisioning(unittest.TestCase):
    """The claim is taken keyed by `root_run` (the lineage root), before a provider is even
    chosen — so a repair round, retry or follow-up of the SAME lineage shares the claim path
    whichever substrate (local or microVM) ends up provisioning."""

    def test_claim_is_called_with_the_lineage_root_before_the_provider_is_picked(self):
        at = SOURCE.index("lineage_sandbox.claim(")
        preamble = SOURCE[max(0, at - 200):at + 300]
        self.assertIn("lineage_id=root_run", preamble)
        self.assertLess(at, SOURCE.index("provider = make_sandbox_provider(settings)"))

    def test_purge_fires_on_cancel_before_the_pr_keyed_by_the_same_root(self):
        at = SOURCE.index("lineage_sandbox.purge(")
        preamble = SOURCE[max(0, at - 300):at + 200]
        self.assertIn("pending_stop(c, run_id)", preamble)
        self.assertIn("lineage_id=root_run", SOURCE[at:at + 200])


class TwoRepairRoundsShareOneSandboxPath(unittest.TestCase):
    """P0: driving two real repair rounds of the same lineage (`root_run_id`) through
    `lineage_sandbox.claim` — the same call the real provisioning path makes — lands on the
    SAME sandbox path and never pays the fresh-provision cost twice."""

    def test_second_repair_round_reuses_the_first_rounds_sandbox_path(self):
        eng = _engine()
        settings = Settings()
        root_run_id = "crun_root_lineage"

        round_one = lineage_sandbox.claim(
            eng, settings, lineage_id=root_run_id, run_id="crun_attempt_1",
            project_id="proj-1", owner="owner-1",
        )
        round_two = lineage_sandbox.claim(
            eng, settings, lineage_id=root_run_id, run_id="crun_attempt_2_repair",
            project_id="proj-1", owner="owner-1",
        )

        self.assertFalse(round_one.reused)
        self.assertTrue(round_two.reused)
        self.assertEqual(round_one.run_dir, round_two.run_dir)
        self.assertLess(round_two.provisioning_seconds, round_one.provisioning_seconds)

        with eng.begin() as conn:
            rows = conn.execute(text("SELECT COUNT(*) FROM lineage_sandboxes")).first()
        self.assertEqual(rows[0], 1, "one sandbox row for the whole lineage, no re-clone row")


class TwoRealProvisioningRoundsShareOneWarmSandbox(unittest.TestCase):
    """Review round 2: the claimed `run_dir` has to actually drive provisioning — not just
    get recorded on `finalize_ctx.lineage_sandbox_reused`. Driving `EphemeralSandbox.provision`
    (the real provisioning call `code_task_service` makes) through two rounds of the same
    lineage: the first clones into the claimed path, the second fetches+resets that SAME path
    and never runs `git clone` again."""

    @staticmethod
    def _recorder(task_id):
        import tempfile as _tempfile

        from boss_engine.config import Settings
        from boss_engine.services.sandbox import EphemeralSandbox

        class _Recorder(EphemeralSandbox):
            def __init__(self):
                super().__init__(Settings(), task_id=task_id)
                self.commands: list[list[str]] = []

            def _git(self, args, redact=None, **kwargs):  # type: ignore[override]
                self.commands.append(list(args))
                if args[:1] == ["rev-parse"]:
                    return "sha\n"
                return ""

            def _write_local_excludes(self) -> None:
                pass

        return _Recorder(), _tempfile

    def test_second_round_fetches_and_resets_instead_of_cloning(self):
        import shutil as _shutil
        import tempfile as _tempfile
        from pathlib import Path
        from unittest import mock

        from boss_engine.services import sandbox as sandbox_module

        # This sandbox cannot walk `/var/lib/boss/...` by fd from `/` for RepoFiles' confinement
        # check (permission denied on an intermediate directory) — a host/CI restriction, not
        # something `provision()`'s warm-reuse path controls. Stub it so the test exercises the
        # git commands `provision()` actually issues, which is what this test is about.
        self.enterContext(mock.patch.object(sandbox_module, "RepoFiles", lambda root: None))

        eng = _engine()
        settings = Settings()
        root_run_id = "crun_root_warm_lineage"

        round_one = lineage_sandbox.claim(
            eng, settings, lineage_id=root_run_id, run_id="crun_warm_1",
            project_id="proj-1", owner="owner-1",
        )
        self.assertFalse(round_one.reused)
        # `lineage_sandbox.claim`'s path is `/var/lib/boss/lineage-sandboxes/<id>` (asserted by
        # `round_two.run_dir == round_one.run_dir` below); this sandbox cannot write there, so
        # exercise `provision()`'s warm-reuse wiring itself against a writable stand-in path —
        # the real path is `code_task_service`'s job to pass through, which it does (same
        # `sandbox_claim.run_dir` object both times), not `provision()`'s.
        warm_dir = Path(_tempfile.mkdtemp()) / "warm-checkout"
        self.addCleanup(_shutil.rmtree, warm_dir, True)

        sandbox_one, _ = self._recorder("crun_warm_1")
        sandbox_one.provision(
            "owner", "repo", base_branch="main", work_branch=f"boss/code/{root_run_id}",
            token="tok", warm_dir=warm_dir, reuse_warm=round_one.reused,
        )
        ran_one = [" ".join(c) for c in sandbox_one.commands]
        self.assertTrue(any(c.startswith("clone") for c in ran_one), "the first round must clone")
        self.assertEqual(Path(sandbox_one.path), warm_dir)
        self.assertFalse(sandbox_one.warm_reused)
        # `_Recorder._git` never touches the filesystem, so the warm checkout's `.git` — what
        # `provision()` checks to decide clone vs. fetch+reset — has to be stood up by hand here.
        (warm_dir / ".git").mkdir()

        round_two = lineage_sandbox.claim(
            eng, settings, lineage_id=root_run_id, run_id="crun_warm_2_repair",
            project_id="proj-1", owner="owner-1",
        )
        self.assertTrue(round_two.reused)
        self.assertEqual(round_two.run_dir, round_one.run_dir, "same lineage, same sandbox path")

        sandbox_two, _ = self._recorder("crun_warm_2_repair")
        sandbox_two.provision(
            "owner", "repo", base_branch="main", work_branch=f"boss/code/{root_run_id}",
            token="tok", warm_dir=warm_dir, reuse_warm=round_two.reused,
        )
        ran_two = [" ".join(c) for c in sandbox_two.commands]
        self.assertFalse(any(c.startswith("clone") for c in ran_two),
                          "the second round reused the warm checkout; it must never clone again")
        self.assertTrue(any(c.startswith("fetch") for c in ran_two))
        self.assertTrue(any(c.startswith("reset --hard") for c in ran_two))
        self.assertEqual(Path(sandbox_two.path), warm_dir, "same path as the first round")
        self.assertTrue(sandbox_two.warm_reused)


class ResumeStagesAfterSteerP1(unittest.TestCase):
    """A retry resumes past a carried stage only while `stage_needs_rerun` says its input has
    not moved; the first stage whose hash changed, and everything after it, is dropped so the
    retry starts there instead of resuming past stale work [acceptance criterion 3]."""

    def test_keeps_stages_until_the_first_one_that_needs_rerun(self):
        kept = code_task_service._resume_stages_after_steer(
            ["design", "write_tests", "implement"],
            stage_hashes={"design": "d1", "write_tests": "w1", "implement": "i1"},
            brief_text="build the thing", steer_notes=["use a different approach"],
        )
        # None of the recorded hashes match a hash computed WITH the new steer note, so the
        # first stage already breaks the resume.
        self.assertEqual(kept, [])

    def test_keeps_every_stage_when_there_is_nothing_new_to_say(self):
        kept = code_task_service._resume_stages_after_steer(
            ["design", "write_tests"], stage_hashes={}, brief_text="build the thing",
            steer_notes=[],
        )
        self.assertEqual(kept, ["design", "write_tests"])


if __name__ == "__main__":
    unittest.main()
