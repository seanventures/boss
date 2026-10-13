"""HOW MANY PARALLEL STREAMS A BUILD STAGE RUNS — DECIDED FROM THE HOST, AT THE STAGE'S START.

A fixed rule ("half the cores") ignores what the host is doing right now: a laptop with twelve
cores, 2.8 GB free and other builds running is not a twelve-core machine. So each writing stage
(write_tests, implement) reads the shape of the host it runs on — INSIDE the container: cgroup
limits when present, else /proc — plus the current load and the other builds running, and picks
N between the project's min and max:

  * each stream reserves ~1 core and ~768 MB;
  * never more than the host has free, never below the min (and says so when the min does not
    fit), never more than the plan has work packages, never more than the model lane allows;
  * the choice and its reasons are recorded on the stage ("3 streams: 12 cores / 2.8 GB free /
    load 0.4 per core; max 6"), so the console shows it and anyone can check it.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: What one stream's own work (installs, builds, a test run) reserves.
MEMORY_PER_STREAM_MB = 768
CORES_PER_STREAM = 1.0
#: THE SPLIT IS OFF UNLESS A PROJECT OPTS IN (build_streams_max >= 2, or build_parallelism).
#: Measured on a HelpDesk-size build (scratchpad profile, 2026-10-13): four parts on three lanes
#: wrote 3.6x the test tokens and 2.3x the implement tokens of one session, then spent 45 min
#: reconciling them, and finished slower than a single session. Build speed no longer turns it
#: on: one lean session is the fast default.
DEFAULT_MAX = 1
SPLIT_OFF = ("split off: one session (the default; parallel parts duplicated work and were "
             "slower; set build_streams_max to opt in)")
HARD_MAX = 8


@dataclass(frozen=True)
class HostShape:
    cores: float
    mem_free_mb: int
    load_per_core: float
    running_builds: int = 0
    source: str = "proc"


@dataclass
class StreamPlan:
    n: int
    why: str
    inputs: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"n": self.n, "why": self.why, **self.inputs}


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def read_host(fs: str = "/", *, running_builds: int = 0) -> HostShape:
    """The host this process runs on, as the container sees it. `fs` roots the files (tests)."""
    base = Path(fs)
    source = "proc"
    cores = float(os.cpu_count() or 1) if fs == "/" else 0.0
    if fs != "/":
        info = _read(base / "proc/cpuinfo") or ""
        cores = float(sum(1 for line in info.splitlines() if line.startswith("processor")) or 1)
    cpu_max = _read(base / "sys/fs/cgroup/cpu.max")
    if cpu_max:
        quota, _, period = cpu_max.partition(" ")
        if quota != "max":
            try:
                limit = int(quota) / int(period or 100000)
                if 0 < limit < cores:
                    cores, source = limit, "cgroup"
            except ValueError:
                pass
    mem_free_mb = 0
    mem_max = _read(base / "sys/fs/cgroup/memory.max")
    if mem_max and mem_max != "max":
        try:
            used = int(_read(base / "sys/fs/cgroup/memory.current") or 0)
            mem_free_mb = max(0, (int(mem_max) - used) // (1024 * 1024))
            source = "cgroup"
        except ValueError:
            mem_free_mb = 0
    if not mem_free_mb:
        meminfo = _read(base / "proc/meminfo") or ""
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                try:
                    mem_free_mb = int(line.split()[1]) // 1024
                except (IndexError, ValueError):
                    pass
    if fs == "/":
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = 0.0
    else:
        raw = _read(base / "proc/loadavg") or "0"
        try:
            load1 = float(raw.split()[0])
        except (IndexError, ValueError):
            load1 = 0.0
    return HostShape(cores=cores, mem_free_mb=mem_free_mb or MEMORY_PER_STREAM_MB,
                     load_per_core=round(load1 / cores, 2) if cores else 0.0,
                     running_builds=max(0, int(running_builds)), source=source)


def plan_streams(host: HostShape, *, min_n: int = 1, max_n: int = 3,
                 packages: Optional[int] = None, model_cap: Optional[int] = None,
                 explicit: int = 0) -> StreamPlan:
    """N for one stage, and why. Pure: the same inputs give the same answer."""
    min_n = max(1, int(min_n or 1))
    max_n = max(min_n, min(HARD_MAX, int(max_n or min_n)))
    busy_cores = host.load_per_core * host.cores + host.running_builds * CORES_PER_STREAM
    free_cores = max(0.0, host.cores - busy_cores)
    by_cpu = int(math.floor(free_cores / CORES_PER_STREAM))
    by_mem = int(host.mem_free_mb // MEMORY_PER_STREAM_MB)
    fits = min(by_cpu, by_mem)
    notes: list[str] = []
    if explicit > 0:
        n = min(int(explicit), HARD_MAX)
        notes.append("set exactly by build_parallelism")
    else:
        n = min(max_n, fits)
        if n < min_n:
            notes.append(f"the host has room for {fits}; ran the minimum {min_n}")
            n = min_n
        elif fits < max_n:
            notes.append("limited by memory" if by_mem <= by_cpu else "limited by free cores")
    if model_cap is not None and n > model_cap:
        # Streams past the model lane only queue; the minimum still stands if asked for.
        n = max(min_n, int(model_cap))
        notes.append(f"the model lane runs {model_cap} at once")
    if packages is not None and n > packages:
        n = max(1, int(packages))
        notes.append(f"the plan has {packages} parts")
    why = (f"{n} stream{'s' if n != 1 else ''}: {host.cores:g} cores / "
           f"{host.mem_free_mb / 1024:.1f} GB free / load {host.load_per_core:.1f} per core"
           + (f"; {host.running_builds} other build{'s' if host.running_builds != 1 else ''} "
              "running" if host.running_builds else "")
           + f"; max {max_n}" + (f" ({'; '.join(notes)})" if notes else ""))
    return StreamPlan(n=n, why=why, inputs={
        "cores": host.cores, "mem_free_mb": host.mem_free_mb,
        "load_per_core": host.load_per_core, "running_builds": host.running_builds,
        "min": min_n, "max": max_n, "packages": packages, "fits": fits, "source": host.source})


def bounds(connection: Any, project_id: Optional[str], settings: Any) -> tuple[int, int, int]:
    """(min, max, explicit) for the project: build_streams_min/max; the max is 1 (split off)
    unless the project sets build_streams_max or build_parallelism."""
    from .project_settings import effective

    def _int(key: str) -> int:
        try:
            return int(effective(connection, project_id, settings, key) or 0)
        except (TypeError, ValueError):
            return 0

    lo = _int("build_streams_min") or 1
    hi = _int("build_streams_max") or DEFAULT_MAX
    return lo, max(lo, hi), _int("build_parallelism")


def split_off(connection: Any, project_id: Optional[str], settings: Any) -> Optional[str]:
    """Why this project's writing stages run as one session, or None when it opted in."""
    try:
        lo, hi, explicit = bounds(connection, project_id, settings)
    except Exception:  # noqa: BLE001 — unreadable settings build with one session
        return SPLIT_OFF
    if explicit > 1 or hi > 1:
        return None
    return SPLIT_OFF


def running_builds(connection: Any, *, exclude_run: Optional[str] = None) -> int:
    """Other builds running on this install right now (each holds at least one stream)."""
    from sqlalchemy import text

    try:
        return int(connection.execute(text(
            "SELECT count(*) FROM code_task_runs WHERE status IN ('running', 'starting') "
            "AND id <> :me"), {"me": exclude_run or ""}).scalar() or 0)
    except Exception:  # noqa: BLE001 — unknown means none
        return 0


def for_stage(connection: Any, project_id: Optional[str], settings: Any, *,
              packages: Optional[int] = None, exclude_run: Optional[str] = None,
              host: Optional[HostShape] = None) -> StreamPlan:
    """The stream plan for a stage starting now on this host."""
    lo, hi, explicit = bounds(connection, project_id, settings)
    shape = host or read_host(running_builds=running_builds(connection, exclude_run=exclude_run))
    cap = max(1, int(getattr(settings, "model_max_concurrency", 3) or 3))
    return plan_streams(shape, min_n=lo, max_n=hi, packages=packages, model_cap=cap,
                        explicit=explicit)
