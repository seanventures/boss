"""THE DOCTOR ACTS ON A QUEUE THE MACHINE COULD SERVE (enterprise validation, operator 2026-11-27:
"why are our BOSS agents not acting while the build is queued so long — all agents should be
always on").

WHAT HAPPENED. On a 4 CPU / 8 GiB host builds sat queued 3.4-3.6 h for the one heavy-build slot
while, at points, the machine showed 0.02-0.05 load per core and 25% of its memory in use. Every
always-on loop looked elsewhere: the system doctor only ever LOWERED capacity (one at a time
under memory pressure); the storage watch reads disk; the Task Shepherd's sweep reads failed
runs and its watch reads running ones; `stuck_work.queue_starved` fires only when no worker is
alive. A queue waiting on a slot while the host idled was nobody's finding.

WHAT THE DOCTOR NOW DOES (every tick):

  * work has waited for a heavy slot past QUEUE_WAIT_SECONDS (since its latest deferral or
    requeue) AND the host has had headroom for SUSTAIN (degradation L0, CPU load per core <
    HEADROOM_LOAD, memory spare for one more build, no memory stall, disk above its floor +
    DISK_MARGIN_GB, every one of those READ — an unreadable reading never adds capacity) → ONE
    more heavy slot, never above the measured value + 1 (`ceiling`), at most one step per
    RAISE_EVERY;
  * headroom gone — memory pressure at once, CPU load ≥ DRAIN_LOAD sustained for SUSTAIN → one
    fewer, by draining: running builds finish their heavy step, nothing is stopped. After a
    lowering the next raise waits LOWER_BACKOFF, doubling on each lowering in the episode, so a
    bursty load does not flap;
  * the queue empty for IDLE_RESET_SECONDS → back to the measured value; the episode ends;
  * A PERSON'S PIN IS A CEILING (ENT-CONC-04 "capacity limits are respected"): a pinned "Builds
    at once" is never raised. When the pin is what holds queued work on an idle host, the doctor
    says so on the queued build and in one notice ("Your limit of 1 is holding 2 builds; the
    machine has room for 2") and changes nothing;
  * work waits and the host is truly busy → the reason (and a typical build's length) on the
    queued build, and one notice per episode, never silently.

One "Decided for you" row per episode, updated as the episode goes on (raise, drain, reset), with
an Undo while the boost stands: Undo drains it and keeps the doctor quiet for UNDO_QUIET_HOURS.
Turn it off for good by pinning "Builds at once" in Setup › Agent capacity.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import admission

STATE_KEY = "capacity_boost"
QUEUE_WAIT_SECONDS = 10 * 60
SUSTAIN = timedelta(minutes=10)
RAISE_EVERY = timedelta(minutes=5)
LOWER_EVERY = timedelta(minutes=1)
LOWER_BACKOFF = timedelta(minutes=30)
MAX_BACKOFF = timedelta(hours=8)
IDLE_RESET_SECONDS = 15 * 60
HEADROOM_LOAD = 0.75
DRAIN_LOAD = 1.25
DISK_MARGIN_GB = 2.0
UNDO_QUIET_HOURS = 24
AGENT = "system_doctor"


@dataclass(frozen=True)
class Plan:
    action: str            # raise | lower | reset | busy | pinned | none
    boost: int             # the boost after the action
    why: str
    estimate_minutes: Optional[int] = None
    #: What the steward remembers for the next look (room_since, hot_since, backoff…).
    memo: dict[str, Any] = field(default_factory=dict)


def ceiling(cap: admission.Capacity, request_mb: float, *, derived: int) -> int:
    """The most heavy builds a boost may reach: the measured value + 1, and never more than one
    per core or than memory holds."""
    if cap.total_mb <= 0 or request_mb <= 0:
        return max(1, int(derived))
    by_memory = math.floor((cap.total_mb - cap.reserve_mb) / request_mb)
    return int(max(1, min(int(derived) + 1, by_memory, cap.cores, admission.MAX_SLOTS)))


def headroom(cap: admission.Capacity, level: int, request_mb: float) -> tuple[bool, str]:
    """(room for one more heavy build, the reason in words when not). Pure."""
    if cap.unknown:
        return False, (f"BOSS could not read this machine's {', '.join(cap.unknown)}, so it adds "
                       "no capacity on a guess")
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


def _at(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def plan(*, cap: admission.Capacity, level: int, request_mb: float, base: int, derived: int,
         boost: int, oldest_wait_s: float, queue_empty_s: float, now: datetime,
         pinned: Optional[int] = None, waiting: int = 1,
         memo: Optional[Mapping[str, Any]] = None,
         typical_minutes: Optional[int] = None) -> Plan:
    """Pure: what the doctor does about capacity on this look, and what it remembers."""
    m = dict(memo or {})
    room, short = headroom(cap, level, request_mb)
    memory_short = (not cap.unknown) and (
        level >= admission.L1 or (cap.total_mb > 0 and cap.spare_mb < request_mb / 2))
    load_hot = (not cap.unknown) and cap.load_per_core >= DRAIN_LOAD
    m["room_since"] = (m.get("room_since") or now.isoformat()) if room else None
    m["hot_since"] = (m.get("hot_since") or now.isoformat()) if load_hot else None
    room_for = now - (_at(m["room_since"]) or now)
    hot_for = now - (_at(m["hot_since"]) or now)
    changed = _at(m.get("changed_at"))
    since = (now - changed) if changed else timedelta.max

    def done(action: str, new_boost: int, why: str, **extra: Any) -> Plan:
        if action in ("raise", "lower", "reset"):
            m["changed_at"] = now.isoformat()
        return Plan(action, new_boost, why, memo=m, **extra)

    if boost > 0 and since >= LOWER_EVERY and (memory_short or hot_for >= SUSTAIN):
        lowered = _at(m.get("lowered_at"))
        previous = timedelta(minutes=float(m.get("backoff_minutes") or 0))
        backoff = (min(MAX_BACKOFF, previous * 2) if lowered and previous
                   and now - lowered < MAX_BACKOFF else LOWER_BACKOFF)
        m["lowered_at"], m["backoff_minutes"] = now.isoformat(), backoff.total_seconds() / 60
        return done("lower", boost - 1,
                    f"headroom is gone: {short or 'the machine is busy'}; one fewer heavy build "
                    "at once, by draining (nothing running is stopped)")
    if boost > 0 and queue_empty_s >= IDLE_RESET_SECONDS:
        m["backoff_minutes"], m["lowered_at"] = 0, None
        return done("reset", 0, f"nothing has waited for a heavy slot for "
                    f"{int(queue_empty_s // 60)} minutes; back to {base} at once")
    if oldest_wait_s < QUEUE_WAIT_SECONDS:
        return done("none", boost, "")
    if pinned is not None:
        # A person's limit is a ceiling: said, never changed.
        room_for_builds = max(int(derived), base)
        if room and room_for_builds > pinned:
            return done("pinned", boost,
                        f"Your limit of {pinned} is holding {waiting} "
                        f"build{'s' if waiting != 1 else ''}; the machine has room for "
                        f"{room_for_builds}. Raise it or set it to Auto in Setup › Agent capacity")
        if not room:
            return done("busy", boost, f"BOSS is not adding a heavy slot: {short}",
                        estimate_minutes=typical_minutes)
        return done("none", boost, "")
    top = ceiling(cap, request_mb, derived=derived)
    if not room:
        return done("busy", boost, f"BOSS is not adding a heavy slot: {short}",
                    estimate_minutes=typical_minutes)
    if base + boost >= top:
        return done("busy", boost,
                    f"BOSS is not adding a heavy slot: {base + boost} at once is the most it runs "
                    f"on this machine", estimate_minutes=typical_minutes)
    quiet = _at(m.get("quiet_until"))
    lowered = _at(m.get("lowered_at"))
    backoff = timedelta(minutes=float(m.get("backoff_minutes") or 0))
    if quiet and now < quiet:
        return done("none", boost, "")
    if lowered and backoff and now - lowered < backoff:
        return done("none", boost, "")
    if room_for < SUSTAIN or since < RAISE_EVERY:
        return done("none", boost, "")
    return done("raise", boost + 1,
                f"work has waited {int(oldest_wait_s // 60)} minutes for a heavy slot while the "
                f"machine had room for {int(room_for.total_seconds() // 60)} minutes (CPU load "
                f"{cap.load_per_core:.2f} per core, {cap.spare_mb / 1024:.1f} GB free); "
                f"{base + boost + 1} heavy builds at once, at most {top} on this machine")


# ------------------------------------------------------------------------------------------ #
# reading and applying
# ------------------------------------------------------------------------------------------ #

def state(connection: Any) -> dict[str, Any]:
    return admission._state(connection, STATE_KEY)


def boost_of(connection: Any) -> int:
    value = state(connection).get("boost")
    return int(value) if isinstance(value, int) and value > 0 else 0


def queue_waits(connection: Any) -> list[dict[str, Any]]:
    """Heavy work waiting for a slot — queued jobs held for capacity, and running builds waiting
    to take their slot back (heavy_slot) — longest first, each timed from when THIS wait began:
    its latest deferral episode or requeue (a retry is not a wait since it was first created)."""
    kinds = sorted(admission.HEAVY_JOB_KINDS)
    rows = connection.execute(text(
        "SELECT j.id, j.project_id, j.payload->>'run_id' AS run_id, "
        "EXTRACT(EPOCH FROM (now() - GREATEST("
        "  COALESCE(CAST(j.last_error->>'deferred_since' AS timestamptz), j.created_at), "
        "  COALESCE((SELECT max(e.created_at) FROM events e WHERE e.entity_id = j.id "
        "    AND e.event_type IN ('job.leased', 'job.handed_back', 'job.lease_reclaimed')), "
        "    j.created_at)))) AS waited FROM jobs j "
        "WHERE j.kind = ANY(:k) AND j.status = 'queued' "
        "AND j.last_error->>'code' = 'host_capacity' LIMIT 50"),
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
        bao = admission.builds_at_once(c, cap, profile.request_mb, boosted=False)
        base = int(bao["value"])
        waits = queue_waits(c)
        oldest = float(waits[0]["waited"]) if waits else 0.0
        empty_since = _at(st.get("queue_empty_since"))
        if waits:
            empty_since = None
        elif empty_since is None:
            empty_since = now
        typical = None
        if waits and waits[0].get("project_id"):
            from .stuck_work import _typical_minutes

            try:
                with c.begin_nested():
                    typical = _typical_minutes(c, waits[0]["project_id"])
            except Exception:  # noqa: BLE001
                typical = None
        p = plan(cap=cap, level=admission.current_level(c, cap), request_mb=profile.request_mb,
                 base=base, derived=int(bao["derived"]), boost=boost,
                 pinned=None if bao["auto"] else int(bao["pinned"] or base),
                 waiting=len(waits), oldest_wait_s=oldest,
                 queue_empty_s=(now - empty_since).total_seconds() if empty_since else 0.0,
                 now=now, memo=st, typical_minutes=typical)
        said = p.action in ("busy", "pinned")
        new = {**st, **p.memo, "boost": p.boost,
               "queue_empty_since": empty_since.isoformat() if empty_since else None,
               "busy": p.why if said else None, "busy_kind": p.action if said else None,
               "busy_estimate_minutes": p.estimate_minutes if said else None,
               "checked_at": now.isoformat()}
        if p.action in ("raise", "lower", "reset"):
            new.update({"last_action": p.action, "last_why": p.why})
            new["episode_decision_id"] = _record(c, p, base=base, before=boost, waits=waits,
                                                 episode=st.get("episode_decision_id"))
            if p.action == "reset":
                new["episode_decision_id"] = None
        admission.set_state(c, STATE_KEY, new)
    return p


def _record(c: Any, p: Plan, *, base: int, before: int, waits: list[dict[str, Any]],
            episode: Optional[str]) -> Optional[str]:
    """ONE "Decided for you" row per episode, in the project whose work waited longest
    (capacity is install-wide; that is where a person looks): the first change writes it, the
    later ones (drain, reset) update it with the history, so a busy day is one row, not twenty."""
    from .autonomy_gate import NONE_RECORD, Route, record_decided
    from .core import emit_event

    words = {"raise": f"Run {base + p.boost} heavy builds at once (was {base + before})",
             "lower": f"Run {base + p.boost} heavy builds at once (was {base + before}), draining",
             "reset": f"Back to {base} heavy builds at once (was {base + before})"}[p.action]
    step = {"action": p.action, "boost": p.boost, "why": p.why,
            "at": datetime.now(timezone.utc).isoformat()}
    undo = "restore_capacity" if p.boost > 0 else NONE_RECORD
    row = None
    if episode:
        row = c.execute(text("SELECT id, project_id, recommendation FROM decisions WHERE id = :d"),
                        {"d": episode}).mappings().first()
    if row is not None:
        rec = row["recommendation"] if isinstance(row["recommendation"], dict) else json.loads(
            row["recommendation"] or "{}")
        auto = dict(rec.get("autonomy") or {})
        if not auto.get("undone"):
            auto["undo"] = undo
        rec.update({"boost_after": p.boost, "steps": (rec.get("steps") or [])[-19:] + [step],
                    "autonomy": auto})
        c.execute(text(
            "UPDATE decisions SET question = :q, recommendation = CAST(:r AS JSONB), "
            "resolution_note = :n, updated_at = now() WHERE id = :d"),
            {"q": words[:500], "r": json.dumps(rec, default=str), "d": row["id"],
             "n": f"Decided by the system doctor: {p.why}"[:900]})
        emit_event(c, str(row["project_id"]), "capacity.steward_acted", AGENT, "system", STATE_KEY,
                   {"action": p.action, "boost_before": before, "boost_after": p.boost,
                    "base": base, "why": p.why, "decision_id": row["id"]})
        return str(row["id"])
    project_id = next((w.get("project_id") for w in waits if w.get("project_id")), None)
    if not project_id:
        return None
    detail = {"boost_before": before, "boost_after": p.boost, "base": base, "steps": [step],
              "run_id": next((w.get("run_id") for w in waits if w.get("run_id")), None)}
    if p.action == "raise":
        recommendation = {**detail, "action": "capacity_raise"}
    elif p.action == "lower":
        recommendation = {**detail, "action": "capacity_lower"}
    else:
        recommendation = {**detail, "action": "capacity_reset"}
    decision_id = record_decided(
        c, str(project_id), task_id=None, question=words, recommendation=recommendation,
        found=Route(answer="act", cls="capacity", why=p.why, agent=AGENT, undo=undo))
    emit_event(c, str(project_id), "capacity.steward_acted", AGENT, "system", STATE_KEY,
               {"action": p.action, "boost_before": before, "boost_after": p.boost,
                "base": base, "why": p.why, "decision_id": decision_id})
    return decision_id


def undo_raise(connection: Any, *, actor: str) -> str:
    """A person undid the doctor's capacity change: the boost goes (draining), the episode ends,
    and the doctor does not raise again for UNDO_QUIET_HOURS."""
    st = state(connection)
    now = datetime.now(timezone.utc)
    admission.set_state(connection, STATE_KEY, {
        **st, "boost": 0, "changed_at": now.isoformat(), "last_action": "undone",
        "last_why": f"undone by {actor}", "episode_decision_id": None,
        "quiet_until": (now + timedelta(hours=UNDO_QUIET_HOURS)).isoformat()})
    return (f"Back to the measured number of heavy builds; BOSS will not add one for "
            f"{UNDO_QUIET_HOURS} h. Running builds finish their step.")


def busy_note(connection: Any) -> Optional[str]:
    """What the doctor said on its last look when it could not (or may not) add a slot."""
    st = state(connection)
    at = _at(st.get("checked_at"))
    if not st.get("busy") or at is None or datetime.now(timezone.utc) - at > timedelta(minutes=5):
        return None
    eta = st.get("busy_estimate_minutes")
    return str(st["busy"]) + (f" (a build here typically takes about {eta} minutes)"
                              if isinstance(eta, int) and eta > 0 else "")


__all__ = ["Plan", "busy_note", "ceiling", "headroom", "plan", "queue_waits", "tick",
           "undo_raise"]
