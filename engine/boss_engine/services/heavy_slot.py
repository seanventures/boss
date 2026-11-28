"""THE HEAVY-BUILD SLOT IS HELD FOR HEAVY WORK, NOT FOR THINKING (enterprise validation).

WHY. "Builds at once" (admission) counted a build's job from lease to end. A build is mostly a
model thinking: on a 4 CPU / 8 GiB host with one heavy slot, one build held the slot ~1.5 h —
its design, its review panel, its acceptance reads included — while two builds sat 12,361 s and
12,876 s in the queue before their first lease. On the laptop's own runs (30 days, 277 builds,
80 h of lease) 10% of the lease was the thinking stages before a build writes (design, spec,
plan, plan review) and 12% the review panel; only the first is given back (review seats keep a
shell that can run the suite), and new leases no longer wait behind a build that is thinking.

THE RULE. A running build gives its slot back while it only reads and thinks, and takes it back
before it works the machine again:

  * a READ-ONLY model session of a THINKING stage (design, standards, spec, plan, review_plan,
    plan_coverage — LIGHT_STAGES), a plain verdict (no tools) and web research are `light`: once
    one has run RELEASE_AFTER_SECONDS with nothing heavy alongside, the slot is released (a
    ten-second verdict never gives the slot away mid-build). Review seats are read-only but
    hold a shell that can run the suite (review PR #367), so they stay heavy;
  * only the scope that gave the slot back may keep it given back: when it ends the build takes
    the slot again, whatever other light sessions (an abandoned seat) are still open, and a
    heavy step ending never gives it away again;
  * a WRITING session (the coder, the QA writer, a repair round) and every BOSS sandbox command
    (install, tests, build, app start) is `heavy`: it waits for the slot first if it was given
    back;
  * between them the build HOLDS, as before: when the last light session ends, the build takes
    the slot back before its next step, so un-instrumented work (git, a preview start) never
    runs without it.

Taking it back goes in order: a build that is already running and wants its slot back goes
before a new lease (admission counts waiting builds as in flight), oldest first. Only the slot
count gates a running build — an in-flight build was never stopped for memory or load before,
and is not now; host checks still gate new leases. Lowering "Builds at once" drains: holders
finish their heavy step, waiters wait.

QUEUE TIME IS NOT WORK TIME. The wait runs under `stage_clock.waiting_for_a_slot` (stage budgets
and review-seat deadlines exclude it) and is recorded as `code_task.heavy_slot_acquired`
{waited_seconds}, which `step_recovery.runs_queued_seconds` adds to the queue time every repair
clock excludes (ENT-REC-39).

STATE. `heavy_slot_releases` (one row per job, for its current fence_epoch): no row = holding
(every older code path and every other heavy kind keeps today's meaning); `light` = released;
`waiting` = released and wants it back. A row from a dead process never counts (a new lease
moves the epoch on; a lapsed lease is not in flight). The in-process `_Track` keeps the
per-run counts, since review seats and the panel's verify run in threads of one build.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

#: A light session gives the slot back only after it has run this long with nothing heavy
#: alongside: design and review panels take minutes; a one-turn verdict takes seconds and must
#: not hand the slot to another build in the middle of a heavy stage.
RELEASE_AFTER_SECONDS = 45.0
#: How often a build waiting for its slot asks again.
POLL_SECONDS = 5.0
#: How often a waiting build marks its run as alive (lists call a quiet run "Stalled").
TOUCH_SECONDS = 60.0

#: Job kinds whose builds give the slot back while they think.
TRACKED_KINDS = ("code.task",)


#: Read-only stages that only think: their sessions may give the slot back. Everything else —
#: review seats, check_tests, acceptance, any stage not named — holds it.
LIGHT_STAGES = frozenset({"design", "standards", "spec", "plan", "review_plan",
                          "plan_coverage", "research"})


def _run_fenced_base():
    from .run_fencing import RunFenced

    return RunFenced


class HeavySlotLost(_run_fenced_base()):
    """The build stopped owning its job (a reclaim) or the run already ended elsewhere while it
    waited for the slot. A RunFenced: the pipeline stands down quietly — no failed status, no
    partial PR — exactly as a fenced duplicate execution does."""

    def __init__(self, run_id: str, why: str) -> None:
        super().__init__(run_id, f"the heavy-build slot wait ({why})")


def stage_is_light(stage: Optional[str]) -> bool:
    return str(stage or "") in LIGHT_STAGES


def read_only_tools() -> str:
    """The tool list of a read-only CLI/SDK session: a thinking stage that gives the slot back
    gets no shell, so it cannot run a suite without the slot (re-review PR #367); every other
    read-only session (review seats, check_tests) keeps Bash and holds the slot."""
    return "Read,Grep,Glob" if stage_is_light(stage_now()) else "Read,Grep,Glob,Bash"


def stage_now() -> Optional[str]:
    try:
        from .llm_telemetry import get_llm_context

        return get_llm_context().get("stage")
    except Exception:  # noqa: BLE001
        return None


@dataclass
class _Track:
    engine: Any
    run_id: str
    job_id: str
    epoch: int
    project_id: Optional[str]
    cond: threading.Condition = field(default_factory=threading.Condition)
    light: int = 0
    heavy: int = 0
    released: bool = False
    acquiring: bool = False
    #: The generation of the light scope that may give the slot back (None: none open).
    opener: Optional[int] = None
    generation: int = 0
    waited_total: float = 0.0


_TRACKS: dict[str, _Track] = {}
_LOCK = threading.Lock()


def _run_id_now() -> Optional[str]:
    try:
        from .llm_telemetry import get_llm_context

        rid = get_llm_context().get("run_id")
    except Exception:  # noqa: BLE001
        return None
    return str(rid) if rid else None


def _track(run_id: Optional[str]) -> Optional[_Track]:
    rid = run_id or _run_id_now()
    if not rid:
        return None
    with _LOCK:
        return _TRACKS.get(str(rid))


def _hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _find_job(connection: Any, run_id: str, lease_token: Optional[str]) -> Optional[dict]:
    params: dict[str, Any] = {"r": run_id, "k": list(TRACKED_KINDS)}
    where = ("kind = ANY(:k) AND payload->>'run_id' = :r AND status IN ('leased', 'running') "
             "AND (lease_expires_at IS NULL OR lease_expires_at > now())")
    if lease_token:
        where += " AND lease_token_hash = :h"
        params["h"] = _hash(lease_token)
    row = connection.execute(text(
        f"SELECT id, fence_epoch, project_id FROM jobs WHERE {where} "
        "ORDER BY heartbeat_at DESC NULLS LAST LIMIT 1"), params).mappings().first()
    return dict(row) if row else None


@contextlib.contextmanager
def tracking(engine: Any, run_id: str, *, lease_token: Optional[str] = None) -> Iterator[None]:
    """This process executes `run_id` under its leased job: light/heavy scopes of the run now
    give back and take back the job's heavy slot. Inert when no leased job is found (a direct
    call, a test) or the table is missing; whatever happens, the job HOLDS again at the end."""
    track = None
    try:
        with engine.begin() as connection:
            job = _find_job(connection, str(run_id), lease_token)
        if job is not None:
            track = _Track(engine=engine, run_id=str(run_id), job_id=str(job["id"]),
                           epoch=int(job["fence_epoch"] or 0),
                           project_id=job.get("project_id"))
            with _LOCK:
                _TRACKS[str(run_id)] = track
    except Exception:  # noqa: BLE001 — an unreadable job leaves the build holding, as before
        log.warning("heavy slot tracking unavailable for %s", run_id, exc_info=True)
        track = None
    try:
        yield
    finally:
        if track is not None:
            with _LOCK:
                if _TRACKS.get(str(run_id)) is track:
                    del _TRACKS[str(run_id)]
            with track.cond:
                track.generation += 1
            _clear_row(track)


@contextlib.contextmanager
def light(run_id: Optional[str] = None) -> Iterator[None]:
    """A read-only model session of the run: the slot may go back while it thinks."""
    track = _track(run_id)
    if track is None:
        yield
        return
    mine: Optional[int] = None
    with track.cond:
        track.light += 1
        if track.heavy == 0 and not track.released and track.opener is None:
            track.generation += 1
            mine = track.opener = track.generation
            _schedule_release(track, mine)
    try:
        yield
    finally:
        with track.cond:
            track.light -= 1
            need = False
            if mine is not None and track.opener == mine:
                # The scope that gave the slot back ended: the build takes it again, whatever
                # else is still open (an abandoned seat never keeps it given away).
                track.opener = None
                track.generation += 1      # a pending release is void
                need = track.released
            elif track.light == 0:
                need = track.released
        if need:
            # Between stages the build holds, as it always did.
            _ensure_held(track)


@contextlib.contextmanager
def heavy(run_id: Optional[str] = None) -> Iterator[None]:
    """Work that loads the machine (a writing session, a sandbox command, a verify): it runs
    holding the slot, waiting for it first when the build had given it back."""
    track = _track(run_id)
    if track is None:
        yield
        return
    with track.cond:
        track.heavy += 1
        track.generation += 1              # no release while heavy work runs
    try:
        _ensure_held(track)
        yield
    finally:
        with track.cond:
            # Never a release here: a heavy step ending means the build goes on to its next
            # step, which holds (review PR #367: an abandoned seat's open light scope gave the
            # slot away between stages).
            track.heavy -= 1


def _schedule_release(track: _Track, generation: int) -> None:
    """Release after RELEASE_AFTER_SECONDS unless anything changed (generation moved on)."""

    def _fire() -> None:
        # Under the run's lock, so a heavy step starting now waits for the row to be written
        # and then takes the slot back through the queue, never past it.
        with track.cond:
            if (track.generation != generation or track.opener != generation
                    or track.released or track.acquiring or track.heavy > 0 or track.light == 0):
                return
            try:
                _write_release(track)
            except Exception:  # noqa: BLE001 — not released after all: the build keeps holding
                log.warning("heavy slot release failed for %s", track.run_id, exc_info=True)
                return
            track.released = True

    timer = threading.Timer(RELEASE_AFTER_SECONDS, _fire)
    timer.daemon = True
    timer.name = f"heavy-slot-release-{track.run_id[-12:]}"
    timer.start()


def _ensure_held(track: _Track) -> None:
    with track.cond:
        while track.acquiring:
            track.cond.wait(timeout=POLL_SECONDS)
        if not track.released:
            return
        track.acquiring = True
    try:
        _acquire(track)
        with track.cond:
            track.released = False
    finally:
        with track.cond:
            track.acquiring = False
            track.cond.notify_all()


# ------------------------------------------------------------------------------------------ #
# the database side
# ------------------------------------------------------------------------------------------ #

def _write_release(track: _Track) -> None:
    from .core import emit_event

    with track.engine.begin() as c:
        c.execute(text(
            "INSERT INTO heavy_slot_releases (job_id, fence_epoch, project_id, run_id, state, "
            "released_at, waiting_since, updated_at) VALUES (:j, :e, :p, :r, 'light', now(), "
            "NULL, now()) ON CONFLICT (job_id) DO UPDATE SET fence_epoch = :e, state = 'light', "
            "released_at = now(), waiting_since = NULL, updated_at = now()"),
            {"j": track.job_id, "e": track.epoch, "p": track.project_id, "r": track.run_id})
        if track.project_id:
            emit_event(c, track.project_id, "code_task.heavy_slot_released", "admission",
                       "code_task_run", track.run_id, {"job_id": track.job_id})


def _clear_row(track: _Track) -> None:
    try:
        with track.engine.begin() as c:
            c.execute(text("DELETE FROM heavy_slot_releases WHERE job_id = :j"),
                      {"j": track.job_id})
    except Exception:  # noqa: BLE001 — a stale row never counts once the lease moves on
        log.warning("heavy slot row not cleared for %s", track.run_id, exc_info=True)


def _check_stopped(c: Any, track: _Track) -> None:
    """A build that was stopped, finished elsewhere or lost its job stops waiting."""
    from . import owned_process

    if owned_process.was_cancelled(track.run_id) or owned_process.draining():
        raise owned_process.OwnerCancelled(
            "stopped while waiting for the heavy-build slot")
    status = c.execute(text("SELECT status FROM code_task_runs WHERE id = :r"),
                       {"r": track.run_id}).scalar()
    if status in ("failed", "completed", "superseded"):
        # It ended elsewhere: stand down quietly, never touching the status it has.
        raise HeavySlotLost(track.run_id, f"the run is already {status}")
    if status == "cancelled":
        raise owned_process.OwnerCancelled("the run was cancelled")
    live = c.execute(text(
        "SELECT 1 FROM jobs WHERE id = :j AND fence_epoch = :e AND status IN ('leased', "
        "'running') AND (lease_expires_at IS NULL OR lease_expires_at > now())"),
        {"j": track.job_id, "e": track.epoch}).first()
    if live is None:
        raise HeavySlotLost(track.run_id, "this build no longer owns its job")
    try:
        from .code_task_service import pending_stop

        with c.begin_nested():
            stop = pending_stop(c, track.run_id)
    except Exception:  # noqa: BLE001
        stop = None
    if stop:
        raise owned_process.OwnerCancelled(f"stopped by {stop.get('actor')}")


def _acquire(track: _Track) -> None:
    """Wait, in order, until this running build may hold the slot again."""
    from . import admission, stage_clock
    from .core import emit_event

    started = time.monotonic()
    announced = False
    touched = 0.0
    with stage_clock.waiting_for_a_slot(track.run_id):
        while True:
            with track.engine.begin() as c:
                _check_stopped(c, track)
                c.execute(text("SELECT pg_advisory_xact_lock(hashtext('boss.admission'))"))
                verdict = admission.reacquire_verdict(c, track.job_id)
                if verdict["admitted"]:
                    c.execute(text("DELETE FROM heavy_slot_releases WHERE job_id = :j"),
                              {"j": track.job_id})
                    waited = time.monotonic() - started
                    track.waited_total += waited
                    if track.project_id:
                        emit_event(c, track.project_id, "code_task.heavy_slot_acquired",
                                   "admission", "code_task_run", track.run_id,
                                   {"job_id": track.job_id,
                                    "waited_seconds": round(waited, 1)})
                    return
                c.execute(text(
                    "UPDATE heavy_slot_releases SET state = 'waiting', fence_epoch = :e, "
                    "waiting_since = COALESCE(waiting_since, now()), updated_at = now() "
                    "WHERE job_id = :j"), {"j": track.job_id, "e": track.epoch})
                if not announced and track.project_id:
                    emit_event(c, track.project_id, "code_task.heavy_slot_waiting", "admission",
                               "code_task_run", track.run_id,
                               {"job_id": track.job_id, "slots": verdict["slots"],
                                "holders": verdict["holders"],
                                "ahead": verdict["ahead"]})
                    announced = True
                if time.monotonic() - touched >= TOUCH_SECONDS:
                    # Alive, and saying so: a run waiting its turn is not a stalled one.
                    c.execute(text("UPDATE code_task_runs SET updated_at = now() "
                                   "WHERE id = :r"), {"r": track.run_id})
                    touched = time.monotonic()
            time.sleep(POLL_SECONDS)


# ------------------------------------------------------------------------------------------ #
# reads (admission, the console, the repair clock)
# ------------------------------------------------------------------------------------------ #

#: SQL predicate (alias `j` = jobs): this leased job has given its slot back for its current lease.
RELEASED_SQL = ("EXISTS (SELECT 1 FROM heavy_slot_releases h WHERE h.job_id = j.id "
                "AND h.fence_epoch = j.fence_epoch)")
#: …and wants it back.
WAITING_SQL = ("EXISTS (SELECT 1 FROM heavy_slot_releases h WHERE h.job_id = j.id "
               "AND h.fence_epoch = j.fence_epoch AND h.state = 'waiting')")


def waiting_row(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    """{job_id, waiting_since} when this run's live job is waiting for its slot back."""
    try:
        with connection.begin_nested():
            row = connection.execute(text(
                "SELECT h.job_id, h.waiting_since FROM heavy_slot_releases h JOIN jobs j "
                "ON j.id = h.job_id AND j.fence_epoch = h.fence_epoch "
                "WHERE h.run_id = :r AND h.state = 'waiting' AND j.status IN ('leased', 'running') "
                "LIMIT 1"), {"r": str(run_id)}).mappings().first()
    except Exception:  # noqa: BLE001 — an older schema: nobody waits mid-run
        return None
    return dict(row) if row else None


__all__ = ["HeavySlotLost", "LIGHT_STAGES", "POLL_SECONDS", "RELEASED_SQL", "RELEASE_AFTER_SECONDS",
           "TRACKED_KINDS", "WAITING_SQL", "heavy", "light", "read_only_tools", "stage_is_light",
           "stage_now",
           "tracking", "waiting_row"]
