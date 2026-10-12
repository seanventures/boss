"""BOSS makes the directories a deploy needs; a person is asked only when BOSS cannot.

A fresh VM reached as root was refused with "/opt/boss-apps is not writable by root; prepare
the host once: sudo mkdir ..." — the directory was merely missing, root could have made it, and
the VM may not have sudo at all. `ensure_dir_sh` is the one way BOSS prepares a directory, and
preflight, bootstrap and the observability stack all use it.
"""
from __future__ import annotations

import inspect
import os
import subprocess

import pytest

from boss_engine.services import deploy_service, deployment_connectors, observability_stack
from boss_engine.services.deploy_service import ensure_dir_sh


def _run(script: str) -> str:
    env = {**os.environ, "PATH": "/usr/bin:/bin"}   # no sudo needed for the cases below
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=env, timeout=20).stdout.strip()


def test_a_missing_directory_under_a_writable_parent_is_made_not_refused(tmp_path):
    target = tmp_path / "opt" / "boss-apps"
    out = _run(ensure_dir_sh(str(target), "me", ok="OK", created="MADE", need="NEED"))
    assert out == "MADE" and target.is_dir()
    # Idempotent: the second pass finds it.
    assert _run(ensure_dir_sh(str(target), "me", ok="OK", created="MADE", need="NEED")) == "OK"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_only_a_directory_boss_cannot_make_is_a_persons_step(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        out = _run(ensure_dir_sh(str(locked / "boss-apps"), "me", ok="OK", created="MADE",
                                 need="NEED") .replace("sudo -n", "false"))
        assert out == "NEED"
    finally:
        locked.chmod(0o700)


def test_paths_are_quoted():
    assert "'/opt/a b'" in ensure_dir_sh("/opt/a b", "u", ok="O", created="C", need="N")


def test_every_host_directory_step_goes_through_the_one_helper():
    for fn in (deployment_connectors.SSHContainerConnector._probe_host,
               deployment_connectors.SSHContainerConnector.bootstrap,
               observability_stack.deploy_stack):
        assert "ensure_dir_sh(" in inspect.getsource(fn), fn.__qualname__
    assert deploy_service.REMOTE_BASE == "/opt/boss-apps"
