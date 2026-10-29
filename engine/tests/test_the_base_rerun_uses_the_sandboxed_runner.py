"""L1 follow-up (security fix): the base-commit rerun must go through the same sandboxed
runner the normal verify gate uses — the run's identity/owned_process, no shell=True — and it
must rerun only the currently-failing test ids, never the whole suite (acceptance criteria 1+2
of PR #15's required fix to governed_code_task_service._rerun_test_cmd_on_base).
"""
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from boss_engine.services import governed_code_task_service as svc
from boss_engine.services import owned_process, sandbox_identity


class _FakeIdentity:
    """Stands in for a bound sandbox_identity.SandboxIdentity: `workspace()` is the only thing
    `_rerun_test_cmd_on_base` needs from it."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.workspace_calls = []

    def workspace(self, prefix: str) -> Path:
        self.workspace_calls.append(prefix)
        path = self.run_dir / f"{prefix}1"
        path.mkdir(parents=True, exist_ok=True)
        return path


class TestTheBaseRerunUsesTheSandboxedRunner(unittest.TestCase):
    def test_dispatched_through_owned_process_with_no_shell_and_the_runs_identity(self):
        root = Path(__file__).resolve().parents[2]
        identity = _FakeIdentity(Path("/tmp"))
        recorded = {}

        def fake_boss_run(args, **kwargs):
            # boss_run (the control plane's uid) must never touch the run's checkout: its .git
            # holds files only the run's uid/group can read (boss-1, 2026-10-29).
            raise AssertionError(f"boss_run used for the base rerun: {args}")

        def fake_run(args, **kwargs):
            if args[:1] == ["git"]:
                # The worktree is created/removed AS THE RUN'S UID under its own workspace.
                if "add" in args:
                    recorded["worktree_target"] = args[args.index("add") + 2]
                return subprocess.CompletedProcess(args, 0, "", "")
            recorded["argv"] = args
            recorded["kwargs"] = kwargs
            self.assertNotIsInstance(args, str)  # argv, never a shell string
            self.assertNotIn("shell", kwargs)
            return subprocess.CompletedProcess(args, 0, "ok", "")

        with mock.patch.object(owned_process, "boss_run", side_effect=fake_boss_run), \
             mock.patch.object(owned_process, "run", side_effect=fake_run), \
             mock.patch.object(sandbox_identity, "current", return_value=identity):
            out = svc._rerun_test_cmd_on_base(
                root, "deadbeef", ["tests/test_a.py::test_only_failing"],
                "python3 -m pytest -q tests/test_a.py tests/test_b.py", ".")

        self.assertEqual(out, "ok\n")
        self.assertEqual(identity.workspace_calls, ["boss-baseline-"])
        self.assertIn(str(identity.run_dir), recorded["worktree_target"])
        # Only the failing id is passed to the test runner — the rest of the suite's file list
        # (test_b.py) never reaches the base rerun.
        self.assertEqual(recorded["argv"],
                         ["python3", "-m", "pytest", "-q", "tests/test_a.py::test_only_failing"])

    def test_only_failing_ids_are_rerun_not_the_full_suite(self):
        root = Path(__file__).resolve().parents[2]
        captured = {}

        def fake_rerun(sha, ids):
            captured["ids"] = set(ids)
            return ""

        head_output = (
            "FAILED tests/test_a.py::test_one - boom\n"
            "FAILED tests/test_a.py::test_two - boom\n"
        )
        svc.classify_against_base(
            project_id="proj-sel", root=root, base_branch="HEAD",
            test_cmd="python3 -m pytest -q tests/test_a.py tests/test_b.py tests/test_c.py",
            fail_output=head_output, cache={}, rerun=fake_rerun,
        )
        self.assertEqual(captured["ids"],
                         {"tests/test_a.py::test_one", "tests/test_a.py::test_two"})


if __name__ == "__main__":
    unittest.main()
