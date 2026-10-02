"""Tearing a stack down removes everything it started on this machine, and says so if it cannot.

The incident: after the first harness runs the developer's Docker still had four
`boss-prev-prev-…` preview containers serving the fake repo on 0.0.0.0, with their images, and a
`git remote-https … e2e-org/e2e-app.git` re-parented to launchd — while every report said nothing
was left behind. The preview step and git start things outside the process groups and the one
container the stack knew about. These tests prove the mark-and-remove design in teardown.py
without Docker: a fake `docker` stands in for the host's, and real processes stand in for strays.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from e2e.harness import stack as stack_mod
from e2e.harness import teardown
from e2e.harness.target import TargetError

SHIM = teardown.STUB_BIN / "docker"

FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_DOCKER_STATE"]
data = json.load(open(state)) if os.path.exists(state) else {"calls": [], "containers": [], "images": []}
argv = sys.argv[1:]
data["calls"].append(argv)
if argv[:2] == ["ps", "-aq"]:
    print("\\n".join(data["containers"]))
elif argv[:2] == ["images", "-q"]:
    print("\\n".join(data["images"]))
elif argv[:2] == ["rm", "-f"] and not data.get("stubborn"):
    data["containers"] = [c for c in data["containers"] if c not in argv[2:]]
elif argv[:2] == ["rmi", "-f"] and not data.get("stubborn"):
    data["images"] = [i for i in data["images"] if i not in argv[2:]]
json.dump(data, open(state, "w"))
"""


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    binary = tmp_path / "docker"
    binary.write_text(FAKE_DOCKER)
    binary.chmod(0o755)
    state = tmp_path / "docker-state.json"
    monkeypatch.setenv("BOSS_E2E_REAL_DOCKER", str(binary))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))

    def seed(**data):
        state.write_text(json.dumps({"calls": [], "containers": [], "images": [], **data}))

    def read():
        return json.loads(state.read_text())

    seed()
    return seed, read


def _state(tmp_path, **overrides) -> stack_mod.StackState:
    values = dict(run_dir=str(tmp_path / "run"), pg_container="boss-e2e-pg-x", pg_port=1,
                  db_password="pw", api_port=2, model_port=3, fakes_port=4, smtp_port=5,
                  web_port=None, owner_key="o", worker_key="w", console_password="p",
                  session_secret="s", run_id="e2e-selftest-run")
    values.update(overrides)
    Path(values["run_dir"]).mkdir(parents=True, exist_ok=True)
    return stack_mod.StackState(**values)


def _detached_sleeper(env_extra: dict[str, str]) -> subprocess.Popen:
    """A process in its own session that nobody recorded — like the orphaned git."""
    env = {**os.environ, **env_extra}
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env=env,
                            start_new_session=True)


def _gone(proc: subprocess.Popen, within: float = 15) -> bool:
    deadline = time.time() + within
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.2)
    return False


def test_every_container_and_image_the_product_builds_is_labelled_with_the_run(tmp_path):
    """The preview step's own `docker build -t boss-prev-…` and `docker run -d --name …`."""
    log = tmp_path / "argv.json"
    real = tmp_path / "real-docker"
    real.write_text(f"#!/bin/sh\n{sys.executable} -c 'import json,sys; "
                    f"json.dump(sys.argv[1:], open(\"{log}\", \"w\"))' \"$@\"\n")
    real.chmod(0o755)
    env = {**os.environ, "BOSS_E2E_REAL_DOCKER": str(real), "BOSS_E2E_RUN_ID": "run-7",
           "BOSS_E2E_RUN_DIR": str(tmp_path)}
    cases = {
        ("build", "-t", "boss-prev-x", "/w"): ["build", "--label", "boss-e2e.run=run-7", "-t",
                                               "boss-prev-x", "/w"],
        ("run", "-d", "--name", "boss-prev-x", "boss-prev-x"):
            ["run", "--label", "boss-e2e.run=run-7", "-d", "--name", "boss-prev-x",
             "boss-prev-x"],
        ("--context", "default", "container", "create", "img"):
            ["--context", "default", "container", "create", "--label", "boss-e2e.run=run-7",
             "img"],
        ("ps", "-a"): ["ps", "-a"],
        ("rm", "-f", "boss-prev-x"): ["rm", "-f", "boss-prev-x"],
    }
    for argv, expected in cases.items():
        subprocess.run([str(SHIM), *argv], env=env, check=True)
        assert json.loads(log.read_text()) == expected, argv
    ledger = (tmp_path / "docker-ledger.jsonl").read_text().splitlines()
    assert len(ledger) == 3, "each creating call is recorded in the run's ledger"


def test_the_shim_finds_the_run_in_home_when_the_environment_was_filtered(tmp_path):
    """The code agent's environment is deny-by-default; the scratch HOME still names the run
    and the real docker."""
    log = tmp_path / "argv.json"
    real = tmp_path / "real-docker"
    real.write_text(f"#!/bin/sh\n{sys.executable} -c 'import json,sys; "
                    f"json.dump(sys.argv[1:], open(\"{log}\", \"w\"))' \"$@\"\n")
    real.chmod(0o755)
    (tmp_path / ".boss-e2e-run-id").write_text("run-from-home")
    (tmp_path / ".boss-e2e-real-docker").write_text(str(real))
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run([sys.executable, str(SHIM), "run", "img"], env=env, check=True)
    assert json.loads(log.read_text()) == ["run", "--label", "boss-e2e.run=run-from-home", "img"]


def test_the_service_environment_carries_the_runs_mark_and_puts_the_shim_first(tmp_path):
    state = _state(tmp_path)
    env = stack_mod.service_env(state)
    assert env["BOSS_E2E_RUN_ID"] == "e2e-selftest-run"
    assert env["PATH"].split(os.pathsep)[0] == str(teardown.STUB_BIN)
    assert os.access(SHIM, os.X_OK)
    assert "BOSS_E2E_RUN_ID" in env["BOSS_AGENT_ENV_ALLOW"]
    home = Path(env["HOME"])
    assert (home / ".boss-e2e-run-id").read_text() == "e2e-selftest-run"


def test_down_removes_labelled_containers_images_and_stray_processes(tmp_path, fake_docker):
    seed, read = fake_docker
    seed(containers=["prev1", "prev2"], images=["img1"])
    state = _state(tmp_path)
    by_mark = _detached_sleeper({"BOSS_E2E_RUN_ID": state.run_id})
    by_home = _detached_sleeper({"HOME": str(Path(state.run_dir) / "home")})
    unrelated = _detached_sleeper({"BOSS_E2E_RUN_ID": "some-other-run"})
    try:
        left = stack_mod.stop_state(state)
        assert _gone(by_mark) and _gone(by_home), "strays carrying the run's mark are stopped"
        assert unrelated.poll() is None, "another run's processes are not touched"
        assert left == []
        calls = read()["calls"]
        assert ["rm", "-f", "prev1", "prev2"] in calls
        assert ["rm", "-f", "boss-e2e-pg-x"] in calls
        assert ["rmi", "-f", "img1"] in calls
        assert all(f"label=boss-e2e.run={state.run_id}" in c for c in calls
                   if c[:1] in (["ps"], ["images"]))
    finally:
        for proc in (by_mark, by_home, unrelated):
            proc.kill()


def test_down_says_what_is_still_alive_and_how_to_remove_it(tmp_path, fake_docker):
    seed, _ = fake_docker
    seed(containers=["stuck1"], images=["stuckimg"], stubborn=True)
    left = stack_mod.stop_state(_state(tmp_path))
    assert any("container stuck1" in line and "docker rm -f stuck1" in line for line in left)
    assert any("image stuckimg" in line and "docker rmi -f stuckimg" in line for line in left)


def test_a_stale_pid_that_now_belongs_to_another_process_is_never_signalled(tmp_path,
                                                                             fake_docker):
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                start_new_session=True)
    try:
        state = _state(tmp_path, pids={"api": stranger.pid},
                       fingerprints={"api": "Thu Jan 1 00:00:00 1970"})
        stack_mod.stop_state(state)
        time.sleep(0.5)
        assert stranger.poll() is None
    finally:
        stranger.kill()


def test_a_recorded_pid_that_is_still_the_stacks_is_stopped(tmp_path, fake_docker):
    mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            start_new_session=True)
    try:
        state = _state(tmp_path, pids={"api": mine.pid},
                       fingerprints={"api": teardown.fingerprint(mine.pid) or ""})
        stack_mod.stop_state(state)
        assert _gone(mine)
    finally:
        mine.kill()


def test_after_down_the_commands_say_no_stack_is_up_instead_of_calling_a_dead_one(
        tmp_path, fake_docker, monkeypatch):
    pointer = tmp_path / "latest-stack.json"
    monkeypatch.setattr(stack_mod, "LATEST_POINTER", pointer)
    state = _state(tmp_path)
    state.save()
    assert stack_mod.StackState.load().run_id == state.run_id
    stack_mod.stop_state(state)
    assert not pointer.exists()
    with pytest.raises(TargetError, match="No local stack is up"):
        stack_mod.StackState.load()
    assert json.loads((Path(state.run_dir) / "stack.json").read_text())["stopped"] is True
