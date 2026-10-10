"""BOSS-480. Real Git, owned disposable repos, no network or actual credentials.

The adapter records the requested UID boundary; it does not claim a macOS test
can prove Linux's actual setuid/network isolation. Existing Linux A51 qualification
must exercise the final image too. No owned_process census/reaper is invoked here.
"""
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from boss_engine.services import owned_process, repo_host, resource_ledger, sandbox_identity
from boss_engine.services.sandbox import EphemeralSandbox, SandboxError


@pytest.fixture()
def local_git(tmp_path, monkeypatch):
    safe = {"PATH": os.defpath, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull}

    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], env=safe, check=True,
                              capture_output=True, text=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--template=", "-b", "main")
    git(origin, "config", "user.name", "owned-test")
    git(origin, "config", "user.email", "owned@example.invalid")
    (origin / "base.txt").write_text("base\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "base")
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    identity = SimpleNamespace(run_dir=run)
    owner = SimpleNamespace(id="owned-run")
    calls = []

    def run_owned(argv, **kwargs):
        sandboxed = kwargs.pop("sandboxed")
        requested_owner = kwargs.pop("owner", None)
        env = kwargs["env"]
        calls.append((list(argv), sandboxed, requested_owner, dict(env)))
        if sandboxed:
            assert requested_owner is owner
        else:
            # Privileged Git is permitted ONLY under a private, unexposed staging parent.
            checkout = Path(argv[2])
            assert checkout.parent.name.startswith(".boss-git-")
            assert stat.S_IMODE(checkout.parent.stat().st_mode) == 0o700
            assert checkout.parent.parent == run.parent
        return subprocess.run(argv, **kwargs)

    monkeypatch.setattr(owned_process, "run", run_owned)
    monkeypatch.setattr(owned_process, "current_owner", lambda: owner)
    monkeypatch.setattr(sandbox_identity, "current", lambda: identity)
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda got: identity if got is owner else None)
    monkeypatch.setattr(repo_host, "clone_url_for", lambda *a: str(origin))
    monkeypatch.setattr(resource_ledger, "claim_dir", lambda *a, **k: None)
    monkeypatch.setattr(resource_ledger, "release_dir", lambda *a, **k: None)
    settings = SimpleNamespace(sandbox_root_path=run.parent, sandbox_clone_depth=1,
                               sandbox_integration_branch="main", sandbox_validation_timeout_seconds=20)
    sandbox = EphemeralSandbox(settings, task_id="owned-task")
    sandbox.provision("owned", "repo", token=None)
    try:
        yield sandbox, calls, git, tmp_path, identity
    finally:
        sandbox.teardown()


def test_private_provisioning_publishes_a_working_clone_then_clears_credentials(local_git):
    sandbox, calls, git, root, _ = local_git
    assert all(not wrapped for _, wrapped, _, _ in calls)
    assert sandbox._git_env == {} and sandbox._git_own_config == []
    assert not list((root / "runs").glob(".boss-git-*"))
    assert git(sandbox.path, "branch", "--show-current").stdout.strip() == "boss/owned-task"
    assert sandbox.diff() == []
    (sandbox.path / "new.txt").write_text("new content\n")
    assert [(f.path, f.content) for f in sandbox.diff()] == [("new.txt", "new content\n")]
    assert calls[-1][1] is True


@pytest.mark.parametrize("mechanism", ["filter", "include-filter", "hook", "fsmonitor"])
def test_workload_config_can_execute_only_with_workload_authority(local_git, mechanism, monkeypatch):
    sandbox, calls, git, root, _ = local_git
    marker = root / "owned-marker.txt"
    probe = sandbox.path / "marker.py"
    probe.write_text(
        "import os,pathlib,sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.environ.get('BOSS_RC7_SYNTHETIC')) + "
        "'|' + str(os.environ.get('BOSS_EGR_CREDENTIAL')))\n"
        "sys.stdout.write(sys.stdin.read() if len(sys.argv) == 1 else 'token\\0')\n")
    command = shlex.quote(sys.executable) + " " + shlex.quote(str(probe))
    if mechanism in ("filter", "include-filter"):
        (sandbox.path / ".gitattributes").write_text("*.txt filter=marker\n")
        if mechanism == "filter":
            git(sandbox.path, "config", "filter.marker.clean", command)
        else:
            included = sandbox.path / "included.cfg"
            # Git itself quotes the config value, avoiding test-only escaping rules.
            git(sandbox.path, "config", "--file", str(included), "filter.marker.clean", command)
            git(sandbox.path, "config", "include.path", str(included))
        (sandbox.path / "new.txt").write_text("owned text\n")
        operation = ["add", "-A"]
    elif mechanism == "hook":
        hook = sandbox.path / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexec " + command + " </dev/null\n")
        hook.chmod(0o755)
        operation = ["commit", "--allow-empty", "-m", "owned-marker"]
    else:
        git(sandbox.path, "config", "core.fsmonitor", command)
        operation = ["status", "--porcelain"]
    # Synthetic nonsecret markers; never inspect the caller's real environment.
    monkeypatch.setenv("BOSS_RC7_SYNTHETIC", "MUST_NOT_INHERIT")
    sandbox._git_env = {"BOSS_EGR_CREDENTIAL": "SYNTHETIC_NOT_A_CREDENTIAL"}
    sandbox._git(operation)
    assert marker.read_text() == "None|None"
    assert calls[-1][1] is True
    assert "BOSS_RC7_SYNTHETIC" not in calls[-1][3]
    assert "BOSS_EGR_CREDENTIAL" not in calls[-1][3]


def test_expired_run_identity_fails_before_any_git_execution(local_git, monkeypatch):
    sandbox, calls, *_ = local_git
    before = len(calls)
    monkeypatch.setattr(sandbox_identity, "for_owner", lambda owner: None)
    with pytest.raises(SandboxError, match="no longer active"):
        sandbox._git(["status", "--porcelain"])
    assert len(calls) == before


def test_required_isolation_refuses_provision_without_a_bound_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_SANDBOX_ISOLATION", "required")
    monkeypatch.setattr(sandbox_identity, "current", lambda: None)
    settings = SimpleNamespace(sandbox_root_path=tmp_path)
    sandbox = EphemeralSandbox(settings, task_id="unbound")
    with pytest.raises(SandboxError, match="active run identity"):
        sandbox.provision("owned", "repo")
    assert list(tmp_path.iterdir()) == []


def test_a_workload_gitdir_redirect_still_uses_workload_authority(local_git):
    sandbox, calls, git, root, _ = local_git
    redirected = root / "owned-redirect.git"
    (sandbox.path / ".git").rename(redirected)
    (sandbox.path / ".git").write_text(f"gitdir: {redirected}\n")
    assert sandbox.has_changes() is False
    assert calls[-1][1] is True
