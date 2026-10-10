"""A re-delivered run must resume its work branch, not re-derive it.

Stages checkpoint by pushing their files to the run's work branch, on the stated promise that
"a failure costs the CURRENT stage, never the run". Only the write half of that was built.
`provision` cloned `base_branch` and then did `checkout -b <work_branch>`, which creates a
fresh branch from base and ignores the pushed commits completely — so every re-delivery got a
tree with none of the completed work in it.

Re-delivery is not rare. It happens on every deploy (SIGTERM kills the worker mid-stage), every
crash, and every lost lease. Measured on one run: a deploy at 18:33 killed an implement stage
that had been running 20 minutes, the round restarted from `provisioning`, and `write_tests`
ran again as a fresh model call. The prompt-building code carries its own measurement of what
that costs — ~4.6 min and ~$1 to rewrite a suite that already existed.
"""

from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services.sandbox import EphemeralSandbox


class _Recorder(EphemeralSandbox):
    """A sandbox whose git calls are recorded instead of run."""

    def __init__(self, *, existing_branches=()):
        super().__init__(Settings(), task_id="crun_test")
        self.commands: list[list[str]] = []
        self._existing = set(existing_branches)

    def _git(self, args, redact=None, **kwargs):  # type: ignore[override]
        self.commands.append(list(args))
        if args[:2] == ["ls-remote", "--heads"]:
            return f"abc123\trefs/heads/{args[3]}\n" if args[3] in self._existing else ""
        if args[:1] == ["rev-parse"]:
            return "basesha\n"
        return ""

    def _write_local_excludes(self) -> None:
        pass


def _provision(sandbox, *, base="main", work="boss/code/crun_1"):
    sandbox.provision("owner", "repo", base_branch=base, work_branch=work, token="tok")
    return [" ".join(c) for c in sandbox.commands]


def test_a_first_run_branches_from_base_exactly_as_before():
    """The common case must not change. Nothing has been checkpointed yet, so there is
    nothing to resume and the old path is the correct one."""
    sandbox = _Recorder()
    ran = _provision(sandbox)

    assert "checkout -b boss/code/crun_1" in ran
    assert not any(c.startswith("fetch --depth") and "crun_1" in c for c in ran)
    assert sandbox.resumed_from_checkpoint is False


def test_a_redelivered_run_checks_the_existing_branch_out():
    """The fix. The branch already carries every checkpointed stage."""
    sandbox = _Recorder(existing_branches={"boss/code/crun_1"})
    ran = _provision(sandbox)

    assert any(c.startswith("fetch --depth") and "boss/code/crun_1" in c for c in ran), \
        "the checkpointed commits were never fetched"
    assert "checkout -b boss/code/crun_1 refs/remotes/origin/boss/code/crun_1" in ran
    assert sandbox.resumed_from_checkpoint is True


def test_it_never_branches_from_base_when_it_resumed():
    """Doing both would discard the checkpoint while appearing to honour it."""
    sandbox = _Recorder(existing_branches={"boss/code/crun_1"})
    ran = _provision(sandbox)
    assert "checkout -b boss/code/crun_1" not in ran, "branched from base as well as resuming"


def test_it_asks_the_remote_rather_than_assuming():
    sandbox = _Recorder()
    ran = _provision(sandbox)
    assert any(c.startswith("ls-remote --heads origin boss/code/crun_1") for c in ran)


def test_a_failed_existence_check_falls_back_to_the_old_behaviour():
    """The check is an optimisation. If it cannot be made, branching from base is always safe
    — it is exactly what the code did before — and must never fail provisioning."""
    class _Broken(_Recorder):
        def _git(self, args, redact=None, **kwargs):
            if args[:1] == ["ls-remote"]:
                raise RuntimeError("no network for ls-remote")
            return super()._git(args, redact=redact, **kwargs)

    sandbox = _Broken()
    ran = _provision(sandbox)
    assert "checkout -b boss/code/crun_1" in ran
    assert sandbox.resumed_from_checkpoint is False


def test_a_feedback_iteration_on_its_own_branch_is_untouched():
    """base_branch == work_branch means it is already checked out by the clone; `checkout -b`
    would fail on an existing branch, which is why that case skips this block entirely."""
    sandbox = _Recorder(existing_branches={"boss/code/crun_1"})
    ran = _provision(sandbox, base="boss/code/crun_1", work="boss/code/crun_1")

    assert not any(c.startswith("checkout -b") for c in ran)
    assert sandbox.resumed_from_checkpoint is False


def test_the_token_is_redacted_on_every_remote_call():
    """`origin` carries the token in its URL, so anything that can echo a remote command has
    to be told to redact."""
    import inspect

    src = inspect.getsource(EphemeralSandbox._remote_branch_exists)
    assert "redact=token" in src
    provision = inspect.getsource(EphemeralSandbox._provision)
    resume = provision[provision.index("RESUME THE WORK BRANCH"):]
    assert "redact=token" in resume


@pytest.mark.parametrize("flag", ["resumed_from_checkpoint"])
def test_the_run_can_tell_that_it_resumed(flag):
    """Whether a run started fresh or resumed changes how its timings should be read, so it
    has to be answerable rather than inferred."""
    sandbox = _Recorder(existing_branches={"boss/code/crun_1"})
    _provision(sandbox)
    assert getattr(sandbox, flag) is True
