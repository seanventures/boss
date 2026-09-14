"""Agent-authored commands should not execute inside the control plane.

A code task runs in a temp directory inside the API/worker container, with full network
egress by explicit design, so the agent's `bash` inherits the control plane's reachability.
On 2026-09-02 an agent's test run reached a production DATABASE_URL and cleared 204 tables.
`agent_env` closed that one variable's route; a deny-list of environment variables is not a
boundary.

This is the boundary: a container with the workspace bind-mounted, no network, non-root, no
capabilities, and resource limits. The live tests below assert the claim rather than
restating it — in particular `test_the_container_really_cannot_reach_the_network`.

Default is still `subprocess`: the container runtime is opt-in until a live verification run
on a real repository is recorded (BOSS-410).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services.container_runner import (
    ContainerCommandRunner,
    ContainerUnavailable,
    docker_available,
    preflight,
)

_DOCKER_OK, _DOCKER_WHY = docker_available()
_IMAGE = os.environ.get("BOSS_TEST_CONTAINER_IMAGE", "python:3.11-slim")
requires_docker = pytest.mark.skipif(not _DOCKER_OK, reason=f"docker unusable: {_DOCKER_WHY}")


def _s(**kw) -> Settings:
    kw.setdefault("BOSS_SANDBOX_CONTAINER_IMAGE", _IMAGE)
    return Settings(**kw)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("print('hello')\n")
    return tmp_path


# --- defaults and fail-closed behaviour ----------------------------------------

def test_the_default_runtime_is_unchanged() -> None:
    """Opt-in until a live verification run is recorded. A new isolation mechanism that
    silently becomes the default is how delivery timelines get wrecked."""
    assert Settings().sandbox_command_runtime == "subprocess"


def test_the_default_network_is_none() -> None:
    """The container needs no network: model calls stay in the control plane."""
    assert Settings().sandbox_container_network == "none"


def test_preflight_explains_itself_when_docker_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _b: None)
    with pytest.raises(ContainerUnavailable) as exc:
        preflight(_s(BOSS_SANDBOX_COMMAND_RUNTIME="docker"))
    message = str(exc.value)
    assert "not on PATH" in message
    assert "BOSS_SANDBOX_COMMAND_RUNTIME=subprocess" in message, (
        "an operator who cannot use docker needs to be told the way out"
    )


def test_a_missing_daemon_is_not_a_silent_downgrade(monkeypatch) -> None:
    monkeypatch.setattr("boss_engine.services.container_runner.docker_available",
                        lambda binary="docker": (False, "daemon not reachable"))
    with pytest.raises(ContainerUnavailable):
        preflight(_s(BOSS_SANDBOX_COMMAND_RUNTIME="docker"))


def test_an_empty_command_is_refused_without_starting_anything(workspace: Path) -> None:
    runner = ContainerCommandRunner(workspace, _s())
    out = runner.run("   ")
    assert out.exit_code == 2 and not out.ok
    assert runner.container_id is None


# --- the isolation claims, tested live -----------------------------------------

@pytest.fixture()
def container(workspace: Path):
    runner = ContainerCommandRunner(workspace, _s(), task_id="pytest")
    try:
        runner.start()
        yield runner
    finally:
        runner.stop()


@requires_docker
def test_a_command_runs_and_reports_its_result(container) -> None:
    out = container.run("python3 app.py")
    assert out.ok and out.exit_code == 0
    assert "hello" in out.stdout


@requires_docker
def test_a_failing_command_is_reported_as_failing(container) -> None:
    out = container.run("python3 -c \"import sys;sys.exit(3)\"")
    assert out.exit_code == 3 and not out.ok


@requires_docker
def test_the_container_really_cannot_reach_the_network(container) -> None:
    """The whole point. If this passes, the isolation claim is false."""
    out = container.run(
        "python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),timeout=4)\"")
    assert not out.ok, "the sandbox container reached the network"


@requires_docker
def test_it_does_not_run_as_root(container) -> None:
    out = container.run("python3 -c \"import os;print(os.getuid())\"")
    assert out.stdout.strip() == str(os.getuid())
    assert out.stdout.strip() != "0"


@requires_docker
def test_writes_land_on_the_host_so_boss_can_read_the_diff(container, workspace: Path) -> None:
    """The change of record is the git diff. A write BOSS cannot see is work lost."""
    container.run("python3 -c \"open('written.txt','w').write('x')\"")
    assert (workspace / "written.txt").read_text() == "x"


@requires_docker
def test_it_cannot_see_the_host_filesystem_outside_the_workspace(container) -> None:
    out = container.run("python3 -c \"import os;print(os.path.exists('/Users'))\"")
    assert out.stdout.strip() == "False"


@requires_docker
def test_a_hanging_command_times_out_rather_than_wedging_the_run(container) -> None:
    out = container.run("python3 -c \"import time;time.sleep(30)\"", timeout=2)
    assert out.timed_out and not out.ok


@requires_docker
def test_one_container_serves_the_whole_task(container) -> None:
    """Latency: a container per COMMAND would add ~1.5s to every step."""
    first = container.container_id
    for _ in range(3):
        container.run("python3 -c pass")
    assert container.container_id == first


@requires_docker
def test_commands_still_yield_to_the_control_plane(container) -> None:
    """`nice` applies inside the container too — the host CPU is still shared."""
    out = container.run("python3 -c \"import os;print(os.nice(0))\"")
    assert int(out.stdout.strip()) > 0


@requires_docker
def test_stopping_is_idempotent_and_leaves_nothing_behind(workspace: Path) -> None:
    runner = ContainerCommandRunner(workspace, _s(), task_id="leak")
    cid = runner.start()
    runner.stop()
    runner.stop()  # must not raise
    import subprocess

    listed = subprocess.run(["docker", "ps", "-aq", "--filter", f"id={cid}"],
                            capture_output=True, text=True, timeout=30)
    assert listed.stdout.strip() == "", "a leaked container outlives the run that made it"
