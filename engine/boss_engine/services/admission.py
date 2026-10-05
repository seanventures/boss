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
    free disk ≥ 5 GB
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

MIN_FREE_DISK_GB = 5.0
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


def sample() -> Capacity:
    """What this API container can see right now (Linux /proc and cgroup v2)."""
    import shutil

    from .self_observability import cpu_snapshot, memory_snapshot

    mem, cpu = memory_snapshot(), cpu_snapshot()
    # statvfs only: this runs on every lease poll, so never walk the preview trees here.
    try:
        usage = shutil.disk_usage("/")
        disk = {"free_gb": round(usage.free / 1e9, 1),
                "used_pct": round(100 * usage.used / usage.total, 1) if usage.total else 0.0}
    except OSError:
        disk = {}
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
    if cap.total_mb <= 0 or request_mb <= 0:
        return 1
    room = cap.total_mb - cap.reserve_mb - previews_mb
    by_memory = math.floor(room / request_mb)
    by_cpu = max(1, cap.cores // 2)
    return int(max(1, min(by_memory, by_cpu, MAX_SLOTS)))


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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gb(mb: float) -> str:
    return f"{mb / 1024:.1f} GB"


def decide(cap: Capacity, profile: Profile, *, in_flight: int, slots: int,
           level: int = L0, force_one: bool = False) -> Decision:
    """Pure: admit or defer one heavy job. Every refusal says what is short, in GB."""
    problems: list[str] = []
    if cap.total_mb > 0 and cap.spare_mb < profile.request_mb:
        problems.append(f"needs {_gb(profile.request_mb)}, {_gb(cap.spare_mb)} free")
    if cap.psi_full_avg60 >= MAX_PSI_FULL_AVG60:
        problems.append(f"the machine waited on memory {cap.psi_full_avg60:.0f}% of the last "
                        "minute")
    if cap.disk_free_gb < MIN_FREE_DISK_GB:
        problems.append(f"{cap.disk_free_gb:.1f} GB of disk free (needs {MIN_FREE_DISK_GB:.0f})")
    if cap.load_per_core >= MAX_LOAD_PER_CORE:
        problems.append(f"CPU load {cap.load_per_core:.1f} per core")
    if level >= L2:
        problems.append("the system doctor has paused new builds until pressure eases")
    # NOTHING RUNNING MEANS ONE MAY RUN. A small or busy machine runs one build at a time
    # rather than none forever. Critical pressure (L2) still holds it — unless the operator
    # chose "one at a time" / "build anyway" on the card (force_one); sustained critical (L3)
    # and a nearly full disk hold even that.
    if problems and in_flight == 0 and cap.disk_free_gb >= 1.0 and (
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
                    shortfall=shortfall,
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


def heavy_in_flight(connection: Any) -> int:
    kinds = sorted(HEAVY_JOB_KINDS)
    return int(connection.execute(text(
        "SELECT count(*) FROM jobs WHERE kind = ANY(:k) AND status IN ('leased', 'running') "
        "AND (lease_expires_at IS NULL OR lease_expires_at > now())"), {"k": kinds}).scalar() or 0)


def running_previews_mb(connection: Any) -> float:
    try:
        n = int(connection.execute(text(
            "SELECT count(*) FROM app_previews WHERE status = 'running'")).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0.0
    from .workload_profiles import DEFAULTS
    return n * DEFAULTS["preview_node"].request_mb


def builds_at_once(connection: Any, cap: Capacity, request_mb: float) -> dict[str, Any]:
    """{value, auto, derived, pinned, warning} — what the console shows as "Builds at once"."""
    derived = derived_slots(cap, request_mb, previews_mb=running_previews_mb(connection))
    pinned = _state(connection, "builds_at_once").get("value")
    if isinstance(pinned, int) and pinned >= 1:
        value = min(pinned, derived * 2, MAX_SLOTS)
        warning = (f"Pinned at {pinned}; this machine measures room for {derived}."
                   if pinned > derived else None)
        return {"value": value, "auto": False, "derived": derived, "pinned": pinned,
                "warning": warning}
    return {"value": derived, "auto": True, "derived": derived, "pinned": None,
            "warning": None}


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
    cap = cap or sample()
    # One heavy admission at a time across lease slots: the in-flight count and free memory
    # are read, then the lease commits; without this two slots admit against the same memory.
    # Held until the claim's transaction ends.
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('boss.admission'))"))
    profile = load_profile(connection, profile_kind)
    slots = builds_at_once(connection, cap, profile.request_mb)["value"]
    force_one = bool(_state(connection, "builds_at_once").get("force_one"))
    return decide(cap, profile, in_flight=heavy_in_flight(connection), slots=slots,
                  level=current_level(connection, cap), force_one=force_one)


def snapshot(connection: Any, *, cap: Optional[Capacity] = None) -> dict[str, Any]:
    """The admission state the System health view and the card read."""
    cap = cap or sample()
    profile = load_profile(connection, "code.task")
    bao = builds_at_once(connection, cap, profile.request_mb)
    in_flight = heavy_in_flight(connection)
    queued = int(connection.execute(text(
        "SELECT count(*) FROM jobs WHERE kind = ANY(:k) AND status = 'queued' "
        "AND last_error->>'code' = 'host_capacity'"),
        {"k": sorted(HEAVY_JOB_KINDS)}).scalar() or 0)
    probe = decide(cap, profile, in_flight=in_flight, slots=bao["value"],
                   level=current_level(connection, cap))
    label = f"Builds at once: {bao['value']} ({'auto' if bao['auto'] else 'pinned'})"
    waiting = f" · {queued} queued, waiting for {probe.shortfall}" if queued and probe.shortfall \
        else (f" · {queued} queued" if queued else "")
    return {"builds_at_once": bao, "running": in_flight, "queued_for_capacity": queued,
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
