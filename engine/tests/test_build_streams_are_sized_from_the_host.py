"""A build stage sizes its parallel streams from the host as it is when the stage starts.

Not "half the cores": the cores and memory the CONTAINER sees (cgroup limits, else /proc), the
current load and the other builds running, clamped to the project's min/max, never more than
the plan's parts, and the choice recorded with its reasons.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from boss_engine.services import build_streams as bs
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import project_settings as ps


def _host(tmp: Path, *, cpus: int = 12, mem_avail_kb: int = 2_800_000, load1: float = 4.8,
          cpu_max: str | None = None, mem_max: str | None = None, mem_cur: int = 0) -> str:
    (tmp / "proc").mkdir(parents=True, exist_ok=True)
    (tmp / "proc/cpuinfo").write_text("".join(f"processor\t: {i}\n" for i in range(cpus)))
    (tmp / "proc/meminfo").write_text(f"MemTotal: 8024512 kB\nMemAvailable: {mem_avail_kb} kB\n")
    (tmp / "proc/loadavg").write_text(f"{load1} 3.0 2.0 1/100 123\n")
    if cpu_max or mem_max:
        (tmp / "sys/fs/cgroup").mkdir(parents=True, exist_ok=True)
        if cpu_max:
            (tmp / "sys/fs/cgroup/cpu.max").write_text(cpu_max)
        if mem_max:
            (tmp / "sys/fs/cgroup/memory.max").write_text(mem_max)
            (tmp / "sys/fs/cgroup/memory.current").write_text(str(mem_cur))
    return str(tmp)


def test_proc_host_is_read_inside_the_container(tmp_path):
    host = bs.read_host(_host(tmp_path))
    assert host.cores == 12 and host.mem_free_mb == 2_800_000 // 1024
    assert host.load_per_core == 0.4 and host.source == "proc"


def test_cgroup_limits_win_over_proc(tmp_path):
    fs = _host(tmp_path, cpu_max="400000 100000", mem_max=str(4 * 1024**3), mem_cur=1024**3)
    host = bs.read_host(fs)
    assert host.cores == 4.0 and host.mem_free_mb == 3072 and host.source == "cgroup"
    # "max" means no limit: /proc stands.
    fs2 = _host(tmp_path / "b", cpu_max="max 100000", mem_max="max")
    assert bs.read_host(fs2).cores == 12


def test_the_laptop_case_is_memory_bound_and_says_why(tmp_path):
    host = bs.read_host(_host(tmp_path))                     # 12 cores, 2.7 GB, load 0.4/core
    plan = bs.plan_streams(host, min_n=1, max_n=6, packages=5)
    assert plan.n == 3                                       # 2734 MB // 768
    assert plan.why.startswith("3 streams: 12 cores / 2.7 GB free / load 0.4 per core; max 6")
    assert "limited by memory" in plan.why


def test_load_and_other_builds_take_cores(tmp_path):
    host = bs.read_host(_host(tmp_path, cpus=4, mem_avail_kb=16_000_000, load1=2.0),
                        running_builds=1)
    plan = bs.plan_streams(host, min_n=1, max_n=6)
    assert plan.n == 1                                       # 4 - 2 (load) - 1 (a build)
    assert "1 other build running" in plan.why


def test_never_below_min_and_says_so(tmp_path):
    host = bs.read_host(_host(tmp_path, mem_avail_kb=500_000))   # room for 0
    plan = bs.plan_streams(host, min_n=2, max_n=6)
    assert plan.n == 2 and "ran the minimum 2" in plan.why


def test_never_above_max_parts_or_the_model_lane(tmp_path):
    big = bs.read_host(_host(tmp_path, cpus=32, mem_avail_kb=64_000_000, load1=0.0))
    assert bs.plan_streams(big, min_n=1, max_n=6).n == 6
    assert bs.plan_streams(big, min_n=1, max_n=6, packages=2).n == 2
    assert bs.plan_streams(big, min_n=1, max_n=6, model_cap=3).n == 3
    assert bs.plan_streams(big, min_n=1, max_n=50).n == bs.HARD_MAX


def test_an_explicit_build_parallelism_overrides_the_host(tmp_path):
    small = bs.read_host(_host(tmp_path, mem_avail_kb=500_000))
    plan = bs.plan_streams(small, min_n=1, max_n=3, explicit=4)
    assert plan.n == 4 and "build_parallelism" in plan.why


def test_the_split_is_off_unless_the_project_opts_in(monkeypatch):
    from boss_engine.services import project_settings

    def with_settings(values):
        monkeypatch.setattr(project_settings, "effective",
                            lambda c, p, s, key: values.get(key))

    with_settings({"build_speed": "fastest"})
    assert bs.bounds(None, "p", object()) == (1, 1, 0)
    assert bs.split_off(None, "p", object()) == bs.SPLIT_OFF
    with_settings({"build_streams_max": 4})
    assert bs.bounds(None, "p", object())[1] == 4 and bs.split_off(None, "p", object()) is None
    with_settings({"build_parallelism": 3})
    assert bs.split_off(None, "p", object()) is None
    assert set(ps.BUILD_SPEED_PARALLELISM.values()) == {1}


def test_each_writing_stage_sizes_itself_and_records_why():
    src = inspect.getsource(g)
    assert "_streams.for_stage(" in src and "code_task.streams_chosen" in src
    assert "'{streams}'" in src
    # One session records why it is one session, in the same place.
    assert src.count("_record_split_off(engine, project_id, run_id, settings, \"") == 2
    # The old fixed half-cores rule is gone.
    assert "cores // 2" not in inspect.getsource(ps.machine_parallelism)
