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
#: /proc, as a constant so a test can stand a recorded machine in its place.
PROC = Path("/proc")


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

    raw = _read_text(PROC / "loadavg")
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


def measures_the_real_machine() -> bool:
    """Is /proc/meminfo the operator's machine, or a virtual machine standing in front of it?

    WHY THIS EXISTS. On a Linux server, a container's /proc/meminfo is the host's, and the
    memory bands mean what they say. On Docker Desktop there is a Linux VM in between, so the
    numbers describe the VM's slice and NOTHING about the machine the operator is sitting at.
    Measured on a Mac whose swap was 23,708 MB of 24,576 used -- 96.5%, minutes from unusable:

        BOSS reported   memory 42.4% ok   swap 2 MB ok   state: ok

    Both readings were correct about the VM and useless about the Mac. A green band that is
    read as "my machine is fine" is worse than no band, so the scope has to travel with the
    number.

    Detects Docker Desktop, whose kernel is linuxkit. Other VM-backed runtimes may present as
    an ordinary Linux kernel and are NOT detected -- this returns True for them, which keeps
    today's behaviour rather than inventing a claim about a setup we cannot identify.
    """
    version = _read_text(PROC / "version") or ""
    return "linuxkit" not in version.lower()


def _read_psi(path: Path) -> dict[str, float]:
    """Parse a PSI file: `some avg10=1.23 avg60=... total=...` / `full avg10=...`.

    Returns {"some_avg10": ..., "full_avg10": ...} for whichever lines exist.
    """
    out: dict[str, float] = {}
    raw = _read_text(path)
    if not raw:
        return out
    for line in raw.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        for field in parts[1:]:
            key, _, value = field.partition("=")
            if key == "avg10":
                try:
                    out[f"{parts[0]}_avg10"] = float(value)
                except ValueError:
                    pass
    return out


def memory_snapshot() -> dict[str, Any]:
    """Memory ceiling, current use, and — the point of the whole exercise — the OOM count.

    `exited -9` is SIGKILL, and the OOM killer is the usual suspect. `memory.events` records
    whether it actually fired. A zero here would have ended I20's hour of guessing in seconds.

    WHAT COUNTS AS "USED". Only memory the kernel cannot take back. On boss-1 (16 GB, Linux)
    the console said "out of memory" while `free -m` read 13,780 MB available: 13 GB of it was
    page cache, which the kernel hands back the moment anything asks. So:

      * the host figure is MemAvailable, never MemFree;
      * the cgroup figure is anon + unevictable from memory.stat, never memory.current, which
        includes the container's file cache and approaches any limit on a perfectly idle box;
      * `oom_kills` is CUMULATIVE since the cgroup was created — a fact about history, kept for
        "why did that process die?", and never on its own a statement about now. Current
        pressure is PSI (`psi_*`), and new kills are counted by `new_oom_kills`.
    """
    out: dict[str, Any] = {}
    current = _read_int(CGROUP_V2 / "memory.current")
    limit_raw = _read_text(CGROUP_V2 / "memory.max")
    stat = _read_keyed(CGROUP_V2 / "memory.stat")
    unreclaimable: Optional[int] = None
    if current is not None:
        if stat and "anon" in stat:
            unreclaimable = stat.get("anon", 0) + stat.get("unevictable", 0)
    else:  # cgroup v1 fallback
        current = _read_int(CGROUP_V1_MEM / "memory.usage_in_bytes")
        limit_raw = _read_text(CGROUP_V1_MEM / "memory.limit_in_bytes")
        stat = _read_keyed(CGROUP_V1_MEM / "memory.stat")
        if stat and ("total_rss" in stat or "rss" in stat):
            unreclaimable = (stat.get("total_rss", stat.get("rss", 0))
                             + stat.get("total_unevictable", stat.get("unevictable", 0)))

    if current is not None:
        out["used_mb"] = round(current / 1_048_576, 1)
    if unreclaimable is not None:
        out["unreclaimable_mb"] = round(unreclaimable / 1_048_576, 1)
    if limit_raw and limit_raw != "max":
        try:
            limit = int(limit_raw)
            # cgroup v1 reports a sentinel near 2^63 for "no limit"; treat that as uncapped.
            if limit < (1 << 62):
                out["limit_mb"] = round(limit / 1_048_576, 1)
                # Against the ceiling, only what cannot be reclaimed counts: cache is evicted
                # before the OOM killer is ever consulted.
                held = unreclaimable if unreclaimable is not None else current
                if held is not None and limit:
                    out["used_pct"] = round(100 * held / limit, 1)
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

    # Pressure stall information: the share of the last 10 s that tasks spent waiting on
    # memory. The host's own file first (it is not namespaced, so it IS the machine on a Linux
    # server), then the container's.
    psi = _read_psi(PROC / "pressure" / "memory") or _read_psi(CGROUP_V2 / "memory.pressure")
    if psi:
        out["psi_some_avg10"] = psi.get("some_avg10")
        out["psi_full_avg10"] = psi.get("full_avg10")

    host = _read_keyed(PROC / "meminfo")
    if host:
        out["host_total_mb"] = round(host.get("MemTotal", 0) / 1024, 1) or None
        out["host_available_mb"] = round(host.get("MemAvailable", 0) / 1024, 1) or None
        if host.get("SwapTotal"):
            out["host_swap_used_mb"] = round(
                (host.get("SwapTotal", 0) - host.get("SwapFree", 0)) / 1024, 1)
    # The `host_*` keys above are named for what they are on a server. On Docker Desktop they
    # describe a VM, so the reading has to say which it is or it will be read as the machine.
    out["host_is_the_machine"] = measures_the_real_machine()
    return out


#: Last oom_kill count seen per cgroup counter, so a verdict can ask "did the OOM killer fire
#: SINCE I LAST LOOKED", which a cumulative counter cannot answer on its own.
_OOM_SEEN: dict[str, int] = {}


def new_oom_kills(count: Optional[int], *, key: str = "self",
                  seen: Optional[dict[str, int]] = None) -> int:
    """OOM kills since the previous call. The first reading is a baseline and returns 0:
    a kill from last week must never become an alarm today."""
    store = _OOM_SEEN if seen is None else seen
    if count is None:
        return 0
    previous = store.get(key)
    store[key] = int(count)
    if previous is None or count < previous:  # first look, or the cgroup was recreated
        return 0
    return int(count) - previous


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
        # WHICH SOURCE IS RUNNING. Asked whether a fix was in the live API, an agent read the
        # source in the image — correctly — and then could not tell whether that image was
        # the one serving traffic, because `git_history` has no repository to read and nothing
        # else reported a version. The build stamps it.
        "build": _build_stamp(),
        "notes": notes or ["Nothing anomalous in CPU, memory or disk."],
    }


def _build_stamp() -> dict[str, Any]:
    """The commit this image was built from, as the build recorded it."""
    import os

    sha = (os.environ.get("BOSS_SOURCE_SHA") or "").strip()
    built = (os.environ.get("BOSS_BUILT_AT") or "").strip()
    return {
        "source_sha": sha or None,
        "built_at": built or None,
        "note": ("the source in this image is what is executing, so read_code answers 'is "
                 "the fix in the running build'" if sha or built else
                 "this image was built without a stamp, so the running commit is unknown; "
                 "read_code still shows the source that is executing"),
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
    # WHAT IS HAPPENING RIGHT NOW, from the work itself.
    #
    # The console's "Agents active" tile counted ROLES that own a task in an active column.
    # Only 23 of 187 tasks carry an owner_role_id at all, so it read 0/21 while four runs were
    # genuinely executing — honest arithmetic over a field nothing populates, which reads as
    # broken. These count the work, which is what the operator is actually asking about.
    runs_running = scalar(
        "SELECT count(*) FROM code_task_runs WHERE project_id = :p AND status = 'running'")
    jobs_queued = scalar(
        "SELECT count(*) FROM jobs WHERE project_id = :p AND status IN ('queued','leased')")
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
        "runs_running": int(runs_running),
        "jobs_queued": int(jobs_queued),
        "deployments": int(deploys),
        "spend_usd": round(int(micros) / 1_000_000, 2),
        "tokens": int(tokens),
        "agent_hours": round(int(latency_ms) / 3_600_000, 2),
    }


# --------------------------------------------------------------------------- #
# What did I just start, and where is it?
# --------------------------------------------------------------------------- #
# An operator pressed Build, saw "Working", and had no idea what happened next — the work was
# real and running, but nothing on screen said so or where to look. This answers both: the
# thing's current STAGE, and the console area that shows that stage.

_STAGE_AREA = {
    "discovery": "discovery",
    "meeting": "meetings",
    "coding": "code_tasks",
    "review": "decisions",
    "deployment": "delivery",
    "done": "code_tasks",
}


def in_flight(connection, project_id: str, *, limit: int = 8) -> list[dict]:
    """Recently started work, newest first, each with the stage it is actually in.

    Stage is derived from what EXISTS, not from a status column that may never have been
    updated: a deployment run means it is deploying, a code run means it is coding, a meeting
    means the panel is sitting. That ordering matters — the furthest-along artifact wins,
    because a task that reached deployment also has a meeting behind it.
    """
    from sqlalchemy import text as _sql

    rows = connection.execute(
        _sql(
            """
            SELECT t.id, t.title, t.status, t.created_at,
                   -- The PRD flow titles its task "PRD meeting: <directive>", and that is the
                   -- signal that actually EXISTS. meetings has no task_id (checked against
                   -- information_schema, after a first version failed on it), and its
                   -- action_task_ids array is not populated on this path -- verified against
                   -- live rows, where every count came back 0 while the runs were real.
                   (CASE WHEN t.title LIKE 'PRD meeting:%' THEN 1 ELSE 0 END)        AS meetings,
                   (SELECT count(*) FROM code_task_runs r WHERE r.task_id = t.id)    AS runs,
                   (SELECT count(*) FROM code_task_runs r
                     WHERE r.task_id = t.id AND r.status = 'running')                AS runs_active,
                   0                                                             AS deploys
            FROM tasks t
            WHERE t.project_id = :p
              AND t.created_at > now() - interval '3 days'
            ORDER BY t.created_at DESC
            LIMIT :lim
            """
        ),
        {"p": project_id, "lim": max(1, min(int(limit), 25))},
    ).mappings().all()

    out = []
    for r in rows:
        status = str(r["status"] or "")
        if int(r["runs_active"] or 0):
            stage = "coding"
        elif int(r["runs"] or 0):
            stage = "review" if status not in ("completed", "failed") else "done"
        elif int(r["meetings"] or 0):
            stage = "meeting"
        else:
            stage = "discovery"
        if status in ("completed", "failed", "cancelled"):
            stage = "done"
        out.append({
            "task_id": r["id"],
            "title": (str(r["title"] or "").splitlines() or [""])[0][:110],
            "status": status,
            "stage": stage,
            "area": _STAGE_AREA.get(stage, "code_tasks"),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "active": status not in ("completed", "failed", "cancelled"),
        })
    return out


# --------------------------------------------------------------------------------------- #
# STALL DETECTION — whether BOSS was able to RUN, which is not what the numbers above answer.
# --------------------------------------------------------------------------------------- #
#
# WHY THE SNAPSHOTS ABOVE ARE NOT ENOUGH.
#
# On 2026-09-20 a build took 85 minutes that had taken under 20 the day before, and the
# operator reasonably concluded BOSS had regressed. It had not. The API process froze twice —
# once for 13m56s — serving nothing at all, not even /health, because the MAC hosting Docker
# was thrashing: 21.4 GB of 22.5 GB swap in use, 23 million pageouts.
#
# Every reader above would have said "healthy" throughout. They see the Docker Desktop VM —
# 3.8 GB available, 1 GB swap, 281 GB free disk — and the VM was fine. The machine underneath
# it was not, and nothing inside a container can see that directly.
#
# So do not measure the host. Measure whether OUR OWN CODE GOT TO RUN. A loop that should wake
# every 20 seconds and wakes 14 minutes late was starved, and that conclusion holds whatever
# the cause — host swap, VM pause, CPU steal, a suspended laptop. It is the one signal that
# survives the container boundary, and it is exactly the thing an operator needs to know
# before blaming the pipeline.

#: A wake-up later than its due time by more than this is a stall, not jitter. Generous on
#: purpose: ordinary scheduling noise and a slow query are not what this is looking for.
STALL_THRESHOLD_SECONDS = 30.0


def stall_seconds(expected_interval: float, actual_elapsed: float) -> float:
    """How much longer than scheduled a wake-up took. 0.0 when it was on time.

    Pure so it can be tested without waiting: the caller owns the clock.
    """
    if expected_interval <= 0 or actual_elapsed <= 0:
        return 0.0
    late = actual_elapsed - expected_interval
    return round(late, 2) if late > STALL_THRESHOLD_SECONDS else 0.0


class StallTracker:
    """Records how long BOSS was unable to run, from inside BOSS.

    Fed by any loop with a known cadence. Keeps a bounded recent history so a run can be asked
    "how much of your elapsed time was the host, not you" — the question that would have
    settled an entire day of investigation in one line.
    """

    def __init__(self, keep: int = 50) -> None:
        self._keep = max(1, keep)
        self._events: list[dict[str, Any]] = []

    def observe(self, expected_interval: float, actual_elapsed: float,
                *, at: Optional[float] = None, source: str = "") -> float:
        """Record one wake-up. Returns the stall in seconds (0.0 if on time)."""
        late = stall_seconds(expected_interval, actual_elapsed)
        if late:
            self._events.append({"stalled_seconds": late, "at": at, "source": source,
                                 "expected_interval": expected_interval})
            del self._events[:-self._keep]
        return late

    def since(self, at: Optional[float]) -> dict[str, Any]:
        """Stalls recorded since a point in time — for attributing ONE run's lost minutes."""
        rows = [e for e in self._events
                if at is None or (e.get("at") is not None and e["at"] >= at)]
        total = round(sum(e["stalled_seconds"] for e in rows), 2)
        return {
            "stalled_seconds": total,
            "stall_events": len(rows),
            "longest_stall_seconds": max((e["stalled_seconds"] for e in rows), default=0.0),
        }

    def recent(self) -> list[dict[str, Any]]:
        return list(self._events)


def start_stall_watch(report, *, interval: float = 20.0, name: str = "api") -> Any:
    """Time a heartbeat IN THIS PROCESS and report when it is not scheduled.

    THE HALF-RIGHT VERSION OF THIS WATCHED THE WRONG PROCESS. The first stall detector timed
    the worker's poll loop — but coding agents run inside the API container, so on
    2026-09-20 the worker polled contentedly every 20 seconds for 67 minutes while the API was
    wedged and a build produced nothing. The measurement said "0 stalls" during a total stall.

    A loop that should wake every `interval` and wakes minutes late was starved, and that has
    to be measured in EVERY process that does work, not in whichever one was convenient. This
    is the API's own clock; the worker keeps its own.

    Returns the thread so a caller can keep a reference; it is a daemon and needs no shutdown.
    """
    import threading
    import time as _time

    tracker = StallTracker()

    def _watch() -> None:
        last = _time.monotonic()
        while True:
            _time.sleep(interval)
            now = _time.monotonic()
            late = tracker.observe(interval, now - last, at=_time.time(), source=name)
            last = now
            if late:
                try:
                    report(late)
                except Exception:  # noqa: BLE001 — never kill the watch over a failed report
                    pass

    thread = threading.Thread(target=_watch, name=f"stall-watch-{name}", daemon=True)
    thread.start()
    return thread


def host_is_degraded(summary: dict[str, Any], *, elapsed_seconds: float = 0.0) -> dict[str, Any]:
    """Turn stall measurements into a verdict an operator can act on.

    Deliberately NOT a judgement about BOSS. It answers one question — was this machine able
    to run the work — so a slow run can be attributed honestly instead of being read as the
    pipeline degrading.
    """
    stalled = float(summary.get("stalled_seconds") or 0.0)
    longest = float(summary.get("longest_stall_seconds") or 0.0)
    share = round(stalled / elapsed_seconds, 3) if elapsed_seconds > 0 else 0.0

    if longest >= 300 or share >= 0.25:
        state, why = "critical", "the host stopped scheduling BOSS for minutes at a time"
    elif stalled > 0:
        state, why = "degraded", "the host intermittently stopped scheduling BOSS"
    else:
        return {"state": "ok", "stalled_seconds": 0.0, "share_of_elapsed": 0.0,
                "detail": "BOSS ran whenever it was due to"}

    return {
        "state": state,
        "stalled_seconds": stalled,
        "longest_stall_seconds": longest,
        "share_of_elapsed": share,
        "detail": (f"{why}: {stalled:.0f}s lost across {summary.get('stall_events', 0)} "
                   f"stalls, longest {longest:.0f}s"
                   + (f", {share:.0%} of elapsed time" if share else "")),
        "not_boss": True,
    }


# --------------------------------------------------------------------------------------- #
# WHAT TO TELL THE PERSON LOOKING AT THE SCREEN.
# --------------------------------------------------------------------------------------- #
#
# "I got the system issue from you but there will be no one to give that info to customer."
#
# Exactly the point. Two runs were read as BOSS degrading when the machine was thrashing, and
# both times the diagnosis came from someone reading container logs by hand. A customer has
# nobody to do that. The verdict has to say what THEY should do, in words that do not assume
# they know what swap is.

#: Elapsed share above which a run's time is mostly not BOSS's doing.
_MOSTLY_THE_MACHINE = 0.4


def _cause(host: dict[str, Any], gauges: dict[str, Any]) -> str:
    """WHICH measurement raised the alarm. The headline may only name what was measured.

    The first version of this said "This computer is out of memory" for EVERY severe verdict,
    and most severe verdicts are not memory at all: stalls of BOSS's own heartbeat, a full disk,
    CPU load. On boss-1, a 16 GB Linux server with 13.7 GB available and load 0.4, it told the
    operator to restart a healthy machine.
    """
    bands = gauges.get("bands") or {}

    def bad(name: str) -> bool:
        return (bands.get(name) or {}).get("band", "ok") != "ok"

    if gauges.get("memory_exhausted") or bad("memory") or bad("swap"):
        return "memory"
    if bad("disk"):
        return "disk"
    if bad("cpu"):
        return "cpu"
    if gauges.get("scope") == "machine" and "memory" in bands:
        # A Linux server whose own gauges are all green, measured directly: the machine had
        # room. Whatever kept BOSS's loop from running, it was not this server running out.
        return "boss_process"
    return "stall"


def operator_advice(host: dict[str, Any], *, elapsed_minutes: float = 0.0,
                    working_minutes: float = 0.0,
                    gauges: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The host verdict as an instruction, not a metric.

    Returns {"show": bool, "severity": ..., "headline": ..., "detail": ..., "action": ...}.
    `show` is false on a healthy machine: an indicator that is always on is an indicator
    nobody reads.

    `gauges` is `pressure()`'s reading. It decides two things the stall evidence cannot:
    WHAT is short (memory, disk, cpu — or nothing on the machine at all), and WHERE BOSS runs.
    "Restart the computer" is advice for a laptop whose Docker Desktop VM is thrashing; on a
    Linux server (`scope == "machine"`) it is never given — restarting a server takes every
    service on it down and clears nothing that `free -m` / `docker stats` would not show.
    """
    state = str(host.get("state") or "ok")
    if state == "ok":
        return {"show": False}
    gauges = gauges or {}

    stalled_min = float(host.get("stalled_seconds") or 0) / 60
    share = float(host.get("share_of_elapsed") or 0)
    severe = state == "critical" or share >= _MOSTLY_THE_MACHINE
    scope = gauges.get("scope")  # "machine" (Linux server), "vm" (Docker Desktop), or unknown
    server = scope == "machine"
    noun = "server" if server else "computer"
    cause = _cause(host, gauges)

    if elapsed_minutes and working_minutes:
        detail = (f"This run took {elapsed_minutes:.0f} minutes, and about "
                  f"{working_minutes:.0f} of those were BOSS actually working. The rest is "
                  f"time the {noun} spent unable to run it.")
    elif stalled_min > 0:
        detail = (f"The {noun} stopped running BOSS for about {stalled_min:.0f} minutes "
                  f"in total, in stretches, while this was going on.")
    else:
        detail = ""
    if gauges.get("reasons"):
        detail = (detail + " Right now: " + ", ".join(gauges["reasons"]) + ".").strip()

    not_boss = True
    if cause == "memory":
        headline = (f"This {noun} is out of memory — BOSS is waiting on it" if severe
                    else f"This {noun} is running low on memory")
        if server:
            action = ("Find what is holding the memory (`free -m`, `docker stats`) and stop it, "
                      "or give the server more RAM. A restart is not needed.")
        elif scope == "vm":
            action = ("Restart the computer before the next run. Closing other applications "
                      "helps, but a restart is what reliably clears it." if severe else
                      "Close what you are not using. If it keeps happening, restart.")
        else:
            action = "Close what you are not using on the machine BOSS runs on."
    elif cause == "disk":
        headline = f"This {noun}'s disk is nearly full"
        action = ("Free disk space — `docker system prune` reclaims old images and build "
                  "cache — before the next run.")
    elif cause == "cpu":
        headline = (f"This {noun} is overloaded — BOSS is waiting on it" if severe
                    else f"This {noun} is busy")
        action = ("Find what is using the CPU (`top`, `docker stats`); run fewer builds at "
                  "once if it is BOSS's own." if server else
                  "Close what you are not using. If it keeps happening, restart.")
    elif cause == "boss_process":
        # Measured headroom on the server itself, so the honest statement is about BOSS.
        not_boss = False
        severe = False
        headline = "BOSS was slow to respond — this server has room to spare"
        action = ("Nothing to do on the server. If runs are slow, the API logs "
                  "(`docker compose logs api`) around this time show what held it up.")
    else:  # stalls with nothing measured to explain them: a VM, or an unknown platform
        headline = (f"This {noun} is too busy to run BOSS — BOSS is waiting on it" if severe
                    else f"This {noun} is struggling to keep up")
        if scope == "vm":
            action = ("Restart the computer before the next run. Closing other applications "
                      "helps, but a restart is what reliably clears it." if severe else
                      "Close what you are not using. If it keeps happening, restart.")
        else:
            action = "Close what you are not using on the machine BOSS runs on."

    return {
        "show": True,
        "severity": "critical" if severe else "warning",
        # Deliberately about the MACHINE, so nobody reads it as the product failing — except
        # when the machine was measured and was fine.
        "headline": headline,
        "detail": detail,
        "action": action,
        "cause": cause,
        "not_a_boss_fault": not_boss,
        "stalled_minutes": round(stalled_min, 1),
    }


# --------------------------------------------------------------------------------------- #
# PRESSURE THRESHOLDS — yellow at 85%, red at 95%.
# --------------------------------------------------------------------------------------- #
#
# WHERE THESE ARE TRUE, AND WHERE THEY LIE.
#
# /proc/meminfo inside a container reports the KERNEL the container shares. On a Linux VM —
# which is where BOSS is deployed — that kernel IS the host, so these percentages are the real
# thing and thresholds on them are exactly right.
#
# On Docker Desktop for Mac it is the Docker VM's kernel, not the Mac's. Measured 2026-09-20
# while the Mac had 0.1 GB free and 23 GB of swap in use: the container read 3.8 GB available
# of 7.8 GB — 51% used, comfortably "green" — through a total freeze. So these numbers must
# never be the ONLY signal. `stall_seconds` measures whether BOSS was scheduled at all, which
# is true everywhere, and the two are reported together: pressure explains, stalls prove.

PRESSURE_WARN = 0.85
PRESSURE_CRITICAL = 0.95

#: PSI `full avg10`: the share of the last 10 s in which EVERY task was stalled on memory.
#: Any sustained full stall is reclaim thrashing; 10% is unambiguous.
PSI_FULL_CRITICAL = 10.0


def _band(fraction: float) -> str:
    if fraction >= PRESSURE_CRITICAL:
        return "critical"
    return "warning" if fraction >= PRESSURE_WARN else "ok"


def pressure(memory: dict[str, Any], cpu: dict[str, Any],
             disk: dict[str, Any]) -> dict[str, Any]:
    """Memory, CPU and disk as bands, from snapshots this module already takes.

    CPU is load PER CORE rather than a utilisation percentage: a box with load 12 on 12 cores
    is saturated, and the same load on 48 cores is idle. 1.0 per core is full, so the same
    85/95 bands apply to it directly.
    """
    out: dict[str, Any] = {"bands": {}, "state": "ok", "reasons": []}

    # MEMORY IS WHAT CANNOT BE HANDED BACK. Host: 1 - MemAvailable/MemTotal (page cache is
    # available). Container: anon + unevictable against its own limit, when it has one. The
    # worse of the two is the band. Cumulative counters never enter it; see memory_snapshot.
    total = float(memory.get("host_total_mb") or 0)
    available = float(memory.get("host_available_mb") or 0)
    fractions: list[float] = []
    if total > 0 and memory.get("host_available_mb") is not None:
        fractions.append(max(0.0, min(1.0, 1 - available / total)))
    if memory.get("limit_mb") and memory.get("used_pct") is not None:
        fractions.append(max(0.0, min(1.0, float(memory["used_pct"]) / 100)))
    exhausted = False
    if fractions:
        used = max(fractions)
        out["bands"]["memory"] = {"used_pct": round(used * 100, 1), "band": _band(used)}
        if _band(used) != "ok":
            out["reasons"].append(f"memory {used:.0%} used")
        exhausted = _band(used) == "critical"
    psi_full = memory.get("psi_full_avg10")
    if psi_full is not None and float(psi_full) >= PSI_FULL_CRITICAL:
        exhausted = True
        out["bands"].setdefault("memory", {"used_pct": None, "band": "critical"})
        out["bands"]["memory"]["band"] = "critical"
        out["reasons"].append(f"memory stalls {float(psi_full):.0f}% of the time")
    if memory.get("oom_kills_new"):
        exhausted = True
        out["bands"].setdefault("memory", {"used_pct": None, "band": "critical"})
        out["bands"]["memory"]["band"] = "critical"
        out["reasons"].append(f"the OOM killer fired {memory['oom_kills_new']} time(s) just now")
    out["memory_exhausted"] = exhausted

    per_core = memory.get("host_swap_used_mb")
    if per_core is not None and total > 0:
        swap = float(per_core)
        # Swap in use at all is a symptom; the size of it relative to RAM is the severity.
        if swap > 0:
            out["bands"]["swap"] = {"used_mb": round(swap, 1),
                                    "band": _band(min(1.0, swap / total))}
            if out["bands"]["swap"]["band"] != "ok":
                out["reasons"].append(f"swapping ({swap:.0f} MB)")

    load = cpu.get("load_per_core")
    if load is not None:
        out["bands"]["cpu"] = {"load_per_core": round(float(load), 2),
                               "band": _band(float(load))}
        if _band(float(load)) != "ok":
            out["reasons"].append(f"cpu load {float(load):.2f} per core")

    used_pct = disk.get("used_pct")
    if used_pct is not None:
        out["bands"]["disk"] = {"used_pct": round(float(used_pct), 1),
                                "band": _band(float(used_pct) / 100)}
        if _band(float(used_pct) / 100) != "ok":
            out["reasons"].append(f"disk {float(used_pct):.0f}% full")

    bands = [b["band"] for b in out["bands"].values()]
    out["state"] = ("critical" if "critical" in bands
                    else "warning" if "warning" in bands else "ok")

    # WHAT THESE NUMBERS ARE ABOUT. Read from the memory snapshot rather than taken as an
    # argument, so every existing caller of pressure() keeps working unchanged.
    #
    # Memory and swap come from /proc/meminfo, which behind Docker Desktop belongs to a VM.
    # Those two bands are then about BOSS's slice and not about the operator's machine, and
    # they will read `ok` while the machine underneath is out of swap. Disk and CPU load are
    # equally the VM's. Saying so is the whole fix: the thresholds were never wrong.
    out["scope"] = "machine" if memory.get("host_is_the_machine", True) else "vm"
    if out["scope"] == "vm":
        out["scope_note"] = (
            "These are BOSS's virtual machine, not this computer — Docker Desktop sits in "
            "between, so a green band here says nothing about the machine you are using. "
            "Whether BOSS is being starved is measured separately, by how late its own loop "
            "runs."
        )
    return out


def pressure_now(preview_root: Optional[str] = None) -> dict[str, Any]:
    """Current pressure, read fresh. Never raises: every reader degrades to absent."""
    try:
        memory = memory_snapshot()
        # Only kills since the last look are evidence about NOW.
        memory["oom_kills_new"] = new_oom_kills(memory.get("oom_kills"))
        return pressure(memory, cpu_snapshot(), disk_snapshot(preview_root))
    except Exception:  # noqa: BLE001
        return {"bands": {}, "state": "ok", "reasons": []}
