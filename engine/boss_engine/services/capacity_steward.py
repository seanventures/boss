"""THE DOCTOR ACTS ON A QUEUE THE MACHINE COULD SERVE (enterprise validation, operator 2026-11-27:
"why are our BOSS agents not acting while the build is queued so long — all agents should be
always on").

WHAT HAPPENED. On a 4 CPU / 8 GiB host builds sat queued 3.4-3.6 h for the one heavy-build slot
while, at points, the machine showed 0.02-0.05 load per core and 25% of its memory in use. Every
always-on loop looked elsewhere: the system doctor only ever LOWERED capacity (one at a time
under memory pressure); the storage watch reads disk; the Task Shepherd's sweep reads failed
runs and its watch reads running ones; `stuck_work.queue_starved` fires only when no worker is
alive. A queue waiting on a slot while the host idled was nobody's finding.

WHAT THE DOCTOR NOW DOES (every tick, at most one change per RAISE_EVERY / LOWER_EVERY):

  * work has waited for a heavy slot past QUEUE_WAIT_SECONDS AND the host has measured headroom
    (degradation L0, CPU load per core < HEADROOM_LOAD, memory spare for one more build, no
    memory stall, disk above its floor + DISK_MARGIN_GB) → one more heavy slot, up to the
    measured CEILING (one per core, memory permitting, never above admission.MAX_SLOTS), and
    recorded in "Decided for you" with an undo;
  * headroom disappears (L1+, load per core ≥ DRAIN_LOAD, memory short) → one fewer, by
    draining: running builds finish their heavy step, nothing is stopped;
  * the queue has been empty for IDLE_RESET_SECONDS → the boost goes back to the base value;
  * work waits and the host is truly busy → the doctor says so on the queued build (the reason)
    and once to a person (a `queue_waits_busy_host` notice, one per episode), never silently.

The boost (`system_state.capacity_boost`) sits on top of whatever "Builds at once" is — auto or
pinned — and never above what the host measures it can run. A person's Undo removes it and
keeps the doctor from raising it again for UNDO_QUIET_HOURS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from . import admission

STATE_KEY = "capacity_boost"
QUEUE_WAIT_SECONDS = 10 * 60
RAISE_EVERY = timedelta(minutes=5)
LOWER_EVERY = timedelta(minutes=1)
IDLE_RESET_SECONDS = 15 * 60
HEADROOM_LOAD = 0.75
DRAIN_LOAD = 1.25
DISK_MARGIN_GB = 2.0
UNDO_QUIET_HOURS = 24
AGENT = "system_doctor"


@dataclass(frozen=True)
class Plan:
    action: str            # raise | lower | reset | busy | none
    boost: int             # the boost after the action
    why: str
    estimate_minutes: Optional[int] = None


def ceiling(cap: admission.Capacity, request_mb: float) -> int:
    """The most heavy builds a boost may reach: one per core, memory permitting."""
    if cap.total_mb <= 0 or request_mb <= 0:
        return 1
    by_memory = math.floor((cap.total_mb - cap.reserve_mb) / request_mb)
    return int(max(1, min(by_memory, cap.cores, admission.MAX_SLOTS)))


def headroom(cap: admission.Capacity, level: int, request_mb: float) -> tuple[bool, str]:
    """(room for one more heavy build, the reason in words when not). Pure."""
    if level >= admission.L1:
        return False, f"the machine is under memory pressure ({admission.LEVEL_WORDS[level]})"
    if cap.load_per_core >= HEADROOM_LOAD:
        return False, f"CPU load is {cap.load_per_core:.2f} per core"
    if cap.total_mb > 0 and cap.spare_mb < request_mb:
        return False, (f"a build needs {request_mb / 1024:.1f} GB and "
                       f"{cap.spare_mb / 1024:.1f} GB is free")
    if cap.psi_full_avg60 >= 1.0:
        return False, "the machine waited on memory in the last minute"
    floor = admission.build_disk_floor_gb() + DISK_MARGIN_GB
    if cap.disk_free_gb < floor:
        return False, f"{cap.disk_free_gb:.0f} GB of disk is free"
    return True, ""


def plan(*, cap: admission.Capacity, level: int, request_mb: float, base: int, boost: int,
         oldest_wait_s: float, queue_empty_s: float, last_change: Optional[datetime],
         now: datetime, quiet_until: Optional[datetime] = None,
         typical_minutes: Optional[int] = None) -> Plan:
    """Pure: what the doctor does about capacity on this tick."""
    top = ceiling(cap, request_mb)
    room, short = headroom(cap, level, request_mb)
    since = (now - last_change) if last_change else timedelta.max
    pressure = (level >= admission.L1 or cap.load_per_core >= DRAIN_LOAD
                or (cap.total_mb > 0 and cap.spare_mb < request_mb / 2))
    if boost > 0 and pressure and since >= LOWER_EVERY:
        return Plan("lower", boost - 1, f"headroom is gone: {short or 'the machine is busy'}; "
                    "one fewer heavy build at once, by draining (nothing running is stopped)")
    if boost > 0 and queue_empty_s >= IDLE_RESET_SECONDS:
        return Plan("reset", 0, "nothing has waited for a heavy slot for "
                    f"{int(queue_empty_s // 60)} minutes; back to {base} at once")
    if oldest_wait_s < QUEUE_WAIT_SECONDS:
        return Plan("none", boost, "")
    if room and base + boost < top and since >= RAISE_EVERY and not (
            quiet_until and now < quiet_until):
        return Plan("raise", boost + 1,
                    f"work has waited {int(oldest_wait_s // 60)} minutes for a heavy slot while "
                    f"the machine has room (CPU load {cap.load_per_core:.2f} per core, "
                    f"{cap.spare_mb / 1024:.1f} GB free); {base + boost + 1} heavy builds at once, "
                    f"at most {top} on this machine")
    if not room:
        return Plan("busy", boost, f"BOSS is not adding a heavy slot: {short}",
                    estimate_minutes=typical_minutes)
    if base + boost >= top:
        return Plan("busy", boost,
                    f"BOSS is not adding a heavy slot: {base + boost} at once is the most this "
                    f"machine measures it can run", estimate_minutes=typical_minutes)
    return Plan("none", boost, "")


# ------------------------------------------------------------------------------------------ #
# reading and applying
# ------------------------------------------------------------------------------------------ #

def state(connection: Any) -> dict[str, Any]:
    return admission._state(connection, STATE_KEY)


def boost_of(connection: Any) -> int:
    value = state(connection).get("boost")
    return int(value) if isinstance(value, int) and value > 0 else 0


def _parse(at: Any) -> Optional[datetime]:
    if not at:
        return None
    try:
        out = datetime.fromisoformat(str(at))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def queue_waits(connection: Any) -> list[dict[str, Any]]:
    """Heavy work waiting for a slot: queued jobs held for capacity, and running builds waiting
    to take their slot back (heavy_slot), oldest first, with how long."""
    kinds = sorted(admission.HEAVY_JOB_KINDS)
    rows = connection.execute(text(
        "SELECT j.id, j.project_id, j.payload->>'run_id' AS run_id, "
        "EXTRACT(EPOCH FROM (now() - j.created_at)) AS waited FROM jobs j "
        "WHERE j.kind = ANY(:k) AND j.status = 'queued' "
        "AND j.last_error->>'code' = 'host_capacity' ORDER BY j.created_at LIMIT 20"),
        {"k": kinds}).mappings().all()
    out = [dict(r) for r in rows]
    try:
        with connection.begin_nested():
            mid = connection.execute(text(
                "SELECT h.job_id AS id, h.project_id, h.run_id, "
                "EXTRACT(EPOCH FROM (now() - h.waiting_since)) AS waited "
                "FROM heavy_slot_releases h JOIN jobs j ON j.id = h.job_id "
                "AND j.fence_epoch = h.fence_epoch WHERE h.state = 'waiting' "
                "AND j.status IN ('leased', 'running')")).mappings().all()
        out += [dict(r) for r in mid]
    except Exception:  # noqa: BLE001 — an older schema: queued jobs only
        pass
    return sorted(out, key=lambda r: -float(r.get("waited") or 0))


def tick(engine: Any, *, cap: Optional[admission.Capacity] = None,
         now: Optional[datetime] = None) -> Plan:
    """One look: plan, apply, record. Called by the system doctor's tick."""
    now = now or datetime.now(timezone.utc)
    cap = cap or admission.sample()
    with engine.begin() as c:
        c.execute(text("SELECT pg_advisory_xact_lock(hashtext('boss.capacity_steward'))"))
        st = state(c)
        boost = boost_of(c)
        profile = admission.load_profile(c, "code.task")
        base = int(admission.builds_at_once(c, cap, profile.request_mb, boosted=False)["value"])
        waits = queue_waits(c)
        oldest = float(waits[0]["waited"]) if waits else 0.0
        empty_since = _parse(st.get("queue_empty_since"))
        if waits:
            empty_since = None
        elif empty_since is None:
            empty_since = now
        level = admission.current_level(c, cap)
        typical = None
        if waits and waits[0].get("project_id"):
            from .stuck_work import _typical_minutes

            try:
                with c.begin_nested():
                    typical = _typical_minutes(c, waits[0]["project_id"])
            except Exception:  # noqa: BLE001
                typical = None
        p = plan(cap=cap, level=level, request_mb=profile.request_mb, base=base, boost=boost,
                 oldest_wait_s=oldest,
                 queue_empty_s=(now - empty_since).total_seconds() if empty_since else 0.0,
                 last_change=_parse(st.get("changed_at")), now=now,
                 quiet_until=_parse(st.get("quiet_until")), typical_minutes=typical)
        new = {**st, "boost": p.boost,
               "queue_empty_since": empty_since.isoformat() if empty_since else None,
               "busy": p.why if p.action == "busy" else None,
               "busy_estimate_minutes": p.estimate_minutes if p.action == "busy" else None,
               "checked_at": now.isoformat()}
        if p.action in ("raise", "lower", "reset"):
            new.update({"changed_at": now.isoformat(), "last_action": p.action,
                        "last_why": p.why})
        admission.set_state(c, STATE_KEY, new)
        if p.action in ("raise", "lower", "reset"):
            _record(c, p, base=base, before=boost, waits=waits)
    return p


_ACTIONS = {"raise": "capacity_raise", "lower": "capacity_lower", "reset": "capacity_reset"}


def _record(c: Any, p: Plan, *, base: int, before: int, waits: list[dict[str, Any]]) -> None:
    """The change, in "Decided for you" of the project whose work waited longest (capacity is
    install-wide; that is where a person looks), and as an install event."""
    from .autonomy_gate import NONE_RECORD, Route, record_decided
    from .core import emit_event

    project_id = next((w.get("project_id") for w in waits if w.get("project_id")), None)
    if project_id is None:
        project_id = c.execute(text(
            "SELECT project_id FROM decisions WHERE approved_by LIKE 'autonomy:dev:system_doctor%' "
            "ORDER BY created_at DESC LIMIT 1")).scalar()
    words = {"raise": f"Run {base + p.boost} heavy builds at once (was {base + before})",
             "lower": f"Run {base + p.boost} heavy builds at once (was {base + before}), draining",
             "reset": f"Back to {base} heavy builds at once (was {base + before})"}[p.action]
    if project_id:
        record_decided(
            c, str(project_id), task_id=None, question=words,
            recommendation={"action": _ACTIONS[p.action], "boost_before": before,
                            "boost_after": p.boost, "base": base,
                            "run_id": next((w.get("run_id") for w in waits if w.get("run_id")),
                                           None)},
            found=Route(answer="act", cls="capacity", why=p.why, agent=AGENT,
                        undo="restore_capacity" if p.action == "raise" else NONE_RECORD))
        emit_event(c, str(project_id), "capacity.steward_acted", AGENT, "system", STATE_KEY,
                   {"action": p.action, "boost_before": before, "boost_after": p.boost,
                    "base": base, "why": p.why})


def undo_raise(connection: Any, *, actor: str) -> str:
    """A person undid a raise: the boost goes, and the doctor does not raise it again for
    UNDO_QUIET_HOURS. Draining, as every lowering is."""
    st = state(connection)
    now = datetime.now(timezone.utc)
    admission.set_state(connection, STATE_KEY, {
        **st, "boost": 0, "changed_at": now.isoformat(), "last_action": "undone",
        "last_why": f"undone by {actor}",
        "quiet_until": (now + timedelta(hours=UNDO_QUIET_HOURS)).isoformat()})
    return (f"Back to the set number of heavy builds; BOSS will not add one for "
            f"{UNDO_QUIET_HOURS} h. Running builds finish their step.")


def busy_note(connection: Any) -> Optional[str]:
    """What the doctor said on its last look when it could not add a slot, else None."""
    st = state(connection)
    at = _parse(st.get("checked_at"))
    if not st.get("busy") or at is None or datetime.now(timezone.utc) - at > timedelta(minutes=5):
        return None
    eta = st.get("busy_estimate_minutes")
    return str(st["busy"]) + (f" (a build here typically takes about {eta} minutes)"
                              if isinstance(eta, int) and eta > 0 else "")


__all__ = ["Plan", "busy_note", "ceiling", "headroom", "plan", "queue_waits", "tick",
           "undo_raise"]
