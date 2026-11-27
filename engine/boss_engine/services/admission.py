"""CAPACITY-AWARE ADMISSION: queue rather than thrash (stability spec §4b.2-§4b.3).

Before this, what started a build was a static number (`BOSS_CODE_AGENT_MAX_CONCURRENCY`, and a
governance fallback of 3 that disagreed with it) and the budget. Nothing asked the machine. On
an 8 GB Docker VM three builds plus their previews drove it into swap (pswpout 932 MB, memory
PSI full 76 s) and then pushed the Mac under it to 91% swap. A queued job costs nothing; a
thrashing host slows every job, and the control plane with them.

At lease time (`jobs.claim_job`, in the API — where the builds run) a heavy job is admitted only
when ALL hold:

    MemAvailable − reserve ≥ the job's request      reserve = max(1 GB, 15% of RAM)
    memory PSI full avg60 < 5
    free disk ≥ BOSS_BUILD_DISK_FLOOR_GB (8 GiB; a hard hold, see DEFAULT_BUILD_DISK_FLOOR_GB)
    load per core < 1.5
    heavy jobs leased or running < "Builds at once"
    the degradation level is below L2

Otherwise the job stays queued, `deferred_reason=host_capacity`, with the shortfall in the
operator's words ("needs 1.2 GB, 0.6 GB free"). `concurrency_governance.admit_or_reject` — the
deterministic overflow hook that had no production caller — makes the final call.

"Builds at once" is derived: (RAM − reserve − running previews) / a build's request, clamped to
[1, cores/2]. 16 GB/8 cores → 4; an 8 GB Docker VM → 2-3 (after its baseline); a 4 GB box → 1.
The operator can pin it (`system_state.builds_at_once`); higher than derived is capped with a
warning.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from sqlalchemy import text

from .concurrency_governance import admit_or_reject
from .workload_profiles import HEAVY_JOB_KINDS, Profile, load as load_profile, profile_for_job

#: DISK FLOOR (storage hygiene layer 1, BOSSAPP-1150). A build on a nearly full disk does not
#: slow down like one short of memory: it fills the disk, and a full disk takes Postgres down
#: with it. So the floor is a hard hold — never waived by "nothing is running, so one may run"
#: — configurable as BOSS_BUILD_DISK_FLOOR_GB (default 8 GiB), and the hold is said on the run
#: and recorded as a `host.disk_low` event; the next poll with room above the floor admits.
DEFAULT_BUILD_DISK_FLOOR_GB = 8.0
#: Where builds write: the container root (Docker's own disk) and the BOSS data volumes; the
#: least free of the paths that exist is the figure. BOSS_BUILD_DISK_PATHS overrides (comma-separated).
DEFAULT_DISK_PATHS = ("/", "/var/lib/boss")
MAX_PSI_FULL_AVG60 = 5.0
MAX_LOAD_PER_CORE = 1.5
MAX_SLOTS = 8

#: Degradation ladder (spec §4b.3).
L0, L1, L2, L3 = 0, 1, 2, 3
LEVEL_WORDS = {L0: "normal", L1: "pressure warning", L2: "critical", L3: "sustained critical"}


@dataclass
class Capacity:
    total_mb: float
    available_mb: float
    cores: int = 1
    psi_full_avg10: float = 0.0
    psi_full_avg60: float = 0.0
    disk_free_gb: float = 100.0
    disk_used_pct: float = 0.0
    load_per_core: float = 0.0
    swap_used_mb: float = 0.0
    scope: str = "machine"            # machine | docker_vm | container
    limit_mb: Optional[float] = None  # the container's own cap, when it has one
    notes: list[str] = field(default_factory=list)

    @property
    def reserve_mb(self) -> float:
        return max(1024.0, 0.15 * self.total_mb)

    @property
    def spare_mb(self) -> float:
        return max(0.0, self.available_mb - self.reserve_mb)


def build_disk_floor_gb() -> float:
    """BOSS_BUILD_DISK_FLOOR_GB, or the default; a value that is not a number is the default."""
    import os

    raw = (os.environ.get("BOSS_BUILD_DISK_FLOOR_GB") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_BUILD_DISK_FLOOR_GB
    except ValueError:
        return DEFAULT_BUILD_DISK_FLOOR_GB
    return max(0.0, value)


def disk_snapshot(paths: Optional[list[str]] = None) -> dict[str, float]:
    """{free_gb, used_pct} of the fullest of `paths` that exist (statvfs only: this runs on
    every lease poll). Empty when none can be read."""
    import os
    import shutil

    if paths is None:
        raw = (os.environ.get("BOSS_BUILD_DISK_PATHS") or "").strip()
        paths = [p.strip() for p in raw.split(",") if p.strip()] or list(DEFAULT_DISK_PATHS)
    out: dict[str, float] = {}
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        free_gb = round(usage.free / 1e9, 1)
        if not out or free_gb < out["free_gb"]:
            out = {"free_gb": free_gb,
                   "used_pct": round(100 * usage.used / usage.total, 1) if usage.total else 0.0}
    return out


def sample() -> Capacity:
    """What this API container can see right now (Linux /proc and cgroup v2)."""
    from .self_observability import cpu_snapshot, memory_snapshot

    mem, cpu = memory_snapshot(), cpu_snapshot()
    disk = disk_snapshot()
    total = float(mem.get("host_total_mb") or 0.0)
    available = float(mem.get("host_available_mb") or 0.0)
    limit = mem.get("limit_mb")
    scope = "machine" if mem.get("host_is_the_machine", True) else "docker_vm"
    if limit:
        # A capped container cannot use the host's free memory beyond its own cap.
        held = float(mem.get("unreclaimable_mb") or mem.get("used_mb") or 0.0)
        cap_spare = float(limit) - held
        if not total or float(limit) < total:
            total = float(limit)
            available = min(available or cap_spare, cap_spare)
            scope = "container"
    cores = int(cpu.get("cpu_limit_cores") or cpu.get("cores") or 1)
    return Capacity(
        total_mb=total or 0.0, available_mb=available or 0.0, cores=max(1, cores),
        psi_full_avg10=float(mem.get("psi_full_avg10") or 0.0),
        psi_full_avg60=float(mem.get("psi_full_avg60") or 0.0),
        disk_free_gb=float(disk.get("free_gb") or 0.0) if disk else 100.0,
        disk_used_pct=float(disk.get("used_pct") or 0.0) if disk else 0.0,
        load_per_core=float(cpu.get("load_per_core") or 0.0),
        swap_used_mb=float(mem.get("host_swap_used_mb") or 0.0),
        scope=scope, limit_mb=float(limit) if limit else None,
    )


def level_of(cap: Capacity) -> int:
    """The instantaneous rung (the doctor makes it sticky and decides L3)."""
    if cap.total_mb <= 0:
        return L0
    free_frac = cap.available_mb / cap.total_mb
    if cap.psi_full_avg10 >= 10 or cap.psi_full_avg60 >= 5 or free_frac < 0.10:
        return L2
    if cap.psi_full_avg60 >= 2 or free_frac < 0.20:
        return L1
    return L0


def derived_slots(cap: Capacity, request_mb: float, *, previews_mb: float = 0.0) -> int:
    return int(derived_slots_why(cap, request_mb, previews_mb=previews_mb)["value"])


def derived_slots_why(cap: Capacity, request_mb: float, *,
                      previews_mb: float = 0.0) -> dict[str, Any]:
    """{value, why}: the heavy builds this machine runs at once, and why that number, from what
    was measured (Setup says it: "4 cores → 2 at once (one per 2 cores); memory fits 5")."""
    if cap.total_mb <= 0 or request_mb <= 0:
        return {"value": 1, "why": "This machine's memory could not be read, so 1 at a time."}
    room = cap.total_mb - cap.reserve_mb - previews_mb
    by_memory = math.floor(room / request_mb)
    by_cpu = max(1, cap.cores // 2)
    value = int(max(1, min(by_memory, by_cpu, MAX_SLOTS)))
    memory = (f"{_gb(cap.total_mb)} memory, {_gb(cap.reserve_mb)} kept free"
              + (f" and {_gb(previews_mb)} for running previews" if previews_mb else "")
              + f": room for {max(0, by_memory)} at {_gb(request_mb)} each")
    cpu = f"{cap.cores} CPU core{'s' if cap.cores != 1 else ''}: {by_cpu} (one per 2 cores)"
    if value == MAX_SLOTS and min(by_memory, by_cpu) > MAX_SLOTS:
        limit = f"the most BOSS runs is {MAX_SLOTS}"
    elif by_memory < 1:
        limit = "memory is short, so 1 at a time (nothing else running means one may run)"
    else:
        limit = "the smaller of the two"
    return {"value": value, "why": f"{memory}; {cpu}; {limit} → {value}."}


@dataclass
class Decision:
    admitted: bool
    reason: str
    code: str = "ok"
    slots: int = 1
    in_flight: int = 0
    level: int = L0
    shortfall: Optional[str] = None
    capacity: Optional[dict[str, Any]] = None
    #: True when free disk is below the floor: the hold no one-at-a-time rule waives.
    disk_low: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gb(mb: float) -> str:
    return f"{mb / 1024:.1f} GB"


def disk_shortfall(cap: Capacity, floor_gb: Optional[float] = None) -> Optional[str]:
    """The disk hold in the operator's words, or None when free disk is at or above the floor."""
    floor = build_disk_floor_gb() if floor_gb is None else float(floor_gb)
    if cap.disk_free_gb >= floor:
        return None
    return (f"{cap.disk_free_gb:.1f} GB of disk free where builds run; builds wait until at "
            f"least {floor:g} GB is free (BOSS_BUILD_DISK_FLOOR_GB)")


def decide(cap: Capacity, profile: Profile, *, in_flight: int, slots: int,
           level: int = L0, force_one: bool = False,
           disk_floor_gb: Optional[float] = None,
           running_total: Optional[int] = None) -> Decision:
    """Pure: admit or defer one heavy job. Every refusal says what is short, in GB.

    `in_flight` is what counts against the slots (builds holding the heavy slot, and running
    builds waiting to take it back — heavy_slot); `running_total`, when given, is every heavy
    job leased at all, which is what "nothing running" means (a build thinking with its slot
    given back is still running on this machine)."""
    problems: list[str] = []
    if cap.total_mb > 0 and cap.spare_mb < profile.request_mb:
        problems.append(f"needs {_gb(profile.request_mb)}, {_gb(cap.spare_mb)} free")
    if cap.psi_full_avg60 >= MAX_PSI_FULL_AVG60:
        problems.append(f"the machine waited on memory {cap.psi_full_avg60:.0f}% of the last "
                        "minute")
    disk_low = disk_shortfall(cap, disk_floor_gb)
    if disk_low:
        problems.append(disk_low)
    if cap.load_per_core >= MAX_LOAD_PER_CORE:
        problems.append(f"CPU load {cap.load_per_core:.1f} per core")
    if level >= L2:
        problems.append("the system doctor has paused new builds until pressure eases")
    # NOTHING RUNNING MEANS ONE MAY RUN. A small or busy machine runs one build at a time
    # rather than none forever. Critical pressure (L2) still holds it — unless the operator
    # chose "one at a time" / "build anyway" on the card (force_one); sustained critical (L3)
    # holds even that. A DISK BELOW THE FLOOR ALWAYS HOLDS: memory pressure slows a build, a
    # full disk breaks the database every build stands on (BOSSAPP-1150).
    # (Review of PR #300: holding the idle probe on CPU load or memory pressure alone left
    # queued builds waiting for a person on any busy host. Sustained pressure is the system
    # doctor's L2, which still holds it, as on main.)
    nothing_running = (in_flight if running_total is None else running_total) == 0
    if problems and not disk_low and nothing_running and (
            level < L2 or (force_one and level < L3)):
        problems = []
    backpressure = {"admit_new_work": not problems, "reason": "; ".join(problems)}
    verdict = admit_or_reject(in_flight=in_flight, max_in_flight=max(1, slots),
                              backpressure=backpressure)
    shortfall = None
    if not verdict["admitted"]:
        shortfall = (backpressure["reason"] if problems else
                     f"{in_flight} of {slots} builds at once already running")
    return Decision(admitted=bool(verdict["admitted"]), reason=str(verdict["reason"]),
                    code=str(verdict["code"]), slots=slots, in_flight=in_flight, level=level,
                    shortfall=shortfall, disk_low=bool(disk_low) and not verdict["admitted"],
                    capacity={k: v for k, v in asdict(cap).items() if k != "notes"})


# ------------------------------------------------------------------------------------------ #
# reading the database
# ------------------------------------------------------------------------------------------ #

def _state(connection: Any, key: str) -> dict[str, Any]:
    try:
        raw = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                                 {"k": key}).scalar()
    except Exception:  # noqa: BLE001 — an older schema has no system_state
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def set_state(connection: Any, key: str, value: dict[str, Any]) -> None:
    connection.execute(text(
        "INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), now()) "
        "ON CONFLICT (key) DO UPDATE SET value = CAST(:v AS JSONB), updated_at = now()"),
        {"k": key, "v": json.dumps(value)})


_LIVE_HEAVY_SQL = ("j.kind = ANY(:k) AND j.status IN ('leased', 'running') "
                   "AND (j.lease_expires_at IS NULL OR j.lease_expires_at > now())")


def heavy_counts(connection: Any) -> dict[str, int]:
    """{holding, waiting, light, total} over live heavy jobs (heavy_slot): `holding` occupy a
    slot; `waiting` are running builds that gave theirs back and want it again; `light` gave it
    back and are thinking. An older schema without heavy_slot_releases: every live job holds."""
    from .heavy_slot import RELEASED_SQL, WAITING_SQL

    kinds = sorted(HEAVY_JOB_KINDS)
    try:
        with connection.begin_nested():
            row = connection.execute(text(
                f"SELECT count(*) AS total, count(*) FILTER (WHERE NOT {RELEASED_SQL}) AS holding, "
                f"count(*) FILTER (WHERE {WAITING_SQL}) AS waiting FROM jobs j "
                f"WHERE {_LIVE_HEAVY_SQL}"), {"k": kinds}).mappings().first()
    except Exception:  # noqa: BLE001 — no releases table yet: the old count
        total = int(connection.execute(text(
            f"SELECT count(*) FROM jobs j WHERE {_LIVE_HEAVY_SQL}"), {"k": kinds}).scalar() or 0)
        return {"holding": total, "waiting": 0, "light": 0, "total": total}
    total, holding, waiting = (int(row["total"] or 0), int(row["holding"] or 0),
                               int(row["waiting"] or 0))
    return {"holding": holding, "waiting": waiting, "light": max(0, total - holding - waiting),
            "total": total}


def heavy_in_flight(connection: Any) -> int:
    """Heavy jobs holding a slot now (a build that gave its slot back while it thinks does not)."""
    return heavy_counts(connection)["holding"]


def reacquire_verdict(connection: Any, job_id: str) -> dict[str, Any]:
    """May this RUNNING build (it gave its slot back) take it again? Only the slot count gates
    it, oldest waiter first: {admitted, slots, holders, ahead}. Called under the admission lock.

    Not the host checks: an in-flight build was never stopped for memory or load, and giving its
    slot back while it thought must not make it hostage to them. New leases still pass them."""
    from .heavy_slot import RELEASED_SQL

    cap = sample()
    profile = load_profile(connection, "code.task")
    slots = max(1, int(builds_at_once(connection, cap, profile.request_mb)["value"] or 1))
    kinds = sorted(HEAVY_JOB_KINDS)
    holders = connection.execute(text(
        f"SELECT j.id, j.payload->>'run_id' AS run_id FROM jobs j WHERE {_LIVE_HEAVY_SQL} "
        f"AND NOT {RELEASED_SQL} ORDER BY j.created_at LIMIT 20"), {"k": kinds}).mappings().all()
    mine = connection.execute(text(
        "SELECT waiting_since FROM heavy_slot_releases WHERE job_id = :j"),
        {"j": job_id}).scalar()
    ahead = int(connection.execute(text(
        "SELECT count(*) FROM heavy_slot_releases h JOIN jobs j ON j.id = h.job_id "
        "AND j.fence_epoch = h.fence_epoch "
        f"WHERE h.state = 'waiting' AND h.job_id <> :me AND {_LIVE_HEAVY_SQL} "
        "AND (CAST(:since AS timestamptz) IS NULL OR h.waiting_since < CAST(:since AS timestamptz))"),
        {"k": kinds, "me": job_id, "since": mine}).scalar() or 0)
    return {"admitted": len(holders) + ahead < slots, "slots": slots, "ahead": ahead,
            "holders": [str(h["run_id"] or h["id"]) for h in holders]}


def running_previews_mb(connection: Any) -> float:
    try:
        n = int(connection.execute(text(
            "SELECT count(*) FROM app_previews WHERE status = 'running'")).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0.0
    from .workload_profiles import DEFAULTS
    return n * DEFAULTS["preview_node"].request_mb


def builds_at_once(connection: Any, cap: Capacity, request_mb: float, *,
                   boosted: bool = True) -> dict[str, Any]:
    """{value, auto, derived, pinned, warning, why, boost} — what the console shows as "Builds
    at once". `boosted`: with the system doctor's boost on top (capacity_steward: work waited
    while the host had room), never above the measured ceiling."""
    base = _builds_at_once_base(connection, cap, request_mb)
    if not boosted:
        return base
    from . import capacity_steward

    boost = capacity_steward.boost_of(connection)
    if boost <= 0:
        return {**base, "boost": 0}
    top = capacity_steward.ceiling(cap, request_mb)
    value = max(int(base["value"]), min(int(base["value"]) + boost, top))
    if value == base["value"]:
        return {**base, "boost": 0}
    why = (f"{base['value']} set, +{value - base['value']} added by the system doctor: work "
           f"waited while this machine had room (at most {top} here). {base['why']}")
    return {**base, "value": value, "boost": value - int(base["value"]), "why": why}


def _builds_at_once_base(connection: Any, cap: Capacity, request_mb: float) -> dict[str, Any]:
    measured = derived_slots_why(cap, request_mb, previews_mb=running_previews_mb(connection))
    derived = int(measured["value"])
    pinned = _state(connection, "builds_at_once").get("value")
    if isinstance(pinned, int) and pinned >= 1:
        value = min(pinned, derived, MAX_SLOTS)
        warning = (f"Pinned at {pinned}; this machine measures room for {derived}."
                   if pinned > derived else None)
        return {"value": value, "auto": False, "derived": derived, "pinned": pinned,
                "warning": warning,
                "why": f"Pinned at {pinned}. Auto would be {derived}: {measured['why']}"}
    return {"value": derived, "auto": True, "derived": derived, "pinned": None,
            "warning": None, "why": measured["why"]}


#: A running build that only thinks (its slot given back) costs about one agent CLI.
THINKING_BUILD_MB = 400


def derived_worker_slots(cap: Capacity, builds: int, request_mb: float) -> dict[str, Any]:
    """{value, why}: job slots when the operator has not chosen. A build holds its heavy slot
    only while it works the machine (heavy_slot), so more builds than heavy slots can run: the
    heavy ones at their request, the rest as thinking agents (~400 MB), within the host's room;
    never fewer than the heavy slots, never more than 4 (the worker's own ceiling)."""
    builds = max(1, int(builds or 1))
    if cap.total_mb <= 0:
        return {"value": min(4, builds),
                "why": "One job slot per heavy build: this machine's memory could not be read."}
    room = cap.total_mb - cap.reserve_mb - builds * request_mb
    thinking = max(0, math.floor(room / THINKING_BUILD_MB))
    value = max(1, min(4, max(builds, builds + thinking)))
    return {"value": value,
            "why": (f"{builds} heavy at {_gb(request_mb)} each leaves {_gb(max(0.0, room))} for "
                    f"builds that are thinking (about {_gb(THINKING_BUILD_MB)} each); at most 4 "
                    f"→ {value}.")}


def current_level(connection: Any, cap: Optional[Capacity] = None) -> int:
    stored = _state(connection, "degradation").get("level")
    if isinstance(stored, int):
        return stored
    return level_of(cap) if cap is not None else L0


def admit_job(connection: Any, kind: str, *, cap: Optional[Capacity] = None) -> Decision:
    """The gate `jobs.claim_job` calls for a heavy job about to be leased."""
    profile_kind = profile_for_job(kind)
    if profile_kind is None:
        return Decision(admitted=True, reason="light work is never held back")
    # THE INSTALL-WIDE WORK HOLD (Infra Agent, BOSSAPP-1157): a heavy job waits while storage is
    # cleaned, with its own code so the console says "paused by Infra Agent", not "no room".
    from . import work_hold

    if work_hold.active(connection, kind):
        incident = work_hold.state(connection).get("incident_id")
        return Decision(admitted=False, code="infra_hold",
                        reason=f"paused by the Infra Agent while storage is cleaned ({incident})",
                        shortfall="storage is being cleaned")
    cap = cap or sample()
    # One heavy admission at a time across lease slots: the in-flight count and free memory
    # are read, then the lease commits; without this two slots admit against the same memory.
    # Held until the claim's transaction ends.
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('boss.admission'))"))
    profile = load_profile(connection, profile_kind)
    slots = builds_at_once(connection, cap, profile.request_mb)["value"]
    force_one = bool(_state(connection, "builds_at_once").get("force_one"))
    # A running build waiting to take its slot back goes first (heavy_slot): it counts as in
    # flight, so a new lease never takes the slot it is waiting for.
    counts = heavy_counts(connection)
    return decide(cap, profile, in_flight=counts["holding"] + counts["waiting"], slots=slots,
                  level=current_level(connection, cap), force_one=force_one,
                  running_total=counts["total"])


def snapshot(connection: Any, *, cap: Optional[Capacity] = None) -> dict[str, Any]:
    """The admission state the System health view and the card read."""
    cap = cap or sample()
    profile = load_profile(connection, "code.task")
    bao = builds_at_once(connection, cap, profile.request_mb)
    counts = heavy_counts(connection)
    in_flight = counts["holding"]
    queued = int(connection.execute(text(
        "SELECT count(*) FROM jobs WHERE kind = ANY(:k) AND status = 'queued' "
        "AND last_error->>'code' = 'host_capacity'"),
        {"k": sorted(HEAVY_JOB_KINDS)}).scalar() or 0)
    probe = decide(cap, profile, in_flight=in_flight + counts["waiting"], slots=bao["value"],
                   level=current_level(connection, cap), running_total=counts["total"])
    label = f"Builds at once: {bao['value']} ({'auto' if bao['auto'] else 'pinned'})"
    waiting = f" · {queued} queued, waiting for {probe.shortfall}" if queued and probe.shortfall \
        else (f" · {queued} queued" if queued else "")
    # JOB SLOTS: what the operator saved in Setup › Agent capacity; until then derived from
    # this machine (derived_worker_slots): builds give their heavy slot back while they think,
    # so a small host runs more builds than heavy slots without running more heavy work.
    configured_workers = _state(connection, "agent_capacity").get("worker_slots")
    auto_workers = derived_worker_slots(cap, int(bao["value"] or 1), profile.request_mb)
    if type(configured_workers) is int:
        workers = max(1, min(4, configured_workers))
        workers_why = f"Saved in Setup. Auto would be {auto_workers['value']}: {auto_workers['why']}"
    else:
        workers, workers_why = int(auto_workers["value"]), auto_workers["why"]
    if bao.get("boost") and workers < min(4, int(bao["value"])):
        # A heavy slot the doctor added is never left idle for want of a job slot.
        workers = min(4, int(bao["value"]))
        workers_why = f"Raised to {workers} with the heavy builds the system doctor added. " + workers_why
    return {"worker_slots": workers, "worker_slots_auto": type(configured_workers) is not int,
            "worker_slots_why": workers_why, "builds_at_once": bao, "running": in_flight,
            "thinking": counts["light"] + counts["waiting"],
            "waiting_for_slot": counts["waiting"], "queued_for_capacity": queued,
            "next_build": probe.as_dict(), "label": label + waiting,
            "level": current_level(connection, cap)}


def respawn_allowed(engine: Any, settings: Any = None) -> bool:
    """Boot respawn of a preview only with headroom and below the pressure rung (spec §4a.6)."""
    cap = sample()
    with engine.begin() as c:
        if current_level(c, cap) >= L1:
            return False
        prof = load_profile(c, "preview_node")
    return cap.total_mb <= 0 or cap.spare_mb >= prof.request_mb
