"""What BOSS can see about the machine it is itself running on.

BOSS can interrogate its own database in fine detail and has host inspection for the machines it
DEPLOYS to. It has nothing at all for the container it runs in. So `claude code agent exited -9`
cost roughly an hour of hypothesis-testing (I20) — OOM killer? deploy restart? internal reaper? —
and was finally settled by READING CODE rather than reading data. The answer was BOSS's own
process reaper, and one number (an OOM count of zero) would have said so immediately.

It is also why experiment E1 is currently unprovable. `/v1/code-tasks?limit=5` goes from 0.37s to
47s while a build runs, and the inference that this is CPU starvation is exactly that — an
inference. Load average and cgroup throttling turn it into a measurement.

None of this needs the docker socket, which the container does not have and should not be given.
Everything here is readable from inside the container:

  * `/proc/loadavg`    — runnable threads, i.e. is anything actually contending?
  * `/sys/fs/cgroup/*` — the container's OWN cpu quota, throttling, memory ceiling and OOM count
  * `/proc/meminfo`    — the host's view, for context when the cgroup has no ceiling

Every reader degrades to `None` rather than raising. A diagnostic that can fail the request it
is diagnosing is worse than no diagnostic, and this runs on macOS in development where none of
these files exist.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

CGROUP_V2 = Path("/sys/fs/cgroup")
CGROUP_V1_CPU = Path("/sys/fs/cgroup/cpu")
CGROUP_V1_MEM = Path("/sys/fs/cgroup/memory")


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return None


def _read_int(path: Path) -> Optional[int]:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def _read_keyed(path: Path) -> dict[str, int]:
    """Parse `key value` files (cpu.stat, memory.events, /proc/meminfo)."""
    out: dict[str, int] = {}
    raw = _read_text(path)
    if not raw:
        return out
    for line in raw.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return out


def cpu_snapshot() -> dict[str, Any]:
    """Is anything contending for CPU, and are we being throttled for it?

    `loadavg` is the number the E1 hypothesis actually predicts: a build that spawns dozens of
    runnable processes drives 1-minute load far above the core count, and that — not a busy API —
    is what delays the API's threads.
    """
    out: dict[str, Any] = {"cores": os.cpu_count()}

    raw = _read_text(Path("/proc/loadavg"))
    if raw:
        parts = raw.split()
        try:
            out["load_1m"], out["load_5m"], out["load_15m"] = (float(p) for p in parts[:3])
            # "3/412" — runnable/total. Runnable is the direct measure of contention.
            if len(parts) > 3 and "/" in parts[3]:
                runnable, total = parts[3].split("/", 1)
                out["runnable"] = int(runnable)
                out["threads_total"] = int(total)
        except (ValueError, IndexError):
            pass
        if out.get("load_1m") is not None and out.get("cores"):
            # >1.0 means more runnable work than cores: someone is waiting for a timeslice.
            out["load_per_core"] = round(out["load_1m"] / max(1, out["cores"]), 2)

    # cgroup v2 quota: "max 100000" (uncapped) or "50000 100000" (half a core)
    quota = _read_text(CGROUP_V2 / "cpu.max")
    if quota:
        bits = quota.split()
        if bits and bits[0] != "max":
            try:
                out["cpu_limit_cores"] = round(int(bits[0]) / int(bits[1]), 2)
            except (ValueError, ZeroDivisionError, IndexError):
                pass
        else:
            out["cpu_limit_cores"] = None  # explicitly uncapped, not merely unknown

    stat = _read_keyed(CGROUP_V2 / "cpu.stat")
    if stat:
        # nr_throttled > 0 proves the container hit its own ceiling — a different fault from
        # simply having too much to do, and it needs a different fix.
        out["throttled_periods"] = stat.get("nr_throttled")
        out["throttled_seconds"] = (
            round(stat["throttled_usec"] / 1_000_000, 2) if "throttled_usec" in stat else None
        )
        out["cpu_seconds_used"] = (
            round(stat["usage_usec"] / 1_000_000, 2) if "usage_usec" in stat else None
        )
    return out


def memory_snapshot() -> dict[str, Any]:
    """Memory ceiling, current use, and — the point of the whole exercise — the OOM count.

    `exited -9` is SIGKILL, and the OOM killer is the usual suspect. `memory.events` records
    whether it actually fired. A zero here would have ended I20's hour of guessing in seconds.
    """
    out: dict[str, Any] = {}
    current = _read_int(CGROUP_V2 / "memory.current")
    limit_raw = _read_text(CGROUP_V2 / "memory.max")
    if current is None:  # cgroup v1 fallback
        current = _read_int(CGROUP_V1_MEM / "memory.usage_in_bytes")
        limit_raw = _read_text(CGROUP_V1_MEM / "memory.limit_in_bytes")

    if current is not None:
        out["used_mb"] = round(current / 1_048_576, 1)
    if limit_raw and limit_raw != "max":
        try:
            limit = int(limit_raw)
            # cgroup v1 reports a sentinel near 2^63 for "no limit"; treat that as uncapped.
            if limit < (1 << 62):
                out["limit_mb"] = round(limit / 1_048_576, 1)
                if current is not None and limit:
                    out["used_pct"] = round(100 * current / limit, 1)
        except ValueError:
            pass

    events = _read_keyed(CGROUP_V2 / "memory.events")
    if events:
        out["oom_kills"] = events.get("oom_kill")
        out["oom_events"] = events.get("oom")
        # `high` counts throttling short of a kill — the warning before the bang.
        out["over_high_watermark"] = events.get("high")
    else:
        failcnt = _read_int(CGROUP_V1_MEM / "memory.failcnt")
        if failcnt is not None:
            out["allocation_failures"] = failcnt

    host = _read_keyed(Path("/proc/meminfo"))
    if host:
        out["host_total_mb"] = round(host.get("MemTotal", 0) / 1024, 1) or None
        out["host_available_mb"] = round(host.get("MemAvailable", 0) / 1024, 1) or None
        if host.get("SwapTotal"):
            out["host_swap_used_mb"] = round(
                (host.get("SwapTotal", 0) - host.get("SwapFree", 0)) / 1024, 1)
    return out


def disk_snapshot(preview_root: Optional[str] = None) -> dict[str, Any]:
    """Free space, and what the previews are holding.

    I39 could only be bounded from BELOW — "31 failed previews, each with a full npm install" —
    because nothing could measure it. This is that measurement.
    """
    out: dict[str, Any] = {}
    try:
        usage = shutil.disk_usage("/")
        out["total_gb"] = round(usage.total / 1_073_741_824, 1)
        out["free_gb"] = round(usage.free / 1_073_741_824, 1)
        out["used_pct"] = round(100 * usage.used / usage.total, 1) if usage.total else None
    except OSError:
        pass

    root = Path(preview_root or os.environ.get("BOSS_PREVIEW_ROOT", "/tmp/boss-previews"))
    if root.is_dir():
        total, dirs = 0, 0
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                dirs += 1
                for walk_root, _sub, files in os.walk(entry):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(walk_root, f))
                        except OSError:
                            continue
        except OSError:
            pass
        out["preview_dirs"] = dirs
        out["preview_mb"] = round(total / 1_048_576, 1)
    return out


def process_snapshot(limit: int = 8) -> dict[str, Any]:
    """What is running in this container, heaviest first.

    Answers "is a build running right now, and is it ours?" — the question that took an hour in
    I20 and is asked every time a process dies without explanation.
    """
    procs: list[dict[str, Any]] = []
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return {"available": False}
    try:
        clock = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        clock = 100
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        stat = _read_text(entry / "stat")
        if not stat:
            continue
        try:
            # comm can contain spaces and parens; everything after the final ')' is positional.
            after = stat[stat.rindex(")") + 2:].split()
            cpu_ticks = int(after[11]) + int(after[12])  # utime + stime
            rss_pages = int(after[21])
        except (ValueError, IndexError):
            continue
        cmdline = (_read_text(entry / "cmdline") or "").replace("\x00", " ").strip()
        procs.append({
            "pid": int(entry.name),
            "cpu_seconds": round(cpu_ticks / clock, 1),
            "rss_mb": round(rss_pages * 4096 / 1_048_576, 1),
            "cmd": (cmdline or _read_text(entry / "comm") or "?")[:120],
        })
    procs.sort(key=lambda p: p["cpu_seconds"], reverse=True)
    return {"available": True, "count": len(procs), "top": procs[:limit]}


def self_snapshot(preview_root: Optional[str] = None) -> dict[str, Any]:
    """Everything above, plus a plain-language reading of it.

    The verdict matters as much as the numbers: an operator asking "why did this die?" wants an
    answer, and a table of cgroup counters is not one.
    """
    cpu = cpu_snapshot()
    memory = memory_snapshot()
    disk = disk_snapshot(preview_root)
    processes = process_snapshot()

    notes: list[str] = []
    if not cpu.get("load_1m") and not memory.get("used_mb"):
        notes.append(
            "This platform exposes neither /proc/loadavg nor cgroup files (macOS in "
            "development, most likely), so nothing here was measured.")
    if (cpu.get("load_per_core") or 0) > 1.5:
        notes.append(
            f"CPU is oversubscribed: 1-minute load {cpu.get('load_1m')} across "
            f"{cpu.get('cores')} cores ({cpu.get('load_per_core')}x). Threads are waiting for a "
            f"timeslice, so request latency will be high without the API being busy.")
    if cpu.get("throttled_periods"):
        notes.append(
            f"The container hit its own CPU ceiling ({cpu['throttled_periods']} throttled "
            f"periods). That is a quota problem, not a load problem, and needs a different fix.")
    if memory.get("oom_kills"):
        notes.append(
            f"The OOM killer has fired {memory['oom_kills']} time(s) in this container. A "
            f"process that 'exited -9' with no other explanation was very likely this.")
    elif memory.get("oom_kills") == 0:
        notes.append(
            "No OOM kills recorded, so a SIGKILL here was NOT the kernel running out of "
            "memory — look for something in BOSS that sends signals.")
    if (memory.get("used_pct") or 0) > 85:
        notes.append(f"Memory is at {memory['used_pct']}% of the container limit.")
    if (disk.get("used_pct") or 0) > 85:
        notes.append(f"Disk is {disk['used_pct']}% full ({disk.get('free_gb')} GB free).")
    if (disk.get("preview_mb") or 0) > 1024:
        notes.append(
            f"Preview workdirs are holding {disk['preview_mb']} MB across "
            f"{disk.get('preview_dirs')} directories.")

    return {
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "processes": processes,
        "notes": notes or ["Nothing anomalous in CPU, memory or disk."],
    }


# --------------------------------------------------------------------------- #
# Delivery metrics
# --------------------------------------------------------------------------- #
# "Are these real or placeholder templates?" — a fair question about a tile reading exactly
# 100, which turned out to be a page size: the console fetched the 100 most recent receipts and
# counted them. These are computed in SQL over the whole window instead, so a number is either
# the real one or absent. Nothing here is sampled, capped, or estimated.

_METRIC_WINDOW_DAYS = 7


def delivery_metrics(connection, project_id: str, *, days: int = _METRIC_WINDOW_DAYS) -> dict:
    """Real counts for the work this project actually did in the last `days`."""
    from sqlalchemy import text as _sql

    def scalar(sql: str, **params):
        row = connection.execute(_sql(sql), {"p": project_id, "d": days, **params}).scalar()
        return row or 0

    since = "created_at > now() - make_interval(days => :d)"

    prs_opened = scalar(
        f"SELECT count(*) FROM code_task_runs "
        f"WHERE project_id = :p AND pull_request_url IS NOT NULL AND {since}")
    # NOT "merged". Nothing in this database records merge state: code_task_runs stores
    # pull_request_url and pull_request_number, and finalize_ctx has no merge key — checked,
    # rather than assumed, before naming this field. What CAN be counted is the run finishing
    # with a PR attached, which is a different claim, so it carries a different name. A real
    # merged-count needs GitHub, and inventing one from status would put a false number on a
    # dashboard people make decisions from.
    runs_completed_with_pr = scalar(
        f"SELECT count(*) FROM code_task_runs "
        f"WHERE project_id = :p AND pull_request_url IS NOT NULL "
        f"  AND status IN ('completed','succeeded','passed') AND {since}")
    runs_total = scalar(f"SELECT count(*) FROM code_task_runs WHERE project_id = :p AND {since}")
    runs_failed = scalar(
        f"SELECT count(*) FROM code_task_runs "
        f"WHERE project_id = :p AND status IN ('failed','error') AND {since}")
    jobs_ran = scalar(f"SELECT count(*) FROM jobs WHERE project_id = :p AND {since}")
    deploys = scalar(f"SELECT count(*) FROM deployment_runs WHERE project_id = :p AND {since}")

    micros = scalar(
        f"SELECT COALESCE(sum(cost_usd_micros), 0) FROM llm_calls WHERE project_id = :p AND {since}")
    tokens = scalar(
        f"SELECT COALESCE(sum(total_tokens), 0) FROM llm_calls WHERE project_id = :p AND {since}")
    # Machine time, from the latency the calls actually reported — not wall-clock, which would
    # count time the operator spent reading.
    latency_ms = scalar(
        f"SELECT COALESCE(sum(latency_ms), 0) FROM llm_calls WHERE project_id = :p AND {since}")

    return {
        "window_days": days,
        "prs_opened": int(prs_opened),
        "runs_completed_with_pr": int(runs_completed_with_pr),
        "prs_merged": None,
        "prs_merged_note": (
            "not tracked — this database stores the PR url and number but no merge state; "
            "a real merged-count requires querying GitHub"
        ),
        "runs_total": int(runs_total),
        "runs_failed": int(runs_failed),
        "run_success_rate": (
            round(100.0 * (runs_total - runs_failed) / runs_total, 1) if runs_total else None
        ),
        "jobs_ran": int(jobs_ran),
        "deployments": int(deploys),
        "spend_usd": round(int(micros) / 1_000_000, 2),
        "tokens": int(tokens),
        "agent_hours": round(int(latency_ms) / 3_600_000, 2),
    }
