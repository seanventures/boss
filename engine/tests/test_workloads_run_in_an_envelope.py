"""Every workload runs in an envelope, and the control plane keeps its memory (spec §4b).

Measured on boss-1: api, worker, web and db ran with Memory=0, NanoCpus=0, no pids limit and no
init. Builds, agents and previews were the API's children, so one heavy build competed with the
control plane and Postgres for the same RAM, and the kernel's choice under pressure was whatever
was largest — often the API.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

COMPOSE = yaml.safe_load((ROOT / "scripts/install/community-compose.yml").read_text())
SERVICES = COMPOSE["services"]


def test_every_container_that_spawns_has_an_init_a_memory_bound_and_an_oom_rank():
    for name in ("api", "worker", "mcp-tools"):
        svc = SERVICES[name]
        assert svc.get("init") is True, f"{name}: no init reaps its orphans"
        assert "mem_limit" in svc, f"{name}: no memory bound"
        assert "oom_score_adj" in svc, f"{name}: the kernel picks blindly"


def test_postgres_is_the_last_thing_the_kernel_kills_and_the_api_keeps_a_reservation():
    assert SERVICES["db"]["oom_score_adj"] <= -900
    assert SERVICES["db"]["mem_reservation"]
    api = SERVICES["api"]
    assert api["oom_score_adj"] < 0 and api["mem_reservation"]
    assert api["oom_score_adj"] > SERVICES["db"]["oom_score_adj"]


def test_a_deploy_gives_the_api_time_to_drain():
    for name in ("api", "worker"):
        grace = str(SERVICES[name].get("stop_grace_period") or "10s")
        seconds = int(grace.rstrip("s")) if grace.endswith("s") else 0
        assert seconds >= 60, f"{name}: {grace} ends in SIGKILL mid-build"


def test_a_preview_container_runs_with_init_limits_and_labels():
    from boss_engine.services import preview_service

    args = preview_service.container_limits("preview_node")
    for flag in ("--memory", "--memory-reservation", "--memory-swap", "--cpus",
                 "--pids-limit", "--oom-score-adj"):
        assert flag in args, flag
    assert args[args.index("--memory") + 1] == args[args.index("--memory-swap") + 1], \
        "swap beyond the cap would hide the limit"
    src = inspect.getsource(preview_service.start_container_preview)
    assert '"docker", "run", "-d", "--init", *container_limits(' in src
    assert "*labels" in src and "preview_labels(engine, preview_id)" in src
    labels = preview_service.preview_labels(None, "prev_x")
    assert "boss.owner=preview" in labels and "boss.preview_id=prev_x" in labels


def test_a_sandbox_container_runs_with_init_limits_and_labels(monkeypatch, tmp_path):
    from boss_engine.services import container_runner

    seen = {}

    def fake_run(argv, **_kw):
        seen["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="cid123", stderr="")

    monkeypatch.setattr(container_runner, "preflight", lambda _s: None)
    monkeypatch.setattr(container_runner.owned_process, "run", fake_run)
    runner = container_runner.ContainerCommandRunner(tmp_path, SimpleNamespace(), task_id="t1")
    runner.start()
    argv = seen["argv"]
    assert argv[1] == "run"
    for token in ("--init", "--memory", "--pids-limit", "--oom-score-adj", "boss.owner=sandbox"):
        assert token in argv, token


def test_workload_processes_are_the_kernels_first_choice():
    from boss_engine.services import owned_process
    from boss_engine.services.workload_profiles import workload_oom_score_adj

    assert workload_oom_score_adj() == int(os.environ.get("BOSS_WORKLOAD_OOM_SCORE_ADJ", 500))
    assert {"code_run", "review_seat", "preview", "sandbox"} <= owned_process.WORKLOAD_KINDS
    if not owned_process._proc_available():
        return
    with owned_process.owner_scope("code_run", "run_oom"):
        proc = owned_process.popen(["sleep", "5"])
    try:
        with open(f"/proc/{proc.pid}/oom_score_adj") as fh:
            assert int(fh.read()) == workload_oom_score_adj()
    finally:
        proc.kill()
