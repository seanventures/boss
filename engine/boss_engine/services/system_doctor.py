"""THE SYSTEM DOCTOR: Shepherd and Metatron watch the machine, not only the runs (spec §4c-§4d).

The operator's goal: customers must not face random issues — slow hardware is fine, but BOSS
must not leak and must use the machine's capacity well. So every 15 s, on its own thread in the
API (the worker's loop is blind while a build runs), the doctor:

1. reads the signals the kernel and BOSS itself give — memory pressure (PSI), available memory,
   swap, OOM kills, disk, the API's own heartbeat stalls, stuck jobs, orphaned processes and
   zombies, stale previews, database connections, deploy churn, and (on a Mac) the host sample
   the optional helper posts;
2. turns them into FINDINGS (`system_findings`), one open row per kind;
3. fixes what is safe ON ITS OWN — scoped to what BOSS created, idempotent, logged, at most once
   per 5 minutes per target and 20 an hour, with a circuit breaker: three tries that do not
   improve the signal and the action becomes an option for a person instead;
4. OFFERS everything that would stop a person's work as 2-3 options on a `system_health` card in
   What needs you, in the words of the spec (§4d) — never doing it unasked;
5. sets the degradation rung admission and Metatron obey (L0-L3).

It never deletes user repos, volumes, databases or non-`boss-*` containers, never runs
`docker system prune -a` or `--volumes`, and never kills user work on its own — except a build
whose own container hit its own memory cap, which is that build's own OOM.

Every action, automatic or chosen, is an `events` row (`system_doctor.acted`) and a
`system_doctor_actions` row with the metrics before and, 60 s later, after.
"""
from __future__ import annotations

import json
import re
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

from . import admission, owned_process

_log = logging.getLogger("boss.system_doctor")

INTERVAL_SECONDS = 15.0
CLEAR_FOR = timedelta(minutes=2)
PER_TARGET = timedelta(minutes=5)
PER_HOUR = 20
BREAKER_RUNS = 3
VERIFY_AFTER = timedelta(seconds=60)
STUCK_NO_HEARTBEAT = timedelta(minutes=3)
STUCK_NO_OUTPUT = timedelta(minutes=15)
DOCKER_MEMORY_GUIDE = "https://docs.docker.com/desktop/settings-and-maintenance/settings/#resources"

#: Options that stop or cancel something a person started: the console asks to confirm.
CONFIRM_OPTIONS = frozenset({"pause_largest_build", "cancel_build"})

#: Every action the doctor may take ON ITS OWN. Anything that stops a person's work is NOT
#: here — it is only ever an option on the card (enforced by test_the_doctor_never_stops_work).
AUTO_ACTIONS = frozenset({
    "reap_previews", "end_orphaned_processes", "remove_orphan_containers", "sweep_temp_dirs",
    "prune_boss_disk", "set_degradation_level", "stop_idle_previews",
    "close_idle_transactions",
})
#: Every option a card may offer. Destructive ones (stop, cancel) must never be automatic.
OFFERED_OPTIONS = frozenset({
    "queue_new_builds", "pause_largest_build", "keep_going", "one_build_at_a_time",
    "docker_memory_guide", "dismiss_today", "free_disk_now", "remind_at_90", "restart_step",
    "cancel_build", "wait_15", "hold_updates", "keep_updating", "build_one_at_a_time",
    "use_bigger_place", "acknowledge", "answer_escalation",
})
DESTRUCTIVE = frozenset({"pause_largest_build", "cancel_build", "restart_step",
                         "free_disk_now"})


# ------------------------------------------------------------------------------------------ #
# signals
# ------------------------------------------------------------------------------------------ #

@dataclass
class Signals:
    cap: admission.Capacity
    level: int = 0
    oom_kills_new: int = 0
    swap_out_mb_s: float = 0.0
    zombies: int = 0
    pid1_is_init: bool = True
    orphan_trees: int = 0
    builds: list[dict[str, Any]] = field(default_factory=list)    # running builds here
    boss_mb: float = 0.0
    used_mb: float = 0.0
    stuck: list[dict[str, Any]] = field(default_factory=list)
    db: dict[str, Any] = field(default_factory=dict)
    deploy_restarts_hour: int = 0
    deploy_hold: bool = False
    api_stall_seconds: float = 0.0
    host_sample: Optional[dict[str, Any]] = None
    stale_previews: int = 0
    reclaimable_disk_gb: Optional[float] = None
    biggest_request_mb: float = 0.0
    docker: bool = False


_VM_LAST: dict[str, Any] = {}


def _vmstat_deltas() -> tuple[int, float]:
    from .self_observability import vmstat_snapshot

    now = time.monotonic()
    snap = vmstat_snapshot()
    prev = _VM_LAST.get("snap")
    at = _VM_LAST.get("at")
    _VM_LAST.update({"snap": snap, "at": now})
    if not prev or not at or not snap:
        return 0, 0.0
    ooms = max(0, int(snap.get("oom_kill", 0)) - int(prev.get("oom_kill", 0)))
    pages = max(0, int(snap.get("pswpout", 0)) - int(prev.get("pswpout", 0)))
    seconds = max(1e-3, now - at)
    return ooms, round(pages * 4096 / 1e6 / seconds, 2)


def _pid1_is_init() -> bool:
    try:
        with open("/proc/1/comm", encoding="utf-8") as fh:
            comm = fh.read().strip()
    except OSError:
        return True
    return comm in ("tini", "docker-init", "dumb-init", "init", "systemd", "s6-svscan")


def _rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576
    except (OSError, ValueError, IndexError):
        return 0.0


def running_builds_here() -> list[dict[str, Any]]:
    """Each build with live processes in THIS API, and the memory its trees hold."""
    by_run: dict[str, float] = {}
    for proc in owned_process.live():
        o = proc.owner
        if o is None:
            continue
        run_id = o.id if o.kind == "code_run" else next(
            (i for i in o.chain), None) if o.kind == "review_seat" else None
        if not run_id:
            continue
        pids = owned_process.members(proc.pgid, proc.token)["alive"]
        by_run[run_id] = by_run.get(run_id, 0.0) + sum(_rss_mb(p) for p in pids)
    return [{"run_id": r, "mem_mb": round(mb, 1)} for r, mb in
            sorted(by_run.items(), key=lambda kv: -kv[1])]


def _state(connection: Any, key: str) -> dict[str, Any]:
    return admission._state(connection, key)


def collect(engine: Any) -> Signals:
    cap = admission.sample()
    ooms, swap_rate = _vmstat_deltas()
    census = owned_process.processes() if owned_process._proc_available() else []
    zombies = owned_process.zombie_count(census) if census else 0
    builds = running_builds_here()
    boss_mb = sum(b["mem_mb"] for b in builds) + _rss_mb(os.getpid())
    sig = Signals(cap=cap, oom_kills_new=ooms, swap_out_mb_s=swap_rate, zombies=zombies,
                  pid1_is_init=_pid1_is_init(), builds=builds, boss_mb=round(boss_mb, 1),
                  used_mb=max(0.0, cap.total_mb - cap.available_mb))
    try:
        sig.orphan_trees = len(owned_process.orphan_trees())
    except Exception:  # noqa: BLE001
        sig.orphan_trees = 0
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        sig.level = admission.current_level(c, cap)
        names = {}
        if builds:
            for r in c.execute(text("SELECT id, title FROM code_task_runs WHERE id = ANY(:ids)"),
                               {"ids": [b["run_id"] for b in builds]}).mappings():
                names[str(r["id"])] = (str(r["title"] or "").splitlines() or [""])[0][:60]
        for b in builds:
            b["name"] = names.get(b["run_id"]) or b["run_id"]
        # STUCK: no heartbeat for 3 minutes, or heartbeating while its agents have said
        # nothing for 15 (the last agent turn, or the run's own update when it has none).
        sig.stuck = [dict(r) for r in c.execute(text(
            "SELECT * FROM (SELECT j.id AS job_id, j.payload->>'run_id' AS run_id, "
            "j.heartbeat_at, r.title, r.phase, GREATEST(r.updated_at, (SELECT max(t.created_at) "
            "FROM agent_turns t WHERE t.code_task_run_id = r.id)) AS updated_at FROM jobs j "
            "LEFT JOIN code_task_runs r ON r.id = j.payload->>'run_id' "
            "WHERE j.kind = 'code.task' AND j.status IN ('leased', 'running')) s "
            "WHERE s.heartbeat_at < :hb OR s.updated_at < :out "
            "ORDER BY LEAST(s.heartbeat_at, s.updated_at) LIMIT 5"),
            {"hb": now - STUCK_NO_HEARTBEAT, "out": now - STUCK_NO_OUTPUT}).mappings()]
        try:
            with c.begin_nested():
                db = c.execute(text(
                    "SELECT (SELECT count(*) FROM pg_stat_activity WHERE datname = "
                    "current_database()) AS conns, current_setting('max_connections')::int AS "
                    "max_conns, (SELECT count(*) FROM pg_stat_activity WHERE state = "
                    "'idle in transaction' AND datname = current_database() AND "
                    "state_change < now() - interval '10 minutes') AS idle_tx_long"
                )).mappings().first()
                sig.db = dict(db or {})
        except Exception:  # noqa: BLE001
            sig.db = {}
        sig.deploy_restarts_hour = int(c.execute(text(
            "SELECT count(*) FROM events WHERE event_type IN "
            "('code_task.interrupted_by_deploy', 'job.handed_back', 'job.lease_reclaimed') "
            "AND created_at > now() - interval '1 hour'")).scalar() or 0)
        sig.deploy_hold = bool(_state(c, "deploy_hold").get("hold"))
        sig.api_stall_seconds = float(c.execute(text(
            "SELECT COALESCE(max((data->>'stalled_seconds')::numeric), 0) FROM events "
            "WHERE event_type = 'host.stalled' AND data->>'source' = 'api' "
            "AND created_at > now() - interval '5 minutes'")).scalar() or 0)
        sample = _state(c, "host_sample")
        at = sample.get("at")
        if at:
            try:
                fresh = now - datetime.fromisoformat(str(at)) < timedelta(minutes=10)
            except ValueError:
                fresh = False
            sig.host_sample = sample if fresh else None
        sig.stale_previews = int(c.execute(text(
            "SELECT count(*) FROM app_previews WHERE status = 'running' AND "
            "expires_at < now()")).scalar() or 0)
        sig.biggest_request_mb = float(admission.load_profile(c, "code.task").request_mb)
    from .resource_reaper import docker_available

    sig.docker = docker_available()
    if sig.docker and cap.disk_used_pct > 80:
        sig.reclaimable_disk_gb = _docker_reclaimable_gb()
    return sig


def _docker_reclaimable_gb() -> Optional[float]:
    """What `docker system df` says images and build cache could give back (BOSS's estimate
    for the disk card; the prune itself removes only BOSS's own)."""
    try:
        out = owned_process.run(["docker", "system", "df", "--format",
                                 "{{.Type}}\t{{.Reclaimable}}"], capture_output=True,
                                text=True, timeout=60).stdout
    except Exception:  # noqa: BLE001
        return None
    total = 0.0
    for line in out.splitlines():
        kind, _, value = line.partition("\t")
        if kind not in ("Images", "Build Cache"):
            continue
        number = value.split(" ", 1)[0].strip()
        for unit, scale in (("GB", 1.0), ("MB", 1 / 1000), ("kB", 1 / 1e6), ("B", 1 / 1e9)):
            if number.endswith(unit):
                try:
                    total += float(number[: -len(unit)]) * scale
                except ValueError:
                    pass
                break
    return round(total, 1) if total else None


# ------------------------------------------------------------------------------------------ #
# diagnosis: pure
# ------------------------------------------------------------------------------------------ #

def _gb(mb: float) -> str:
    return f"{mb / 1024:.1f}"


def _opt(key: str, label: str, tip: str) -> dict[str, Any]:
    return {"key": key, "label": label, "tip": tip, "confirm": key in CONFIRM_OPTIONS}


def diagnose(sig: Signals) -> list[dict[str, Any]]:
    """Signals → findings. Each: kind, severity, title, detail, evidence, options,
    needs_choice, auto (actions BOSS takes itself). The copy is the spec's (§4d)."""
    out: list[dict[str, Any]] = []
    cap = sig.cap
    level = admission.level_of(cap)
    host = sig.host_sample or {}
    mac_swap_frac = (float(host.get("swap_used_mb") or 0) / float(host["swap_total_mb"])
                     if host.get("swap_total_mb") else 0.0)
    pressured = level >= admission.L1 or sig.oom_kills_new > 0 or mac_swap_frac > 0.7
    if pressured:
        boss_share = sig.boss_mb / sig.used_mb if sig.used_mb else 0.0
        evidence = {"available_mb": round(cap.available_mb), "total_mb": round(cap.total_mb),
                    "psi_full_avg60": cap.psi_full_avg60, "psi_full_avg10": cap.psi_full_avg10,
                    "boss_mb": sig.boss_mb, "used_mb": round(sig.used_mb),
                    "oom_kills_new": sig.oom_kills_new, "builds": sig.builds,
                    "scope": cap.scope, "host_sample": host or None}
        severity = "critical" if (level >= admission.L2 or sig.oom_kills_new) else "warn"
        if sig.builds and boss_share >= 0.30 and mac_swap_frac <= 0.7:
            biggest = sig.builds[0]
            builds_gb = sum(b["mem_mb"] for b in sig.builds)
            out.append({
                "kind": "memory_pressure_from_builds", "severity": severity,
                "title": "Builds are running out of memory",
                "detail": (f"{len(sig.builds)} build{'s' if len(sig.builds) != 1 else ''} "
                           f"are using {_gb(builds_gb)} of {_gb(cap.total_mb)} GB; the machine "
                           f"has been waiting on memory {cap.psi_full_avg60:.0f}% of the last "
                           "minute."),
                "evidence": {**evidence, "largest": biggest},
                "options": [
                    _opt("queue_new_builds", "Queue new builds (recommended)",
                         "Run one build at a time until memory frees up. Nothing running is "
                         "stopped."),
                    _opt("pause_largest_build", f"Pause “{biggest['name']}” build",
                         f"Stops the largest build ({_gb(biggest['mem_mb'])} GB). It resumes "
                         "from its last step when you press Resume."),
                    _opt("keep_going", "Keep going",
                         "Change nothing. Builds may slow down or fail."),
                ],
                "needs_choice": True, "auto": ["set_degradation_level", "stop_idle_previews"],
            })
        else:
            swap_used = float(host.get("swap_used_mb") or cap.swap_used_mb)
            swap_total = float(host.get("swap_total_mb") or 0)
            tops = host.get("top") or []
            consumers = ", ".join(f"{t.get('name')} {_gb(float(t.get('mb') or 0))} GB"
                                  for t in tops[:2])
            parts = []
            if swap_total:
                parts.append(f"Swap {_gb(swap_used)} of {_gb(swap_total)} GB used.")
            else:
                parts.append(f"{_gb(cap.available_mb)} of {_gb(cap.total_mb)} GB free.")
            parts.append(", ".join(x for x in (consumers, f"BOSS {_gb(sig.boss_mb)} GB") if x)
                         + ".")
            out.append({
                "kind": "memory_pressure_external", "severity": severity,
                "title": "Your computer is low on memory, and most of it is not BOSS",
                "detail": " ".join(parts),
                "evidence": evidence,
                "options": [
                    _opt("one_build_at_a_time", "Run one build at a time",
                         "BOSS starts one build at a time until memory frees up."),
                    _opt("docker_memory_guide", "Give Docker less memory: how",
                         "Opens a guide to Docker Desktop's memory setting."),
                    _opt("dismiss_today", "Dismiss for today", "Hide this until tomorrow."),
                ],
                "needs_choice": True, "auto": ["set_degradation_level", "stop_idle_previews"],
            })

    if cap.disk_used_pct > 80 or (0 < cap.disk_free_gb < 3):
        reclaim = sig.reclaimable_disk_gb
        free_label = f"{reclaim:.0f} GB" if reclaim else "space"
        critical = cap.disk_used_pct > 90 or (0 < cap.disk_free_gb < 3)
        out.append({
            "kind": "disk_filling", "severity": "critical" if critical else "warn",
            "title": f"Disk is {cap.disk_used_pct:.0f}% full and grows ~1.3 GB per update",
            "detail": (f"BOSS can free {free_label} of old build cache and images it made."
                       if not critical else
                       "BOSS has already freed its own old build cache and images."),
            "evidence": {"used_pct": cap.disk_used_pct, "free_gb": cap.disk_free_gb,
                         "reclaimable_gb": reclaim},
            "options": ([
                _opt("free_disk_now", f"Free {free_label} now",
                     "Removes only BOSS's own old builds; your apps and data are untouched."),
                _opt("remind_at_90", "Remind me at 90%", "Hide this until the disk is 90% full."),
            ] if not critical else [_opt("acknowledge", "OK", "Close this notice.")]),
            "needs_choice": True, "auto": ["prune_boss_disk"] if critical else [],
        })

    if sig.stuck:
        s = sig.stuck[0]
        last = s.get("updated_at") or s.get("heartbeat_at")
        minutes = 0
        if last is not None:
            last_aware = last if getattr(last, "tzinfo", None) else last.replace(tzinfo=timezone.utc)
            minutes = int((datetime.now(timezone.utc) - last_aware).total_seconds() // 60)
        name = (str(s.get("title") or "").splitlines() or [""])[0][:60] or str(s.get("run_id"))
        since = last.strftime("%H:%M") if last is not None else "a while"
        out.append({
            "kind": "stuck_job", "severity": "warn",
            "title": f"“{name}” build has not moved for {minutes} min",
            "detail": f"Last activity: {s.get('phase') or 'unknown'}. No output since {since}.",
            "evidence": {"stuck": [{k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                                    for k, v in x.items()} for x in sig.stuck]},
            "options": [_opt("restart_step", "Restart this step",
                             "Hands the build back to the queue; it restarts the step it was on."),
                        _opt("cancel_build", "Cancel build", "Stops the build."),
                        _opt("wait_15", "Wait 15 more minutes", "Ask again in 15 minutes.")],
            "needs_choice": True, "auto": [],
            "target": s.get("run_id"),
        })

    if sig.deploy_restarts_hour >= 2 and not sig.deploy_hold:
        out.append({
            "kind": "deploy_churn_killing_builds",
            "severity": "critical" if sig.deploy_restarts_hour >= 4 else "warn",
            "title": f"Updates restarted a build {sig.deploy_restarts_hour} times in an hour",
            "detail": "Each restart redid clone and install (~6 min).",
            "evidence": {"restarts_last_hour": sig.deploy_restarts_hour},
            "options": [_opt("hold_updates", "Hold updates while builds run (recommended)",
                             "Updates wait for running builds to finish (up to 30 min)."),
                        _opt("keep_updating", "Keep updating immediately",
                             "Updates go ahead; builds resume after each one.")],
            "needs_choice": True, "auto": [],
        })

    spare_mb = cap.total_mb - cap.reserve_mb
    if cap.total_mb and sig.biggest_request_mb > spare_mb:
        out.append({
            "kind": "host_too_small_for_workload", "severity": "warn",
            "title": (f"This app needs about {_gb(sig.biggest_request_mb)} GB to build; "
                      f"this machine has {_gb(max(0.0, spare_mb))} GB to spare"),
            "detail": "",
            "evidence": {"request_mb": sig.biggest_request_mb, "spare_mb": round(spare_mb)},
            "options": [_opt("build_one_at_a_time", "Build anyway, one at a time",
                             "BOSS runs one build at a time and lets it use what there is."),
                        _opt("use_bigger_place", "Use a bigger place",
                             "Opens Places, to build on a machine with more memory.")],
            "needs_choice": True, "auto": [],
        })

    # BOSS fixes these itself; they never become a card.
    if sig.orphan_trees:
        out.append({"kind": "leaked_processes", "severity": "warn",
                    "title": f"{sig.orphan_trees} process tree(s) outlived their build",
                    "detail": "", "evidence": {"orphan_trees": sig.orphan_trees},
                    "options": [], "needs_choice": False, "auto": ["end_orphaned_processes"]})
    if sig.zombies and not sig.pid1_is_init:
        out.append({"kind": "zombie_processes", "severity": "warn",
                    "title": f"{sig.zombies} finished processes are never collected",
                    "detail": ("This container runs without an init. Add `init: true` to the "
                               "api service in the compose file (BOSS's own file has it)."),
                    "evidence": {"zombies": sig.zombies}, "options": [],
                    "needs_choice": False, "auto": []})
    if sig.stale_previews:
        out.append({"kind": "stale_previews", "severity": "info",
                    "title": f"{sig.stale_previews} preview(s) ran past their time",
                    "detail": "", "evidence": {"stale": sig.stale_previews}, "options": [],
                    "needs_choice": False, "auto": ["reap_previews"]})
    if int(sig.db.get("idle_tx_long") or 0):
        out.append({"kind": "db_connection_leak", "severity": "warn",
                    "title": "Database sessions held a transaction open for over 10 minutes",
                    "detail": "", "evidence": dict(sig.db), "options": [],
                    "needs_choice": False, "auto": ["close_idle_transactions"]})
    elif sig.db.get("max_conns") and int(sig.db.get("conns") or 0) > \
            0.85 * int(sig.db["max_conns"]):
        out.append({"kind": "db_connection_leak", "severity": "critical",
                    "title": "The database is close to its connection limit",
                    "detail": f"{sig.db.get('conns')} of {sig.db.get('max_conns')} in use.",
                    "evidence": dict(sig.db), "options": [], "needs_choice": False,
                    "auto": ["close_idle_transactions"]})
    return out


def target_level(sig: Signals, previous: dict[str, Any], now: float) -> dict[str, Any]:
    """The rung, made sticky: L2 held for 60 s (or a new OOM kill) is L3."""
    level = admission.level_of(sig.cap)
    since = previous.get("critical_since")
    if level >= admission.L2:
        since = since or now
        if now - float(since) >= 60 or sig.oom_kills_new:
            level = admission.L3
    else:
        since = None
    return {"level": level, "critical_since": since, "at": now,
            "words": admission.LEVEL_WORDS.get(level)}


# ------------------------------------------------------------------------------------------ #
# persistence
# ------------------------------------------------------------------------------------------ #

def _upsert_findings(connection: Any, found: list[dict[str, Any]],
                     now: datetime) -> dict[str, str]:
    """Open/refresh one row per kind; start the clear clock on the ones not seen; resolve after
    two clear minutes. Returns {kind: id} for what is open."""
    kinds = {f["kind"] for f in found}
    ids: dict[str, str] = {}
    for f in found:
        row = connection.execute(text(
            "SELECT id, status, snoozed_until FROM system_findings WHERE kind = :k AND status "
            "IN ('open', 'snoozed')"), {"k": f["kind"]}).mappings().first()
        payload = {"t": f["title"][:300], "d": (f.get("detail") or "")[:1000],
                   "e": json.dumps(f.get("evidence") or {}, default=str),
                   "o": json.dumps(f.get("options") or []), "n": bool(f.get("needs_choice")),
                   "s": f.get("severity") or "warn"}
        if row is None:
            fid = f"sf_{uuid.uuid4().hex[:16]}"
            connection.execute(text(
                "INSERT INTO system_findings (id, kind, severity, title, detail, evidence, "
                "options, needs_choice) VALUES (:id, :k, :s, :t, :d, CAST(:e AS JSONB), "
                "CAST(:o AS JSONB), :n)"), {"id": fid, "k": f["kind"], **payload})
        else:
            fid = str(row["id"])
            connection.execute(text(
                "UPDATE system_findings SET severity = :s, title = :t, detail = :d, "
                "evidence = CAST(:e AS JSONB), options = CAST(:o AS JSONB), needs_choice = "
                "CASE WHEN chosen_option IS NOT NULL THEN needs_choice ELSE :n END, "
                "last_seen = now(), clear_since = NULL, status = CASE WHEN status = 'snoozed' "
                "AND snoozed_until <= now() THEN 'open' ELSE status END WHERE id = :id"),
                {"id": fid, **payload})
        ids[f["kind"]] = fid
    for row in connection.execute(text(
            "SELECT id, kind, clear_since FROM system_findings WHERE status IN "
            "('open', 'snoozed')")).mappings():
        if row["kind"] in kinds:
            continue
        if row["clear_since"] is None:
            connection.execute(text("UPDATE system_findings SET clear_since = :n WHERE id = "
                                    ":id"), {"n": now, "id": row["id"]})
        else:
            cleared = row["clear_since"]
            cleared = cleared if cleared.tzinfo else cleared.replace(tzinfo=timezone.utc)
            if now - cleared >= CLEAR_FOR:
                connection.execute(text(
                    "UPDATE system_findings SET status = 'resolved', resolved_at = now() "
                    "WHERE id = :id"), {"id": row["id"]})
                _on_resolved(connection, str(row["id"]), str(row["kind"]))
    return ids


def _on_resolved(connection: Any, finding_id: str, kind: str) -> None:
    """A temporary choice ends with the trouble that prompted it: "one at a time until memory
    frees up" goes back to auto when it has."""
    pin = admission._state(connection, "builds_at_once")
    if pin.get("until_finding") == finding_id:
        admission.set_state(connection, "builds_at_once", {"value": None})


def _metrics(sig: Signals) -> dict[str, Any]:
    return {"available_mb": round(sig.cap.available_mb), "psi_full_avg60": sig.cap.psi_full_avg60,
            "disk_used_pct": sig.cap.disk_used_pct, "orphan_trees": sig.orphan_trees,
            "stale_previews": sig.stale_previews, "zombies": sig.zombies,
            "idle_tx_long": int(sig.db.get("idle_tx_long") or 0)}


def _improved(action: str, before: dict[str, Any], after: dict[str, Any]) -> bool:
    key = {"end_orphaned_processes": "orphan_trees", "reap_previews": "stale_previews",
           "stop_idle_previews": "available_mb", "prune_boss_disk": "disk_used_pct",
           "close_idle_transactions": "idle_tx_long", "set_degradation_level": "psi_full_avg60",
           "remove_orphan_containers": "available_mb", "sweep_temp_dirs": "disk_used_pct",
           }.get(action, "available_mb")
    b, a = before.get(key), after.get(key)
    if b is None or a is None:
        return True
    return (a > b) if key == "available_mb" else (a <= b if b == 0 else a < b)


def record_action(connection: Any, *, finding_id: Optional[str], kind: Optional[str],
                  action: str, target: str, actor: str, automatic: bool,
                  before: dict[str, Any], outcome: str, detail: str = "",
                  project_id: Optional[str] = None) -> str:
    aid = f"sda_{uuid.uuid4().hex[:16]}"
    connection.execute(text(
        "INSERT INTO system_doctor_actions (id, finding_id, finding_kind, action, target, "
        "actor, automatic, outcome, detail, before_metrics) VALUES (:id, :f, :k, :a, :t, "
        ":actor, :auto, :o, :d, CAST(:b AS JSONB))"),
        {"id": aid, "f": finding_id, "k": kind, "a": action, "t": target[:300], "actor": actor,
         "auto": automatic, "o": outcome[:200], "d": detail[:1000],
         "b": json.dumps(before, default=str)})
    pid = project_id or os.environ.get("BOSS_DEFAULT_PROJECT_ID") or "boss-dogfood"
    try:
        with connection.begin_nested():
            from .core import emit_event
            emit_event(connection, pid, "system_doctor.acted", actor, "system_finding",
                       finding_id or action,
                       {"action": action, "target": target, "automatic": automatic,
                        "outcome": outcome, "detail": detail[:300], "before": before,
                        "action_id": aid})
    except Exception:  # noqa: BLE001 — the action row is the audit; the event is the feed
        pass
    return aid


def allowed_auto(connection: Any, action: str, target: str) -> tuple[bool, str]:
    """Rate limit + circuit breaker (spec §4c)."""
    recent = connection.execute(text(
        "SELECT count(*) FROM system_doctor_actions WHERE automatic AND action = :a AND "
        "target = :t AND created_at > now() - make_interval(secs => :s)"),
        {"a": action, "t": target, "s": PER_TARGET.total_seconds()}).scalar()
    if recent:
        return False, "ran within the last 5 minutes"
    hour = connection.execute(text(
        "SELECT count(*) FROM system_doctor_actions WHERE automatic AND created_at > now() - "
        "interval '1 hour'")).scalar()
    if int(hour or 0) >= PER_HOUR:
        return False, "20 automatic actions in the last hour"
    last = [r[0] for r in connection.execute(text(
        "SELECT improved FROM system_doctor_actions WHERE automatic AND action = :a AND "
        "verified_at IS NOT NULL ORDER BY created_at DESC LIMIT :n"),
        {"a": action, "n": BREAKER_RUNS}).all()]
    if len(last) >= BREAKER_RUNS and not any(last):
        return False, f"{BREAKER_RUNS} tries did not improve the signal"
    return True, ""


def verify_actions(connection: Any, sig: Signals) -> int:
    rows = connection.execute(text(
        "SELECT id, action, before_metrics FROM system_doctor_actions WHERE verified_at IS NULL "
        "AND created_at < now() - make_interval(secs => :s) LIMIT 50"),
        {"s": VERIFY_AFTER.total_seconds()}).mappings().all()
    after = _metrics(sig)
    for r in rows:
        before = r["before_metrics"] if isinstance(r["before_metrics"], dict) else \
            json.loads(r["before_metrics"] or "{}")
        connection.execute(text(
            "UPDATE system_doctor_actions SET after_metrics = CAST(:a AS JSONB), improved = :i, "
            "verified_at = now() WHERE id = :id"),
            {"a": json.dumps(after), "i": _improved(r["action"], before, after), "id": r["id"]})
    return len(rows)


# ------------------------------------------------------------------------------------------ #
# automatic remedies
# ------------------------------------------------------------------------------------------ #

def _auto(engine: Any, settings: Any, action: str, sig: Signals) -> str:
    from . import preview_service, resource_reaper

    if action == "reap_previews":
        return json.dumps(preview_service.reap_previews(engine, settings))
    if action == "stop_idle_previews":
        return json.dumps(preview_service.reap_previews(engine, settings, idle_minutes=5))
    if action == "end_orphaned_processes":
        return json.dumps(owned_process.sweep_orphans())
    if action == "remove_orphan_containers":
        if not sig.docker:
            return "docker not reachable"
        from .resource_ledger import install_id
        with engine.begin() as c:
            inst = install_id(c)
        return json.dumps(resource_reaper.reap_docker(engine, install_id=inst))
    if action == "sweep_temp_dirs":
        return json.dumps(resource_reaper.reap_dirs())
    if action == "prune_boss_disk":
        if not sig.docker:
            return "docker not reachable from the API; the host prune runs after each update"
        from .resource_ledger import install_id
        with engine.begin() as c:
            inst = install_id(c)
        return json.dumps(resource_reaper.prune_disk(install_id=inst), default=str)
    if action == "close_idle_transactions":
        with engine.begin() as c:
            n = c.execute(text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE state = "
                "'idle in transaction' AND datname = current_database() AND usename = "
                "current_user AND pid <> pg_backend_pid() AND state_change < now() - "
                "interval '10 minutes'")).scalar()
        return f"closed {int(n or 0)} session(s)"
    return "unknown action"


# ------------------------------------------------------------------------------------------ #
# the tick
# ------------------------------------------------------------------------------------------ #

_LEVEL_STATE: dict[str, Any] = {}
_LAST: dict[str, Any] = {}


def tick(engine: Any, settings: Any = None, *, sig: Optional[Signals] = None) -> dict[str, Any]:
    sig = sig or collect(engine)
    found = diagnose(sig)
    now = datetime.now(timezone.utc)
    level = target_level(sig, _LEVEL_STATE, time.time())
    _LEVEL_STATE.update(level)
    acted: list[dict[str, Any]] = []
    with engine.begin() as c:
        ids = _upsert_findings(c, found, now)
        prev = admission._state(c, "degradation")
        if prev.get("level") != level["level"]:
            admission.set_state(c, "degradation", {"level": level["level"],
                                                   "words": level["words"],
                                                   "since": now.isoformat()})
            record_action(c, finding_id=None, kind="degradation", action="set_degradation_level",
                          target=f"L{level['level']}", actor="boss", automatic=True,
                          before=_metrics(sig), outcome=f"level {prev.get('level', 0)} → "
                          f"{level['level']} ({level['words']})")
        verify_actions(c, sig)
    for f in found:
        for action in f.get("auto") or []:
            if action == "set_degradation_level":
                continue
            target = str(f.get("target") or f["kind"])
            with engine.begin() as c:
                ok, why = allowed_auto(c, action, target)
            if not ok:
                continue
            try:
                outcome = _auto(engine, settings, action, sig)
            except Exception as exc:  # noqa: BLE001 — one remedy failing never stops the tick
                outcome = f"failed: {type(exc).__name__}: {exc}"
            with engine.begin() as c:
                record_action(c, finding_id=ids.get(f["kind"]), kind=f["kind"], action=action,
                              target=target, actor="boss", automatic=True,
                              before=_metrics(sig), outcome=outcome[:200], detail=outcome)
            acted.append({"action": action, "outcome": outcome[:200]})
    report = {"at": now.isoformat(), "level": level["level"],
              "findings": [f["kind"] for f in found], "acted": acted}
    _LAST.clear()
    _LAST.update(report)
    return report


# ------------------------------------------------------------------------------------------ #
# what a person sees and chooses
# ------------------------------------------------------------------------------------------ #

def open_findings(connection: Any, *, cards_only: bool = False) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT id, kind, severity, status, title, detail, evidence, options, needs_choice, "
        "first_seen, last_seen, snoozed_until FROM system_findings WHERE status IN ('open', "
        "'snoozed') ORDER BY first_seen")).mappings().all()
    out = []
    now = datetime.now(timezone.utc)
    for r in rows:
        d = dict(r)
        snoozed = d.get("snoozed_until")
        if d["status"] == "snoozed" and snoozed is not None:
            snoozed = snoozed if snoozed.tzinfo else snoozed.replace(tzinfo=timezone.utc)
            if snoozed > now and cards_only:
                continue
        if cards_only and not d.get("needs_choice"):
            continue
        for k in ("first_seen", "last_seen", "snoozed_until"):
            if d.get(k) is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        out.append(d)
    return out


_RAW_ID = re.compile(r"\b(?:crun|wfr|drun|ctr|act|dec|job|shesc|mtg|mrun)_[0-9a-z]{6,}\b")
_STUCK = re.compile(r"stuck in `?([a-z_ ]+)`? with no state change for (\d+) minutes?", re.I)
_CLASS_Q = re.compile(r"^\s*([^—]+?)\s+—\s+how should this proceed\?\s*$", re.I)
_STAGE_WORDS = {"coding": "coding", "queued": "waiting in the queue", "validating": "checking",
                "provisioning": "starting", "opening_pr": "opening its PR"}


def _span_words(minutes: int) -> str:
    if minutes >= 2 * 1440:
        return f"{minutes // 1440} days"
    if minutes >= 120:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


def escalation_words(question: str, task_ref: str, build: Optional[Mapping[str, Any]]
                     ) -> tuple[str, str]:
    """A Shepherd question as an operator reads it: (title, detail). NEVER a raw id, never
    "unknown" (independent test): the build's app name stands where its id stood, a stuck
    time is days or hours, and an unrecognised failure is said as one. Pure."""
    from . import factory_view

    line = (str(question or "").splitlines() or [""])[0].strip()
    name = factory_view.app_name(build) if build else ""
    if not name and ":" in line:
        # The question names its build before the colon ("Slugify util: stuck in …").
        head = line.split(":", 1)[0].strip()
        if head and not _RAW_ID.fullmatch(head) and len(head) <= 80:
            name = head
    thing = f"“{name}”" if name else "A build"
    stuck = _STUCK.search(line)
    cls = _CLASS_Q.match(line)
    from .build_waits import waits_on_person as _waits, why_waiting as _why

    if build and _waits(build.get("status"), build.get("phase"), build.get("decision_status")):
        # A BUILD THAT WAITS ON YOU is never "stopped for a reason BOSS couldn't name".
        title = f"{thing} waits on you to {_why(build.get('phase'), build.get('decision_status')) or 'answer it'}"
    elif stuck:
        stage = _STAGE_WORDS.get(stuck.group(1).strip().lower(), stuck.group(1).replace("_", " "))
        title = f"{thing} stuck {stage} for {_span_words(int(stuck.group(2)))}. Continue or cancel?"
    elif cls:
        what = cls.group(1).strip()
        what = "stopped for a reason BOSS couldn't name" if what.lower() == "unknown" else f"stopped: {what}"
        title = f"{thing} {what}. How should it go on?"
    else:
        title = line
        if name:
            title = title.replace(task_ref, name)
    title = _RAW_ID.sub(name or "a build", title).replace("`", "")
    title = re.sub(r"\bunknown\b", "unrecognised", title, flags=re.I)[:160]
    if build:
        status = str(build.get("status") or "")
        word = {"cancelled": "stopped", "failed": "failed", "completed": "finished",
                "blocked": "waiting on you", "running": "still marked running"}.get(status, status or "unknown")
        detail = f"Build {'of ' + name if name else ''} · {word}".replace("  ", " ")
    else:
        detail = "Its build is gone"
    detail = re.sub(r"\bunknown\b", "state not recorded", detail)
    return title, detail


def plain_words(text_: Any) -> str:
    """Every finding's words as a card shows them: no raw ids, no "unknown" (the class the
    independent test asked for — escalation_words is the rich form for Shepherd's questions)."""
    out = _RAW_ID.sub("a build", str(text_ or "")).replace("`", "")
    return re.sub(r"\bunknown\b", "unrecognised", out, flags=re.I)


def shepherd_escalation_cards(connection: Any, project_id: str) -> list[dict[str, Any]]:
    """Shepherd's open questions, which waited 2-3 days unanswered inside a console nobody
    opened, are cards in What needs you through the same kind (spec §4d). Newest first: the
    old ones are counted and cleared as one row (command_center_view.old_questions)."""
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT id, task_ref, question, options, created_at FROM shepherd_escalations "
                "WHERE project_id = :p AND status = 'open' ORDER BY created_at DESC LIMIT 10"),
                {"p": project_id}).mappings().all()
            refs = [str(r["task_ref"]) for r in rows if r["task_ref"]]
            builds = {str(b["id"]): dict(b) for b in connection.execute(text(
                "SELECT r.id, r.title, r.status, r.phase, r.spec, r.finalize_ctx, "
                "d.status AS decision_status FROM code_task_runs r "
                "LEFT JOIN decisions d ON d.id = r.decision_id WHERE r.id = ANY(:ids)"),
                {"ids": refs}).mappings()} if refs else {}
    except Exception:  # noqa: BLE001 — an older schema
        return []
    out = []
    for r in rows:
        options = r["options"] if isinstance(r["options"], list) else json.loads(
            r["options"] or "[]")
        title, detail = escalation_words(str(r["question"] or ""), str(r["task_ref"] or ""),
                                         builds.get(str(r["task_ref"] or "")))
        opts = []
        for o in options[:3]:
            label = str(o.get("label") or o.get("action") or "") if isinstance(o, dict) else str(o)
            key = str(o.get("action") or o.get("label") or "") if isinstance(o, dict) else str(o)
            opts.append({"key": f"answer:{key}", "label": label[:60].capitalize(), "tip": label,
                         "confirm": False})
        out.append({
            "id": f"shepherd:{r['id']}", "kind": "shepherd_escalation", "severity": "warn",
            "title": title, "detail": detail, "options": opts,
            "first_seen": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return out


def card_items(connection: Any, project_id: str) -> list[dict[str, Any]]:
    """`command_center.assemble(system=…)`: the findings that need a choice, plus Shepherd's
    open questions, each shaped as a `system_health` needs_you item."""
    items = []
    for f in open_findings(connection, cards_only=True) + shepherd_escalation_cards(
            connection, project_id):
        options = f.get("options") or []
        if isinstance(options, str):
            options = json.loads(options)
        fid = str(f["id"])
        items.append({
            "id": fid if fid.startswith("shepherd:") else f"system:{fid}",
            "kind": "system_health",
            "finding_id": fid, "finding_kind": f["kind"],
            "severity": f.get("severity") or "warn",
            "title": plain_words(f["title"]), "detail": plain_words(f.get("detail") or ""),
            "what_to_do": None,
            "answers": [{"key": o["key"], "label": o["label"], "enabled": True,
                         "reason": o.get("tip"), "confirm": bool(o.get("confirm"))}
                        for o in options],
            "actions": [o["key"] for o in options],
            "run_id": None, "at": f.get("first_seen"),
        })
    return items


class ActRefused(ValueError):
    pass


def act(engine: Any, settings: Any, finding_id: str, option: str, *, actor: str) -> dict[str, Any]:
    """Do what the person chose, record it, and say what happened in place."""
    if finding_id.startswith("shepherd:"):
        esc = finding_id.split(":", 1)[1]
        answer = option.split(":", 1)[1] if option.startswith("answer:") else option
        with engine.begin() as c:
            n = c.execute(text(
                "UPDATE shepherd_escalations SET status='answered', answer=:a, answered_by=:by, "
                "answered_at=now() WHERE id=:id AND status='open'"),
                {"a": answer, "by": actor, "id": esc}).rowcount
            if not n:
                raise ActRefused("that question was already answered")
            record_action(c, finding_id=finding_id, kind="shepherd_escalation",
                          action="answer_escalation", target=esc, actor=actor, automatic=False,
                          before={}, outcome=f"answered: {answer}")
        return {"ok": True, "result": f"Answered: {answer}", "resolved": True}

    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM system_findings WHERE id = :id"),
                        {"id": finding_id}).mappings().first()
    if row is None:
        raise LookupError("no such finding")
    if row["status"] not in ("open", "snoozed"):
        raise ActRefused(f"this is already {row['status']}")
    options = row["options"] if isinstance(row["options"], list) else json.loads(
        row["options"] or "[]")
    if option not in {o["key"] for o in options}:
        raise ActRefused(f"“{option}” is not one of this card's options")
    evidence = row["evidence"] if isinstance(row["evidence"], dict) else json.loads(
        row["evidence"] or "{}")
    cap = admission.sample()
    before = {"available_mb": round(cap.available_mb), "psi_full_avg60": cap.psi_full_avg60,
              "disk_used_pct": cap.disk_used_pct}
    handler = _CHOICES.get(option)
    if handler is None:
        raise ActRefused(f"nothing handles “{option}”")
    result = handler(engine, settings, dict(row), evidence, actor)
    with engine.begin() as c:
        record_action(c, finding_id=finding_id, kind=row["kind"], action=option,
                      target=str(result.get("target") or row["kind"]), actor=actor,
                      automatic=False, before=before, outcome=result["result"][:200],
                      detail=json.dumps(result, default=str)[:1000])
        status = result.get("status")
        if status == "resolved":
            c.execute(text("UPDATE system_findings SET status='resolved', resolved_at=now(), "
                           "chosen_option=:o, chosen_by=:a WHERE id=:id"),
                      {"o": option, "a": actor, "id": finding_id})
        elif status == "snoozed":
            c.execute(text("UPDATE system_findings SET status='snoozed', snoozed_until=:u, "
                           "chosen_option=:o, chosen_by=:a WHERE id=:id"),
                      {"u": result["until"], "o": option, "a": actor, "id": finding_id})
        else:
            c.execute(text("UPDATE system_findings SET chosen_option=:o, chosen_by=:a, "
                           "needs_choice=false WHERE id=:id"),
                      {"o": option, "a": actor, "id": finding_id})
    return {"ok": True, **result}



#: Shepherd questions older than this are history: Clean up clears them (command_center_view
#: groups exactly these into one row).
OLD_QUESTION = timedelta(days=7)
_ENDED_BUILD = frozenset({"completed", "failed", "cancelled", "simulated"})


CLEAN_UP_PAGE = 200


from .build_waits import waits_on_person, why_waiting  # noqa: E402


def clean_up_old(engine: Any, settings: Any, project_id: str, *, actor: str,
                 older_than: timedelta = OLD_QUESTION) -> dict[str, Any]:
    """CLEAN UP, the Command Center's one button for old stuck builds.

    Each of the project's open Shepherd questions older than `older_than` ("stuck in `coding`
    … Let it continue, or cancel it?") is about a build. If that build has already ended (or
    no longer exists) the question is closed as answered — nothing else changes. If it is still
    live with no state change for as long, nobody is going to continue it: it is cancelled the
    way a person's Stop cancels it (code_task_service.cancel_run, the console's own path) and
    the question is answered "cancel". Recent questions and findings are never touched. Every
    choice is recorded (system_doctor_actions), as a person's choice on a card is."""
    from . import code_task_service

    cutoff = datetime.now(timezone.utc) - older_than
    closed = cancelled = 0
    def pages():
        # PAGED, every old question (independent test: a page of ten looked like the total).
        after = ""
        while True:
            with engine.begin() as c:
                page = c.execute(text(
                    "SELECT e.id, e.task_ref, r.status, r.phase, r.updated_at, "
                    "d.status AS decision_status FROM shepherd_escalations e "
                    "LEFT JOIN code_task_runs r ON r.id = e.task_ref "
                    "LEFT JOIN decisions d ON d.id = r.decision_id "
                    "WHERE e.project_id = :p AND e.status = 'open' AND e.created_at < :cut "
                    "AND e.id > :after ORDER BY e.id LIMIT :n"),
                    {"p": project_id, "cut": cutoff, "after": after, "n": CLEAN_UP_PAGE}).mappings().all()
            yield from page
            if len(page) < CLEAN_UP_PAGE:
                return
            after = str(page[-1]["id"])

    for row in pages():
        esc, ref = str(row["id"]), str(row["task_ref"] or "")
        status = str(row["status"] or "")
        updated = row["updated_at"]
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        live = bool(status) and status not in _ENDED_BUILD
        answer = "cleared: the build already ended"
        # WAITING ON A PERSON IS NOT STUCK (independent re-check R1): a build at its plan, its
        # merge, or any open decision is the operator's move, never BOSS's. Clean up closes
        # the stale question and leaves the build and its decision exactly as they are.
        waiting = waits_on_person(status, row["phase"], row["decision_status"])
        if waiting:
            answer = "cleared: the build waits on a person"
        with engine.begin() as c:
            if live and not waiting and (updated is None or updated < cutoff):
                try:
                    code_task_service.cancel_run(
                        c, ref, project_id, actor=actor,
                        reason="Cleared by Clean up: no progress for over 7 days")
                    cancelled += 1
                    answer = "cancel"
                except (code_task_service.CancelRefused, LookupError):
                    pass
            elif live and not waiting:
                continue  # it moved since the question: not history, leave the card
            n = c.execute(text(
                "UPDATE shepherd_escalations SET status='answered', answer=:a, answered_by=:by, "
                "answered_at=now() WHERE id=:id AND status='open'"),
                {"a": answer, "by": actor, "id": esc}).rowcount
            if not n:
                continue
            closed += 1
            record_action(c, finding_id=f"shepherd:{esc}", kind="shepherd_escalation",
                          action="clean_up_old", target=ref or esc, actor=actor,
                          automatic=False, before={"build_status": status or None},
                          outcome=answer, project_id=project_id)
    words = f"Cleared {closed} old {'question' if closed == 1 else 'questions'}"
    if cancelled:
        words += f" · stopped {cancelled} stuck {'build' if cancelled == 1 else 'builds'}"
    return {"ok": True, "closed": closed, "cancelled": cancelled, "result": words}


def _snooze(minutes: float, message: str) -> Callable[..., dict[str, Any]]:
    def _h(engine, settings, row, evidence, actor):
        return {"result": message, "status": "snoozed",
                "until": datetime.now(timezone.utc) + timedelta(minutes=minutes)}
    return _h


def _one_at_a_time(engine, settings, row, evidence, actor):
    with engine.begin() as c:
        # force_one: one build runs even under the pressure that would otherwise hold it —
        # the operator chose to build anyway, one at a time.
        admission.set_state(c, "builds_at_once", {"value": 1, "until_finding": row["id"],
                                                  "set_by": actor, "force_one": True})
    return {"result": "Builds run one at a time until memory frees up. Nothing running was "
                      "stopped.", "status": "chosen"}


def _pause_largest(engine, settings, row, evidence, actor):
    biggest = (evidence or {}).get("largest") or {}
    run_id = biggest.get("run_id")
    if not run_id:
        raise ActRefused("no build is running here any more")
    from .code_task_service import CancelRefused, cancel_run
    with engine.begin() as c:
        project = c.execute(text("SELECT project_id FROM code_task_runs WHERE id=:id"),
                            {"id": run_id}).scalar()
        try:
            from .people_names import who

            cancel_run(c, run_id, str(project), actor=actor,
                       reason=f"Paused by {who(actor, c)} to free memory; press Resume to continue")
        except (LookupError, CancelRefused) as exc:
            raise ActRefused(str(exc)) from exc
        c.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) || "
            "jsonb_build_object('paused_for_memory', true) WHERE id = :id"), {"id": run_id})
    owned_process.terminate_owner(run_id, reason="cancel")
    freed = float(biggest.get("mem_mb") or 0)
    return {"result": f"Paused “{biggest.get('name') or run_id}” · freed about "
                      f"{freed / 1024:.1f} GB", "status": "chosen", "target": run_id}


def _free_disk(engine, settings, row, evidence, actor):
    from . import resource_reaper
    from .resource_ledger import install_id

    if not resource_reaper.docker_available():
        return {"result": "BOSS cannot reach Docker from here. On the server run: "
                          "boss prune --dedicated", "status": "chosen",
                "command": "boss prune --dedicated"}
    with engine.begin() as c:
        inst = install_id(c)
    out = resource_reaper.prune_disk(install_id=inst, force=True)
    after = (out.get("after") or {}).get("used_fraction")
    before = (out.get("before") or {}).get("used_fraction")
    return {"result": (f"Freed BOSS's old builds · disk {before:.0%} → {after:.0%}"
                       if before is not None and after is not None else "Freed BOSS's old builds"),
            "status": "resolved"}


def _restart_step(engine, settings, row, evidence, actor):
    stuck = ((evidence or {}).get("stuck") or [{}])[0]
    job = stuck.get("job_id")
    if not job:
        raise ActRefused("the build is no longer stuck")
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET lease_expires_at = now(), updated_at = now() "
                       "WHERE id = :id AND status IN ('leased', 'running')"), {"id": job})
    if stuck.get("run_id"):
        owned_process.terminate_owner(str(stuck["run_id"]), reason="restart")
    return {"result": "Handed back to the queue; the step restarts in under a minute.",
            "status": "resolved", "target": job}


def _cancel_build(engine, settings, row, evidence, actor):
    stuck = ((evidence or {}).get("stuck") or [{}])[0]
    run_id = stuck.get("run_id")
    if not run_id:
        raise ActRefused("no build to cancel")
    from .code_task_service import CancelRefused, cancel_run
    with engine.begin() as c:
        project = c.execute(text("SELECT project_id FROM code_task_runs WHERE id=:id"),
                            {"id": run_id}).scalar()
        try:
            from .people_names import who

            cancel_run(c, run_id, str(project), actor=actor, reason=f"Cancelled by {who(actor, c)}")
        except (LookupError, CancelRefused) as exc:
            raise ActRefused(str(exc)) from exc
    owned_process.terminate_owner(run_id, reason="cancel")
    return {"result": "Build cancelled.", "status": "resolved", "target": run_id}


def _hold_updates(engine, settings, row, evidence, actor):
    with engine.begin() as c:
        admission.set_state(c, "deploy_hold", {"hold": True, "set_by": actor})
    return {"result": "Updates now wait for running builds to finish.", "status": "resolved"}


def _keep_updating(engine, settings, row, evidence, actor):
    with engine.begin() as c:
        admission.set_state(c, "deploy_hold", {"hold": False, "set_by": actor})
    return {"result": "Updates go ahead immediately; builds resume after each.",
            "status": "snoozed",
            "until": datetime.now(timezone.utc) + timedelta(days=1)}


def _guide(url: str, message: str) -> Callable[..., dict[str, Any]]:
    def _h(engine, settings, row, evidence, actor):
        return {"result": message, "status": "chosen", "open": url}
    return _h


_CHOICES: dict[str, Callable[..., dict[str, Any]]] = {
    "queue_new_builds": _one_at_a_time,
    "one_build_at_a_time": _one_at_a_time,
    "build_one_at_a_time": _one_at_a_time,
    "pause_largest_build": _pause_largest,
    "keep_going": _snooze(60, "Nothing changed. BOSS asks again in an hour if it persists."),
    "dismiss_today": _snooze(24 * 60, "Hidden until tomorrow."),
    "wait_15": _snooze(15, "BOSS asks again in 15 minutes."),
    "remind_at_90": _snooze(7 * 24 * 60, "BOSS reminds you when the disk is 90% full."),
    "acknowledge": lambda e, s, r, ev, a: {"result": "Noted.", "status": "resolved"},
    "docker_memory_guide": _guide(DOCKER_MEMORY_GUIDE, "Opened the Docker Desktop guide."),
    "use_bigger_place": _guide("?area=places", "Opened Places."),
    "free_disk_now": _free_disk,
    "restart_step": _restart_step,
    "cancel_build": _cancel_build,
    "hold_updates": _hold_updates,
    "keep_updating": _keep_updating,
}


def recent_actions(connection: Any, *, since: Optional[str] = None,
                   limit: int = 100) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"n": max(1, min(500, limit))}
    where = ""
    if since:
        where = "WHERE created_at > CAST(:since AS timestamptz)"
        params["since"] = since
    rows = connection.execute(text(
        f"SELECT id, finding_id, finding_kind, action, target, actor, automatic, outcome, "
        f"detail, before_metrics, after_metrics, improved, created_at, verified_at FROM "
        f"system_doctor_actions {where} ORDER BY created_at DESC LIMIT :n"), params).mappings()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("created_at", "verified_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        out.append(d)
    return out


def record_host_sample(connection: Any, sample: dict[str, Any]) -> dict[str, Any]:
    """The optional Mac helper's reading of the machine under Docker Desktop."""
    clean = {
        "swap_used_mb": float(sample.get("swap_used_mb") or 0),
        "swap_total_mb": float(sample.get("swap_total_mb") or 0),
        "memory_pressure": str(sample.get("memory_pressure") or "")[:40],
        "top": [{"name": str(t.get("name") or "")[:40], "mb": float(t.get("mb") or 0)}
                for t in (sample.get("top") or [])[:8] if isinstance(t, dict)],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    admission.set_state(connection, "host_sample", clean)
    return clean


# ------------------------------------------------------------------------------------------ #
# the thread
# ------------------------------------------------------------------------------------------ #

_THREAD: Optional[threading.Thread] = None


def last_report() -> dict[str, Any]:
    return dict(_LAST)


def start(engine: Any, settings: Any, *, interval: float = INTERVAL_SECONDS) -> threading.Thread:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                report = tick(engine, settings)
                if report.get("acted"):
                    _log.info("system doctor acted: %s", report["acted"])
            except Exception:  # noqa: BLE001
                _log.debug("system doctor tick failed", exc_info=True)

    _THREAD = threading.Thread(target=_loop, name="boss-system-doctor", daemon=True)
    _THREAD.start()
    return _THREAD
