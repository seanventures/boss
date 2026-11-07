"""WHY WORK IS WAITING, AND WHAT AN AGENT FOUND WHEN IT STOPPED MOVING. Nobody has to dig.

Two halves, one module, so every surface and the diagnosing agent read the SAME facts:

1. `wait_reason(...)` — THE "waiting for what" of a build. The Code Task header and its Now card,
   Project › Work rows and drawer, Agents at work: each said "Queued" / "Waiting" with nothing
   behind it. On the laptop (2026-11-07) four CVE builds said "Queued 0 of 7 · 1h 19m" for hours
   while every attempt crashed in five seconds. This reads the run, its job and the queue and
   says it plainly ("Waiting for a build slot: 2 in use by X and Y · next in about 4 min",
   "Waiting for you: give Forge a grant", "Stuck: every attempt crashed (30×) …"). Never a bare
   word.

2. `sweep(...)` / `diagnose(...)` — ALWAYS-ON DIAGNOSIS. Work with no progress for
   STUCK_MINUTES, work that failed or was blocked, and work paused on a person gets an
   investigation without anyone asking: BOSS gathers the evidence (run, job, its last crash,
   events, the queue), triages it with its own rules, and the Shepherd agent takes ONE turn
   through the one agent pipeline (AgentService.decide, model-agnostic; BOSSAPP-759) to choose
   the next move and say why. A safe move within autonomy (restart a dead job, put a lost one
   back in the queue, resume under a grant that now covers it) is taken at once; only when no
   safe move exists is ONE plain card raised saying exactly what a person must do. The result
   lives on the run (`finalize_ctx.diagnosis`) as five short steps — stuck → checked → cause →
   tried → next — which every surface draws as a small diagram (visibility rule).
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger("boss.stuck_work")

#: A build that has not left the queue, or not moved, for this long is investigated.
STUCK_MINUTES = 15
#: Attempts that crashed before reporting back, past which a build is crash-looping.
CRASH_LOOP_ATTEMPTS = 3
#: Model turns one sweep may spend (each is a governed, receipted call).
MAX_TURNS_PER_SWEEP = 3
#: A diagnosis is redone when its facts change, or after this long while still stuck.
REDIAGNOSE_MINUTES = 60
#: Fixes the Shepherd takes on ONE build before it stops and hands it to a person, whatever the
#: cause: an agent that keeps "fixing" the same stuck build for ever is worse than none.
MAX_FIXES_PER_ITEM = 3
#: Why BOSS does not fix it itself, per cause: the one line a person reads under "what to do".
WHY_NOT: dict[str, str] = {
    "no_grant": "Only a person can let an agent act for them.",
    "crash_loop": "It is BOSS's own bug: another attempt runs the same code.",
    "budget": "Raising a budget is a person's call.",
    "no_worker": "BOSS cannot start its own worker from inside it (infrastructure).",
    "failed": "The failure is in the work itself; trying again needs a person's go-ahead.",
    "quiet": "BOSS found nothing it can safely change.",
    "capped": "BOSS already tried its fixes and the work still does not move.",
}
#: Who investigates. The Shepherd: BOSS's agent for work that stopped moving.
AGENT = "Shepherd"
ACTOR = "agent:shepherd"

LIVE_STATUSES = ("running", "queued", "repairing")
ENDED_STATUSES = ("failed", "blocked")
WAITING_ON_PERSON = "waiting_on_person"

# ───────────────────────────── small helpers ─────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _mins(delta_seconds: float) -> int:
    return max(0, int(delta_seconds // 60))


def span_words(minutes: int) -> str:
    """12 -> "12 min", 135 -> "2h 15m"."""
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _clip(value: Any, n: int = 160) -> str:
    from .redaction import redact_secrets

    s = " ".join(str(value or "").split())
    s = redact_secrets(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _names(titles: list[str]) -> str:
    titles = [t for t in titles if t]
    if not titles:
        return ""
    if len(titles) == 1:
        return titles[0]
    if len(titles) == 2:
        return f"{titles[0]} and {titles[1]}"
    return f"{titles[0]}, {titles[1]} and {len(titles) - 2} more"


def _short_title(title: Any) -> str:
    first = (str(title or "").splitlines() or [""])[0].strip()
    return first if len(first) <= 40 else first[:39].rstrip() + "…"


# ───────────────────────────── crash capture ─────────────────────────────


def where_it_broke(exc: BaseException) -> str:
    """The deepest BOSS frame of a traceback: `person_wait.py:125 _load_run`."""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    own = [f for f in frames if "boss_engine" in f.filename or "/app/" in f.filename
           or "apps/" in f.filename]
    f = (own or frames or [None])[-1]
    if f is None:
        return ""
    return f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}"


def crash_words(exc: BaseException) -> str:
    return _clip(f"{type(exc).__name__}: {exc}", 240)


def record_step_crash(engine: Any, run_id: str, exc: BaseException) -> str:
    """Write what crashed onto the run (`finalize_ctx.last_crash`, counted) and an event, in its
    own transaction; return the words the step answers with. Never raises."""
    said = crash_words(exc)
    where = where_it_broke(exc)
    try:
        from .core import emit_event

        with engine.begin() as c:
            row = c.execute(text("SELECT project_id, finalize_ctx FROM code_task_runs "
                                 "WHERE id = :id"), {"id": run_id}).mappings().first()
            if row is None:
                return said
            prev = _as_dict(_as_dict(row["finalize_ctx"]).get("last_crash"))
            crash = {"type": type(exc).__name__, "message": said, "where": where,
                     "at": _now().isoformat(), "count": int(prev.get("count") or 0) + 1,
                     "first_at": prev.get("first_at") or _now().isoformat()}
            c.execute(text(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
                "jsonb_build_object('last_crash', CAST(:c AS jsonb)) WHERE id = :id"),
                {"c": json.dumps(crash), "id": run_id})
            emit_event(c, row["project_id"], "code_task.step_crashed", "boss", "code_task_run",
                       run_id, crash)
    except Exception:  # noqa: BLE001 — recording a crash must never become a second crash
        log.warning("step crash not recorded run_id=%s", run_id, exc_info=True)
    return f"{said}" + (f" (at {where})" if where else "")


# ───────────────────────────── the facts ─────────────────────────────


def _job(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, status, attempt, max_attempts, lease_owner, lease_expires_at, heartbeat_at, "
        "run_after, last_error, priority, created_at, updated_at, on_behalf_of, grant_id "
        "FROM jobs WHERE kind = 'code.task' AND payload->>'run_id' = :r "
        "ORDER BY created_at DESC LIMIT 1"), {"r": run_id}).mappings().first()
    return dict(row) if row else None


def _running_builds(connection: Any, project_id: Optional[str], exclude: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT r.id, r.title, r.project_id, j.created_at AS job_at, j.heartbeat_at "
        "FROM jobs j JOIN code_task_runs r ON r.id = j.payload->>'run_id' "
        "WHERE j.kind = 'code.task' AND j.status IN ('leased', 'running') "
        "AND j.lease_expires_at > now() AND r.id <> :x ORDER BY j.created_at LIMIT 20"),
        {"x": exclude}).mappings().all()
    return [dict(r) for r in rows]


def _queue_position(connection: Any, job: Mapping[str, Any]) -> tuple[int, int]:
    """(this job's place among queued builds, how many are queued)."""
    rows = connection.execute(text(
        "SELECT id FROM jobs WHERE kind = 'code.task' AND status IN ('queued', 'failed_retryable') "
        "ORDER BY priority ASC, created_at ASC LIMIT 200")).scalars().all()
    ids = list(rows)
    place = ids.index(job["id"]) + 1 if job["id"] in ids else 0
    return place, len(ids)


def _typical_minutes(connection: Any, project_id: Optional[str]) -> Optional[int]:
    """Median minutes a finished build took in this project (last 10), else None."""
    vals = connection.execute(text(
        "SELECT EXTRACT(EPOCH FROM (updated_at - created_at)) / 60 FROM code_task_runs "
        "WHERE project_id = :p AND status = 'completed' ORDER BY updated_at DESC LIMIT 10"),
        {"p": project_id}).scalars().all()
    vals = sorted(float(v) for v in vals if v is not None and float(v) > 0)
    if not vals:
        return None
    return max(1, int(round(vals[len(vals) // 2])))


def _attempts_lost(run: Mapping[str, Any], job: Optional[Mapping[str, Any]]) -> int:
    ctx = _as_dict(run.get("finalize_ctx"))
    redelivered = ctx.get("redelivered_attempts") or []
    lost = len(redelivered) if isinstance(redelivered, list) else 0
    if job is not None:
        lost = max(lost, int(job.get("attempt") or 0) - int(job.get("max_attempts") or 3))
    return lost


def _decision(connection: Any, decision_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not decision_id:
        return None
    row = connection.execute(text(
        "SELECT id, status, question, recommendation FROM decisions WHERE id = :d"),
        {"d": decision_id}).mappings().first()
    return dict(row) if row else None


# ───────────────────────────── 1. waiting for what ─────────────────────────────


def wait_reason(connection: Any, run: Mapping[str, Any], *, job: Any = "load",
                now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Why this build is not moving right now, in words, from real state. None while it is
    genuinely working (an agent is on it) or has ended well.

    Returns {code, who, words, detail, since, eta_minutes}:
      who = "you" (a person's move), "boss" (BOSS is on it / will retry), "queue" (its turn
      comes), "stuck" (nothing will move it without a fix).
    """
    now = now or _now()
    status = str(run.get("status") or "")
    phase = str(run.get("phase") or "")
    run_id = str(run.get("id") or run.get("run_id") or "")
    ctx = _as_dict(run.get("finalize_ctx"))
    if status in ("completed", "cancelled", "simulated"):
        return None
    if job == "load":
        job = _job(connection, run_id) if run_id else None

    def out(code: str, who: str, words: str, detail: str = "", since: Any = None,
            eta: Optional[int] = None) -> dict[str, Any]:
        s = _aware(since)
        return {"code": code, "who": who, "words": words, "detail": detail,
                "since": s.isoformat() if s else None, "eta_minutes": eta}

    # A person's move first: it is never "stuck".
    if status == WAITING_ON_PERSON:
        pw = _as_dict(ctx.get("person_wait"))
        builder = pw.get("builder") or "the builder"
        return out("person_grant", "you",
                   f"Waiting for you: let {builder} build for you (a standing grant)",
                   _clip(pw.get("ask") or "", 300), pw.get("opened_at"))
    from .build_waits import why_waiting

    dec = _decision(connection, run.get("decision_id"))
    asked = why_waiting(phase, (dec or {}).get("status"))
    if asked and status not in ("failed",):
        return out("person", "you", f"Waiting for you: {asked}",
                   _clip((dec or {}).get("question") or "", 300), run.get("updated_at"))

    if status in ENDED_STATUSES:
        diag = _as_dict(ctx.get("diagnosis"))
        # The Shepherd's words only when they are about THIS ending (not an earlier one).
        fresh = _aware(diag.get("at")) and _aware(run.get("updated_at")) and \
            _aware(diag.get("at")) >= _aware(run.get("updated_at")) - timedelta(minutes=5)
        words = ("Blocked: " if status == "blocked" else "Failed: ") + _clip(
            (diag.get("cause") if fresh else None) or run.get("error") or diag.get("cause")
            or "no reason was recorded", 140)
        return out(status, "you" if diag.get("needs_person") or status == "blocked" else "boss",
                   words, _clip(run.get("error") or "", 300), run.get("updated_at"))

    if status not in LIVE_STATUSES:
        return None

    # The job's own verdict outranks every timing guess.
    crash = _as_dict(ctx.get("last_crash"))
    lost = _attempts_lost(run, job)
    if job is None:
        if phase == "queued" or status == "queued":
            return out("no_job", "stuck", "Stuck: nothing is carrying it (no job in the queue)",
                       "BOSS will restart it.", run.get("created_at"))
        return None
    jstatus = str(job.get("status") or "")
    if jstatus in ("deadlettered", "failed_final", "cancelled"):
        err = _as_dict(job.get("last_error")).get("message") or ""
        return out("job_ended", "stuck", "Stuck: its job ended and the build never noticed",
                   _clip(err, 300), job.get("updated_at"))
    if lost >= CRASH_LOOP_ATTEMPTS or (crash and int(crash.get("count") or 0) >= CRASH_LOOP_ATTEMPTS):
        n = max(lost, int(crash.get("count") or 0))
        what = crash.get("message") or "each attempt stopped before it could report back"
        return out("crash_loop", "stuck", f"Stuck: every attempt crashed ({n}×)",
                   _clip(what, 300), crash.get("first_at") or run.get("created_at"))
    lease = _aware(job.get("lease_expires_at"))
    if jstatus in ("leased", "running") and lease is not None and lease < now:
        silent = _mins((now - lease).total_seconds())
        return out("lost", "boss", f"Its worker stopped answering {span_words(silent)} ago",
                   "BOSS puts it back in the queue; it restarts the step it was on.", lease)
    if jstatus in ("leased", "running"):
        if phase in ("queued", "provisioning", ""):
            return out("starting", "boss", "Starting: a worker has it", "", job.get("heartbeat_at"))
        return None  # an agent is on it: working, not waiting

    # Queued: what it waits for, from what the claim path wrote.
    err = _as_dict(job.get("last_error"))
    run_after = _aware(job.get("run_after"))
    eta_retry = _mins((run_after - now).total_seconds()) + 1 if run_after and run_after > now else None
    code = str(err.get("code") or err.get("kind") or "")
    if jstatus == "failed_retryable":
        return out("retrying", "boss",
                   f"Retrying{' in about ' + span_words(eta_retry) if eta_retry else ' now'} "
                   f"after: {_clip(err.get('message') or 'an error', 90)}",
                   _clip(err.get("message") or "", 300), job.get("updated_at"), eta_retry)
    if code == "budget_hold" or ctx.get("budget_wait_since"):
        return out("budget", "you", "Waiting for budget: the project's cap is reached",
                   _clip(err.get("message") or "Raise the cap or wait for spend to free up.", 300),
                   ctx.get("budget_wait_since") or job.get("updated_at"))
    running = _running_builds(connection, run.get("project_id"), run_id)
    names = _names([_short_title(r["title"]) for r in running[:2]])
    typical = _typical_minutes(connection, run.get("project_id"))
    eta_slot: Optional[int] = None
    if running and typical:
        started = [_aware(r["job_at"]) for r in running if r.get("job_at")]
        if started:
            oldest = min(s for s in started if s is not None)
            eta_slot = max(1, typical - _mins((now - oldest).total_seconds()))
    in_use = (f"{len(running)} in use by {names}" if running else "")
    if code == "host_capacity":
        return out("host", "queue",
                   "Waiting for room on this machine" + (f": {in_use}" if in_use else ""),
                   _clip(err.get("message") or "", 300), job.get("updated_at"), eta_slot)
    if code == "yielded_to_operator":
        return out("yielded", "queue", "Waiting: a person's build goes first",
                   _clip(err.get("waiting_for") or err.get("message") or "", 300),
                   job.get("updated_at"), eta_slot)
    if code and ("quota" in code or "allocation" in code or "tracker" in code
                 or err.get("message") == "Job is waiting for tracked quota allocation."):
        return out("quota", "queue",
                   "Waiting for a build slot" + (f": {in_use}" if in_use else "")
                   + (f" · next in about {span_words(eta_slot)}" if eta_slot else ""),
                   f"Held by the project's work allocation ({code.replace('_', ' ')}).",
                   job.get("updated_at"), eta_slot)
    place, total = _queue_position(connection, job)
    waited = _mins((now - (_aware(job.get("created_at")) or now)).total_seconds())
    if running:
        words = (f"Waiting for a build slot: {in_use}"
                 + (f" · next in about {span_words(eta_slot)}" if eta_slot else ""))
        detail = f"Place {place} of {total} in the queue." if place else ""
        return out("slot", "queue", words, detail, job.get("created_at"), eta_slot)
    if waited >= STUCK_MINUTES:
        return out("no_worker", "stuck",
                   f"Stuck: queued {span_words(waited)} and no worker has taken it",
                   "Nothing else is running, so a worker should have picked it up.",
                   job.get("created_at"))
    return out("next", "queue", "Next in line for a worker"
               + (f" (place {place} of {total})" if total > 1 and place else ""),
               "", job.get("created_at"))


def wait_reasons(connection: Any, runs: list[Mapping[str, Any]]) -> dict[str, Optional[dict]]:
    """`wait_reason` for several runs (Project › Work, Agents at work). Never raises."""
    out: dict[str, Optional[dict]] = {}
    for r in runs:
        rid = str(r.get("id") or r.get("run_id") or "")
        if not rid:
            continue
        try:
            with connection.begin_nested():
                full = r if "finalize_ctx" in r else _load(connection, rid) or r
                out[rid] = wait_reason(connection, full)
        except Exception:  # noqa: BLE001 — a row with no reason still renders
            log.warning("wait_reason failed run_id=%s", rid, exc_info=True)
            out[rid] = None
    return out


def _load(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text("SELECT * FROM code_task_runs WHERE id = :id"),
                             {"id": run_id}).mappings().first()
    return dict(row) if row else None


# ───────────────────────────── 2. the investigation ─────────────────────────────

#: The moves the Shepherd may choose. `acts` = BOSS does it itself, now, within autonomy.
MOVES: dict[str, dict[str, Any]] = {
    "restart_job": {"acts": True, "label": "Restart its job",
                    "means": "Put a fresh job in the queue for this build: its old one ended or "
                             "was lost, so nothing is carrying it. The build resumes from the "
                             "stage it reached."},
    "requeue_lost": {"acts": True, "label": "Hand it back to the queue",
                     "means": "Its worker stopped answering: release the lease so another worker "
                              "slot takes it now."},
    "resume_under_grant": {"acts": True, "label": "Resume under the grant",
                           "means": "A person's standing grant now covers this build: resume it "
                                    "under that grant."},
    "stop_crash_loop": {"acts": True, "label": "Stop retrying, report it",
                        "means": "Every attempt crashed the same way before it could report: "
                                 "stop re-running it (another attempt changes nothing), mark it "
                                 "failed with the crash, and tell a person what to do."},
    "retry_on_this_version": {"acts": True, "label": "Try once on this version",
                              "means": "Every attempt was lost while an older BOSS ran, so none "
                                       "of them says anything about this version: run it once "
                                       "more here. If it fails again, the reason is recorded."},
    "wait": {"acts": False, "label": "Let it wait",
             "means": "It is waiting for something that frees up by itself (a slot, a retry "
                      "timer, budget headroom): nothing to do but say so."},
    "ask_person": {"acts": False, "label": "Ask a person",
                   "means": "Only a person can unblock it: raise ONE plain card that says what "
                            "they must do and why."},
}


def evidence(connection: Any, run_id: str, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Everything the Shepherd reads about one build: the run, its job, its last crash, the
    queue around it, its recent events and agent turns. Small, redacted, no secrets."""
    now = now or _now()
    run = _load(connection, run_id) or {}
    if not run:
        return {}
    ctx = _as_dict(run.get("finalize_ctx"))
    job = _job(connection, run_id)
    events = connection.execute(text(
        "SELECT event_type, created_at, data FROM events WHERE entity_id = :r "
        "ORDER BY created_at DESC LIMIT 12"), {"r": run_id}).mappings().all()
    turns = connection.execute(text(
        "SELECT max(created_at) AS last, count(*) AS n FROM agent_turns "
        "WHERE code_task_run_id = :r"), {"r": run_id}).mappings().first() or {}
    deadletter = None
    if job and job.get("status") == "deadlettered":
        deadletter = connection.execute(text(
            "SELECT reason, created_at FROM deadletters WHERE source_kind = 'job' AND source_id = :j "
            "ORDER BY created_at DESC LIMIT 1"), {"j": job["id"]}).mappings().first()
    created = _aware(run.get("created_at")) or now
    last_turn = _aware(turns.get("last"))
    progress_at = max([d for d in (created, last_turn) if d is not None])
    reason = wait_reason(connection, run, job=job, now=now)
    return {
        "run": {"id": run_id, "project_id": run.get("project_id"), "title": _short_title(run.get("title")),
                "status": run.get("status"), "phase": run.get("phase"),
                "error": _clip(run.get("error"), 400),
                "age_minutes": _mins((now - created).total_seconds()),
                "minutes_without_progress": _mins((now - progress_at).total_seconds()),
                "agent_turns": int(turns.get("n") or 0)},
        "job": None if job is None else {
            "id": job["id"], "status": job.get("status"), "attempt": job.get("attempt"),
            "max_attempts": job.get("max_attempts"), "holder": job.get("lease_owner"),
            "lease_expired_minutes_ago": (_mins((now - _aware(job["lease_expires_at"])).total_seconds())
                                          if _aware(job.get("lease_expires_at"))
                                          and _aware(job["lease_expires_at"]) < now else None),
            "last_error": _clip(_as_dict(job.get("last_error")).get("message"), 300),
            "carries_grant": bool(job.get("grant_id")),
            "lost_on_older_boss": bool(_as_dict(job.get("last_error")).get("lost_on_older_boss"))
            or _older_boss(job, ctx)},
        "retried_after_upgrade": bool(ctx.get("retried_after_upgrade")),
        "attempts_lost": _attempts_lost(run, job),
        "last_crash": _as_dict(ctx.get("last_crash")) or None,
        "person_wait": {k: v for k, v in _as_dict(ctx.get("person_wait")).items()
                        if k in ("builder", "ask", "blocker_type", "decision_id", "opened_at")}
        or None,
        "deadletter": _clip((deadletter or {}).get("reason"), 300) if deadletter else None,
        "wait_reason": reason,
        "recent_events": [{"type": e["event_type"],
                           "at": (_aware(e["created_at"]) or now).isoformat()} for e in events],
        "previous_diagnosis": {k: _as_dict(ctx.get("diagnosis")).get(k)
                               for k in ("signature", "chosen", "at")}
        if ctx.get("diagnosis") else None,
    }


def _older_boss(job: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    """Every lost attempt of this job happened while an OLDER BOSS ran: its last sign of life and
    its last recorded crash both predate this process. Then those attempts say nothing about the
    code running now, and one try on this version is the honest next step."""
    from .jobs import PROCESS_STARTED_AT

    seen = _aware(job.get("heartbeat_at")) or _aware(job.get("lease_expires_at"))
    crash_at = _aware(_as_dict(ctx.get("last_crash")).get("at"))
    return bool(seen and seen <= PROCESS_STARTED_AT and (crash_at is None or crash_at <= PROCESS_STARTED_AT))


def triage(ev: Mapping[str, Any]) -> dict[str, Any]:
    """BOSS's own reading of the facts: the likely cause (with its evidence), the moves that are
    permissible, and the move its rules would take. A HINT for the Shepherd, never the decider
    when a model answers; the answer when none does. Pure."""
    run = ev.get("run") or {}
    job = ev.get("job") or {}
    crash = ev.get("last_crash") or {}
    reason = ev.get("wait_reason") or {}
    code = str(reason.get("code") or "")
    pw = ev.get("person_wait") or {}

    def t(cause_code: str, cause: str, proof: str, moves: list[str], pick: str,
          nxt: str, who: str) -> dict[str, Any]:
        return {"cause_code": cause_code, "cause": cause, "proof": proof, "moves": moves,
                "pick": pick, "next": nxt, "who": who, "why_not": WHY_NOT.get(cause_code, "")}

    if code == "person_grant" or pw.get("blocker_type") == "authority_no_grant":
        return t("no_grant", f"No person's grant lets {pw.get('builder') or 'its builder'} act",
                 "Started by an always-on agent, not a person; the build refused to run without "
                 "a grant (identity: no_grant).",
                 ["resume_under_grant", "ask_person"], "ask_person",
                 f"Give {pw.get('builder') or 'the builder'} a standing grant (My account ▸ "
                 "Agents acting for me); BOSS resumes it by itself.", "you")
    if (code == "crash_loop" and job.get("lost_on_older_boss")
            and not ev.get("retried_after_upgrade")):
        return t("lost_on_older_boss", "Every attempt crashed on the previous BOSS version",
                 f"{ev.get('attempts_lost')} attempts of job {job.get('id')}, none reported back, "
                 "all before this BOSS started",
                 ["retry_on_this_version", "ask_person"], "retry_on_this_version",
                 "BOSS tries it once on this version; if it stops again, the reason is shown "
                 "here.", "boss")
    if code == "crash_loop" or (crash and int(crash.get("count") or 0) >= CRASH_LOOP_ATTEMPTS):
        where = f" at {crash.get('where')}" if crash.get("where") else ""
        return t("crash_loop", f"BOSS crashes running this build{where}",
                 f"{ev.get('attempts_lost') or crash.get('count')} attempts, each ended in seconds: "
                 f"{crash.get('message') or job.get('last_error') or 'no report reached the queue'}",
                 ["stop_crash_loop", "ask_person"], "stop_crash_loop",
                 "It needs a BOSS fix: send the crash (Details) to your BOSS admin, then press "
                 "Retry.", "you")
    if code == "job_ended":
        return t("job_ended", "Its job ended and the build was never told",
                 f"job {job.get('id')} is {job.get('status')}: {ev.get('deadletter') or job.get('last_error') or ''}",
                 ["restart_job", "ask_person"], "restart_job",
                 "BOSS restarted its job; it resumes from the stage it reached.", "boss")
    if code == "no_job":
        return t("no_job", "Nothing is carrying this build", "No code.task job exists for it.",
                 ["restart_job", "ask_person"], "restart_job",
                 "BOSS restarted its job.", "boss")
    if code == "lost":
        return t("lost_lease", "Its worker stopped answering",
                 f"lease held by {job.get('holder')} expired {job.get('lease_expired_minutes_ago')} min ago",
                 ["requeue_lost", "ask_person"], "requeue_lost",
                 "BOSS handed it back to the queue; another worker slot takes it.", "boss")
    if code in ("slot", "quota", "host", "yielded", "next", "retrying", "starting"):
        return t(code, reason.get("words") or "It is waiting its turn", reason.get("detail") or "",
                 ["wait"], "wait", reason.get("words") or "It runs when its turn comes.", "boss")
    if code == "budget":
        return t("budget", "The project's budget cap is reached", reason.get("detail") or "",
                 ["wait", "ask_person"], "ask_person",
                 "Raise the project's budget cap, or wait for spend to free up.", "you")
    if code == "no_worker":
        return t("no_worker", "No worker is taking work from the queue",
                 reason.get("words") or "", ["ask_person"], "ask_person",
                 "Restart BOSS's worker (boss restart worker); queued builds then start.", "you")
    if (run.get("status") == "failed" and job.get("lost_on_older_boss")
            and not ev.get("retried_after_upgrade")):
        return t("lost_on_older_boss", "Every attempt was lost on the previous BOSS version",
                 f"job {job.get('id')}: {job.get('attempt')} attempts, none reported back, all "
                 "before this BOSS started",
                 ["retry_on_this_version", "ask_person"], "retry_on_this_version",
                 "BOSS tries it once on this version; if it stops again, the reason is shown "
                 "here.", "boss")
    if run.get("status") in ENDED_STATUSES:
        return t("failed", "It failed: " + _clip(run.get("error") or "no reason recorded", 120),
                 _clip(run.get("error") or "", 300), ["ask_person"], "ask_person",
                 "Read the failure and press Retry, or change the request.", "you")
    return t("quiet", "It has not moved and BOSS found no recorded cause",
             f"{run.get('minutes_without_progress')} min without an agent turn",
             ["requeue_lost", "ask_person"], "ask_person",
             "Restart the step, or cancel the build.", "you")


def signature(ev: Mapping[str, Any], tri: Mapping[str, Any]) -> str:
    run = ev.get("run") or {}
    return f"{tri.get('cause_code')}|{run.get('status')}|{run.get('phase')}"


def _steps(ev: Mapping[str, Any], tri: Mapping[str, Any], *, chosen: str, tried: str,
           nxt: str, who: str) -> list[dict[str, Any]]:
    run = ev.get("run") or {}
    job = ev.get("job") or {}
    stuck_for = int(run.get("minutes_without_progress") or 0)
    if run.get("status") in ENDED_STATUSES:
        stuck = {"k": "stuck", "label": "Failed" if run.get("status") == "failed" else "Blocked",
                 "detail": _clip(run.get("error") or "", 200)}
    elif run.get("status") == WAITING_ON_PERSON:
        stuck = {"k": "stuck", "label": "Paused", "detail": "Waiting on a person"}
    else:
        stuck = {"k": "stuck", "label": f"No progress {span_words(stuck_for)}",
                 "detail": (ev.get("wait_reason") or {}).get("words") or ""}
    checked_bits = []
    if job:
        checked_bits.append(f"job {job.get('status')} (attempt {job.get('attempt')} of "
                            f"{job.get('max_attempts')})")
    if ev.get("last_crash"):
        checked_bits.append(f"{(ev['last_crash'] or {}).get('count')} crashes")
    checked_bits.append(f"{len(ev.get('recent_events') or [])} events")
    checked_bits.append(f"{run.get('agent_turns') or 0} agent turns")
    return [
        stuck,
        {"k": "checked", "label": "Job, events, log", "detail": "; ".join(checked_bits)},
        {"k": "cause", "label": _clip(tri.get("cause"), 60), "detail": _clip(tri.get("proof"), 300)},
        {"k": "tried", "label": MOVES.get(chosen, {}).get("label", chosen), "detail": _clip(tried, 300)},
        {"k": "next", "label": "You" if who == "you" else "BOSS", "detail": _clip(nxt, 300),
         "who": who},
    ]


SITUATION = ("{agent} is BOSS's agent for work that stopped moving. It reads a build's run, job, "
             "crash record, queue and events, names the likely cause with its evidence, and picks "
             "the ONE next move. BOSS carries out a move that acts, inside its own governance.")
REQUEST = ("Build {run_id} (\"{title}\") is {state}. Read the evidence, name the likely cause, and "
           "choose the one next move. Prefer a move BOSS can take itself; choose ask_person only "
           "when nothing BOSS does would move it.")
BRIEF = ("Cite only facts in the context. In `reason` write two short sentences for the operator: "
         "what happened and why (the cause, with its evidence). Never invent a fix BOSS cannot "
         "take; never choose a move the context does not offer.")


def _agent_turn(connection: Any, settings: Any, ev: Mapping[str, Any], tri: Mapping[str, Any],
                adapter: Any = None) -> dict[str, Any]:
    """The Shepherd's ONE turn through the one agent pipeline. Never raises."""
    try:
        from .agent_service import AgentService, Choice

        run = ev.get("run") or {}
        choices = [Choice(k, MOVES[k]["means"], acts=MOVES[k]["acts"], label=MOVES[k]["label"])
                   for k in tri.get("moves") or [] if k in MOVES]
        got = AgentService(settings).decide(
            connection, project_id=str(run.get("project_id") or ""), actor=ACTOR, role="sre",
            agent=AGENT, external_ref=f"code_task_run:{run.get('id')}",
            ref=f"stuck:{run.get('id')}", situation=SITUATION.format(agent=AGENT),
            message=REQUEST.format(run_id=run.get("id"), title=run.get("title") or "",
                                   state=(ev.get("wait_reason") or {}).get("words")
                                   or run.get("status")),
            choices=choices, context=dict(ev),
            hints=[f"BOSS's rules read: cause={tri.get('cause')}; proof={tri.get('proof')}; "
                   f"they would choose {tri.get('pick')}"],
            brief=BRIEF, adapter=adapter, min_confidence=0.0, context_limit=9000)
        return {"decided": bool(got.decided), "action": got.action, "reason": got.reason,
                "status": got.status, "by": dict(got.by or {}),
                "cost_usd_micros": int(got.cost_usd_micros or 0), "error": got.error}
    except Exception as exc:  # noqa: BLE001 — the coded diagnosis still lands
        log.warning("shepherd turn failed", exc_info=True)
        return {"decided": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _act(connection: Any, settings: Any, run_id: str, move: str,
         ev: Mapping[str, Any]) -> tuple[bool, str]:
    """Carry out a move that acts. (done, what was done, in words). Bounded and idempotent."""
    from .core import emit_event
    from .jobs import JOB_PRIORITY_INTERACTIVE, JobLeaseService, enqueue_job

    run = _load(connection, run_id) or {}
    project = run.get("project_id")
    job = ev.get("job") or {}
    if move == "restart_job":
        if run.get("status") not in LIVE_STATUSES:
            return False, "It is no longer running, so no job was restarted."
        live = connection.execute(text(
            "SELECT 1 FROM jobs WHERE kind = 'code.task' AND payload->>'run_id' = :r "
            "AND status IN ('queued', 'leased', 'running', 'failed_retryable')"), {"r": run_id}).first()
        if live:
            return False, "A live job already carries it."
        enqueue_job(connection, project, "code.task", {"run_id": run_id},
                    task_id=run.get("task_id"), priority=JOB_PRIORITY_INTERACTIVE)
        emit_event(connection, project, "code_task.job_restarted", ACTOR, "code_task_run", run_id,
                   {"reason": "no live job carried it"})
        return True, "Restarted its job; it resumes from the stage it reached."
    if move == "retry_on_this_version" and run.get("status") in LIVE_STATUSES:
        if _as_dict(run.get("finalize_ctx")).get("retried_after_upgrade"):
            return False, "Already tried once on this version."
        # The old job's attempt count belongs to the old version: end it and start a fresh one.
        connection.execute(text(
            "UPDATE jobs SET status = 'cancelled', lease_owner = NULL, lease_token_hash = NULL, "
            "lease_expires_at = NULL, updated_at = now(), last_error = CAST(:e AS jsonb) "
            "WHERE kind = 'code.task' AND payload->>'run_id' = :r AND status IN "
            "('queued', 'leased', 'running', 'failed_retryable')"),
            {"r": run_id, "e": json.dumps({"message": "replaced: every attempt was lost on an older BOSS"})})
        connection.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = (COALESCE(finalize_ctx, '{}'::jsonb) - "
            "'redelivered_attempts' - 'last_crash') || jsonb_build_object('retried_after_upgrade', "
            "CAST(:at AS text), 'lost_before_upgrade', COALESCE(finalize_ctx->'redelivered_attempts', "
            "'[]'::jsonb)) WHERE id = :id"),
            {"at": _now().isoformat(), "id": run_id})
        enqueue_job(connection, project, "code.task", {"run_id": run_id},
                    task_id=run.get("task_id"), priority=JOB_PRIORITY_INTERACTIVE)
        emit_event(connection, project, "code_task.retried_after_upgrade", ACTOR, "code_task_run",
                   run_id, {"reason": "every attempt was lost on an older BOSS"})
        return True, "Started it fresh on this BOSS version (the old attempts ran older code)."
    if move == "retry_on_this_version":
        if run.get("status") != "failed" or _as_dict(run.get("finalize_ctx")).get("retried_after_upgrade"):
            return False, "Already tried once on this version."
        connection.execute(text(
            "UPDATE code_task_runs SET status = 'running', phase = 'queued', error = NULL, "
            "updated_at = now(), finalize_ctx = (COALESCE(finalize_ctx, '{}'::jsonb) - "
            "'redelivered_attempts' - 'last_crash') || jsonb_build_object('retried_after_upgrade', "
            "CAST(:at AS text), 'lost_before_upgrade', COALESCE(finalize_ctx->'redelivered_attempts', "
            "'[]'::jsonb)) WHERE id = :id AND status = 'failed'"), {"at": _now().isoformat(), "id": run_id})
        enqueue_job(connection, project, "code.task", {"run_id": run_id},
                    task_id=run.get("task_id"), priority=JOB_PRIORITY_INTERACTIVE)
        emit_event(connection, project, "code_task.retried_after_upgrade", ACTOR, "code_task_run",
                   run_id, {"reason": "every attempt was lost on an older BOSS"})
        return True, "Started it once more on this BOSS version."
    if move == "requeue_lost":
        moved = connection.execute(text(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second', updated_at = now() "
            "WHERE id = :j AND status IN ('leased', 'running') AND lease_expires_at < now()"),
            {"j": job.get("id")}).rowcount if job.get("id") else 0
        return bool(moved), ("Handed it back to the queue; the next free worker slot takes it."
                             if moved else "Its job was already taken back.")
    if move == "resume_under_grant":
        from .person_wait import verify_and_resume

        got = verify_and_resume(connection, settings, None, run_id=run_id)
        return bool(got.get("resumed")), ("Resumed it under the grant that now covers it."
                                          if got.get("resumed") else "No grant covers it yet.")
    if move == "stop_crash_loop":
        if job.get("id") and job.get("status") in ("leased", "running", "queued", "failed_retryable"):
            connection.execute(text(
                "UPDATE jobs SET lease_expires_at = LEAST(COALESCE(lease_expires_at, now()), now()), "
                "attempt = GREATEST(attempt, max_attempts) WHERE id = :j"), {"j": job["id"]})
            done = JobLeaseService(settings).retire_lost_lease(
                connection, job["id"], actor=ACTOR,
                reason="BOSS crashed on every attempt and stopped retrying"
                + (f" (at {(ev.get('last_crash') or {}).get('where')})"
                   if (ev.get("last_crash") or {}).get("where") else "") + ": " + _clip(
                    (ev.get("last_crash") or {}).get("message") or "no crash text reached BOSS", 300))
            if done is None and job.get("status") in ("queued", "failed_retryable"):
                connection.execute(text("UPDATE jobs SET status = 'cancelled', updated_at = now() "
                                        "WHERE id = :j AND status IN ('queued', 'failed_retryable')"),
                                   {"j": job["id"]})
        crash = ev.get("last_crash") or {}
        msg = ("BOSS crashed on every attempt and stopped retrying: "
               + _clip(crash.get("message") or job.get("last_error") or "the attempts never reported", 300))
        connection.execute(text(
            "UPDATE code_task_runs SET status = 'failed', error = :e, updated_at = now() "
            "WHERE id = :id AND status IN ('running', 'queued', 'repairing')"), {"e": msg, "id": run_id})
        emit_event(connection, project, "code_task.failed", ACTOR, "code_task_run", run_id,
                   {"reason": "crash_loop", "error": msg})
        return True, "Stopped re-running it (each attempt crashed the same way) and marked it failed."
    return False, ""


CARD_ACTION = "stuck_work_needs_person"


def _raise_card(connection: Any, run_id: str, ev: Mapping[str, Any], tri: Mapping[str, Any],
                nxt: str, headline: str) -> Optional[str]:
    """ONE card per cause per project: a second build stuck the same way joins the first card.
    A person-wait build already has its card (person_wait) and gets no second one."""
    from .core import create_blocking_decision
    from .decision_actions import OPEN_STATUSES

    pw = ev.get("person_wait") or {}
    if pw.get("decision_id"):
        return str(pw["decision_id"])
    run = ev.get("run") or {}
    project = run.get("project_id")
    cause_code = str(tri.get("cause_code") or "")
    row = connection.execute(text(
        "SELECT id FROM decisions WHERE project_id = :p AND status = ANY(:open_statuses) "
        "AND recommendation->>'action' = :a AND recommendation->>'cause_code' = :c "
        "ORDER BY created_at LIMIT 1"), {"p": project, "a": CARD_ACTION, "c": cause_code,
                                   "open_statuses": sorted(OPEN_STATUSES)}).scalar()
    if row:
        connection.execute(text(
            "UPDATE decisions SET recommendation = jsonb_set(recommendation, '{run_ids}', "
            "COALESCE(recommendation->'run_ids', '[]'::jsonb) || CAST(:r AS jsonb)), "
            "updated_at = now() WHERE id = :id AND NOT (COALESCE(recommendation->'run_ids', "
            "'[]'::jsonb) @> CAST(:r AS jsonb))"), {"r": json.dumps([run_id]), "id": row})
        return str(row)
    # ONE line: what the person must do. The cause, the diagram and the details ride with it.
    question = nxt
    return create_blocking_decision(
        connection, project, None, _clip(question, 280),
        {"action": CARD_ACTION, "run_id": run_id, "run_ids": [run_id], "cause_code": cause_code,
         "cause": tri.get("cause"), "why": headline, "next": nxt, "why_not": tri.get("why_not"),
         "title": run.get("title")},
        {}, cause_code, default_action="hold")


def diagnose(connection: Any, settings: Any, run_id: str, *, adapter: Any = None,
             use_model: bool = True, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Investigate one build, take the safe move, and write what was found on the run.
    Returns the diagnosis (also at `finalize_ctx.diagnosis`), or None for an unknown run."""
    from .core import emit_event

    now = now or _now()
    ev = evidence(connection, run_id, now=now)
    if not ev:
        return None
    tri = triage(ev)
    sig = signature(ev, tri)
    turn = _agent_turn(connection, settings, ev, tri, adapter=adapter) if use_model else {}
    chosen = turn.get("action") if turn.get("decided") and turn.get("action") in (tri.get("moves") or []) \
        else tri.get("pick")
    by = ({"agent": AGENT, **{k: v for k, v in (turn.get("by") or {}).items() if v}}
          if turn.get("decided") else {"agent": AGENT, "source": "BOSS's rules (no model answered)"
                                       if use_model else "BOSS's rules"})
    done, tried = (False, "")
    run_row = _load(connection, run_id) or {}
    fixes = int(_as_dict(run_row.get("finalize_ctx")).get("shepherd_fixes") or 0)
    if MOVES.get(str(chosen), {}).get("acts") and fixes >= MAX_FIXES_PER_ITEM \
            and chosen != "stop_crash_loop":
        # BOUNDED: never a fourth fix on the same build. Stop and say why it is not fixed.
        tri = {**tri, "cause_code": "capped", "why_not": WHY_NOT["capped"],
               "next": f"BOSS tried {fixes} fixes ({tri.get('cause')}). Look at its log, then "
                       "retry it or cancel it.", "who": "you"}
        chosen = "ask_person"
        tried = f"Stopped after {fixes} fixes; none got it moving."
    if MOVES.get(str(chosen), {}).get("acts"):
        try:
            with connection.begin_nested():
                done, tried = _act(connection, settings, run_id, str(chosen), ev)
        except Exception as exc:  # noqa: BLE001
            done, tried = False, f"Tried to {MOVES[str(chosen)]['label'].lower()} and failed: {type(exc).__name__}"
    elif chosen == "wait":
        tried = "Nothing to do: it frees up by itself."
    if done:
        fixes += 1
        connection.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
            "jsonb_build_object('shepherd_fixes', CAST(:n AS int)) WHERE id = :id"),
            {"n": fixes, "id": run_id})
        try:
            from .run_terminal import on_status_written

            on_status_written(connection, run_id)
        except Exception:  # noqa: BLE001
            pass
    if not tried:
        tried = {"ask_person": "Nothing BOSS can safely do moves it."}.get(str(chosen), "Looked only.")
    nxt, who = tri.get("next") or "", tri.get("who") or "you"
    needs_person = (chosen == "ask_person" or who == "you") and not (done and who == "boss")
    if chosen == "stop_crash_loop":
        needs_person = True
    headline = _clip(turn.get("reason") or f"{tri.get('cause')}. {tri.get('proof')}", 300)
    # A card only when work is HALTED on a person: an ended build is over (its page and this
    # diagnosis say why); a card for each of 150 old failures would bury the ones that matter.
    ended = (ev.get("run") or {}).get("status") in ENDED_STATUSES
    halted = needs_person and (not ended or tri.get("cause_code") in ("crash_loop", "no_grant"))
    card = _raise_card(connection, run_id, ev, tri, nxt, headline) if halted else None
    project = (ev.get("run") or {}).get("project_id")
    from .project_experience.common import href as _href

    links = {"log": _href(project, area="code_tasks", build=run_id, logs="build"),
             "run": _href(project, area="code_tasks", build=run_id)}
    diag = {
        "at": now.isoformat(), "signature": sig, "trigger": (ev.get("wait_reason") or {}).get("code")
        or (ev.get("run") or {}).get("status"),
        "headline": headline, "cause": tri.get("cause"), "cause_code": tri.get("cause_code"),
        "proof": _clip(tri.get("proof"), 300), "chosen": chosen, "acted": done, "tried": tried,
        "next": nxt, "who": who, "needs_person": needs_person, "card_id": card, "by": by,
        "why_not_fixed": tri.get("why_not") if needs_person else "", "links": links,
        "fixes": fixes, "max_fixes": MAX_FIXES_PER_ITEM,
        "steps": _steps(ev, tri, chosen=str(chosen), tried=tried, nxt=nxt, who=who),
        "cost_usd_micros": int(turn.get("cost_usd_micros") or 0),
    }
    connection.execute(text(
        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
        "jsonb_build_object('diagnosis', CAST(:d AS jsonb)) WHERE id = :id"),
        {"d": json.dumps(diag, default=str), "id": run_id})
    emit_event(connection, (ev.get("run") or {}).get("project_id"), "code_task.diagnosed", ACTOR,
               "code_task_run", run_id,
               {"cause_code": tri.get("cause_code"), "chosen": chosen, "acted": done,
                "needs_person": needs_person, "card_id": card, "by": by})
    return diag


def due(connection: Any, *, now: Optional[datetime] = None, limit: int = 20) -> list[str]:
    """Builds that need an investigation now: live and not moving for STUCK_MINUTES, failed or
    blocked in the last day, or paused on a person -- and either never diagnosed, diagnosed
    under other facts, or diagnosed long ago while still stuck."""
    from . import projects_archive

    now = now or _now()
    horizon = now - timedelta(minutes=STUCK_MINUTES)
    rows = connection.execute(text(
        "SELECT c.id, c.status, c.phase, c.finalize_ctx->'diagnosis' AS diag, c.updated_at, "
        "c.created_at, (SELECT max(t.created_at) FROM agent_turns t WHERE t.code_task_run_id = c.id) "
        "AS last_turn FROM code_task_runs c WHERE ("
        "  (c.status IN ('running', 'queued', 'repairing') AND c.created_at < :h)"
        "  OR (c.status IN ('failed', 'blocked') AND c.updated_at > :day)"
        "  OR c.status = 'waiting_on_person') AND " + projects_archive.live_sql("c.project_id")
        + " ORDER BY c.updated_at DESC LIMIT 200"),
        {"h": horizon, "day": now - timedelta(days=1)}).mappings().all()
    out: list[str] = []
    for r in rows:
        diag = _as_dict(r["diag"])
        if r["status"] in LIVE_STATUSES:
            last = max(d for d in (_aware(r["created_at"]), _aware(r["last_turn"])) if d is not None)
            if r["phase"] not in ("queued", "provisioning") and (now - last) < timedelta(minutes=STUCK_MINUTES):
                continue  # an agent took a turn recently: working
            if r["phase"] in ("queued", "provisioning") and (now - last) < timedelta(minutes=STUCK_MINUTES):
                continue
        at = _aware(diag.get("at"))
        if diag and at is not None and r["status"] in ENDED_STATUSES:
            # Ended: one investigation per ending (a later retry that fails again is a new one).
            upd = _aware(r["updated_at"])
            if upd is None or at >= upd - timedelta(minutes=5):
                continue
        if diag and at is not None:
            same = str(diag.get("signature") or "").split("|")[1:] == [str(r["status"]), str(r["phase"])]
            if same and (r["status"] not in LIVE_STATUSES or now - at < timedelta(minutes=REDIAGNOSE_MINUTES)):
                continue
        out.append(str(r["id"]))
        if len(out) >= limit:
            break
    return out


def sweep(engine: Any, settings: Any, *, now: Optional[datetime] = None, adapter: Any = None,
          max_turns: int = MAX_TURNS_PER_SWEEP) -> dict[str, Any]:
    """The always-on pass (monitor tick, every minute): investigate what is due, a few model
    turns at most per pass; the rest are diagnosed by BOSS's rules now and by the agent on a
    later pass. One build's failure never stops the others."""
    from ..db import begin

    now = now or _now()
    with begin(engine) as c:
        ids = due(c, now=now)
    report = {"diagnosed": 0, "acted": 0, "cards": 0, "errors": 0}
    for i, rid in enumerate(ids):
        try:
            with begin(engine) as c:
                d = diagnose(c, settings, rid, adapter=adapter, use_model=i < max_turns, now=now)
            if d:
                report["diagnosed"] += 1
                report["acted"] += 1 if d.get("acted") else 0
                report["cards"] += 1 if d.get("card_id") else 0
        except Exception:  # noqa: BLE001
            log.exception("stuck-work diagnosis failed run_id=%s", rid)
            report["errors"] += 1
    return report


def public_diagnosis(finalize_ctx: Any) -> Optional[dict[str, Any]]:
    """The diagnosis a surface shows: the five steps and the headline, nothing internal."""
    d = _as_dict(_as_dict(finalize_ctx).get("diagnosis"))
    if not d:
        return None
    return {k: d.get(k) for k in ("at", "headline", "cause", "chosen", "acted", "tried", "next",
                                  "who", "needs_person", "card_id", "by", "steps",
                                  "why_not_fixed", "links", "fixes", "max_fixes")}


# ───────────────────────────── 3. on the agents' work items and cards ─────────────────────────────


def runs_of_item(connection: Any, work_item_id: str) -> list[dict[str, Any]]:
    """The builds an always-on agent's work item started (directly, or through a workflow run it
    launched), newest first."""
    rows = connection.execute(text(
        "SELECT c.* FROM code_task_runs c WHERE c.id IN (SELECT ref FROM work_item_links "
        "WHERE work_item_id = :w AND kind IN ('code_task_run', 'build')) "
        "OR c.finalize_ctx->>'workflow_run_id' IN (SELECT ref FROM work_item_links "
        "WHERE work_item_id = :w AND kind = 'workflow_run') ORDER BY c.created_at DESC LIMIT 10"),
        {"w": work_item_id}).mappings().all()
    return [dict(r) for r in rows]


def item_status(connection: Any, work_item_id: str) -> dict[str, Any]:
    """What an agent's work item is waiting on now (its newest unfinished build's reason) and the
    Shepherd's diagnosis of it. {} when it launched no build or every build ended well."""
    try:
        with connection.begin_nested():
            runs = runs_of_item(connection, work_item_id)
            for run in runs:
                if str(run.get("status")) in ("completed", "cancelled", "simulated"):
                    continue
                why = wait_reason(connection, run)
                return {"why": why, "diagnosis": public_diagnosis(run.get("finalize_ctx")),
                        "run_id": run["id"], "run_title": _short_title(run.get("title"))}
    except Exception:  # noqa: BLE001 — an item without a reason still renders
        log.warning("item_status failed work_item_id=%s", work_item_id, exc_info=True)
    return {}


def card_diagnosis(connection: Any, decision: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The diagnosis a Needs-you card carries: of the build it names (a grant ask, a stuck-work
    card), or of the build its agent's work item launched (a squad that says it is stuck)."""
    rec = _as_dict(decision.get("recommendation"))
    try:
        with connection.begin_nested():
            for rid in [rec.get("run_id"), *(rec.get("run_ids") or [])]:
                if rid:
                    run = _load(connection, str(rid))
                    if run:
                        d = public_diagnosis(run.get("finalize_ctx"))
                        if d:
                            return d
            items = [rec.get("work_item_id")] + [e.get("work_item_id")
                                                 for e in rec.get("batch") or [] if isinstance(e, dict)]
            for wid in [w for w in items if w]:
                st = item_status(connection, str(wid))
                if st.get("diagnosis"):
                    return st["diagnosis"]
    except Exception:  # noqa: BLE001
        log.warning("card_diagnosis failed", exc_info=True)
    return None


# ───────────────────────────── 4. the Stalled work board ─────────────────────────────

BOARD_DAYS = 2


def _entry(run: Mapping[str, Any], diag: Mapping[str, Any]) -> dict[str, Any]:
    return {"run_id": run["id"], "title": _short_title(run.get("title")), "status": run.get("status"),
            "project_id": run.get("project_id"), "at": diag.get("at"),
            "cause": diag.get("cause"), "next": diag.get("next"), "who": diag.get("who"),
            "tried": diag.get("tried"), "why_not_fixed": diag.get("why_not_fixed") or "",
            "fixes": int(diag.get("fixes") or 0), "max_fixes": int(diag.get("max_fixes") or MAX_FIXES_PER_ITEM),
            "card_id": diag.get("card_id"), "links": diag.get("links") or {},
            "steps": diag.get("steps") or [], "by": diag.get("by") or {}}


def board(connection: Any, project_id: Optional[str] = None, *,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """What the Shepherd is doing about stalled work (Agents at work › Stalled work): what it
    FIXED (it acted and the build moved on), what it is TRYING (it acted; the build has not moved
    yet), and what NEEDS A PERSON (the one card each, with why BOSS cannot fix it). One project,
    or every project when `project_id` is None (the Shepherd scans them all)."""
    now = now or _now()
    rows = connection.execute(text(
        "SELECT * FROM code_task_runs WHERE finalize_ctx->'diagnosis' IS NOT NULL "
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) AND updated_at > :since "
        "ORDER BY updated_at DESC LIMIT 200"),
        {"p": project_id, "since": now - timedelta(days=BOARD_DAYS)}).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {"fixed": [], "trying": [], "needs_person": []}
    for r in rows:
        run = dict(r)
        diag = _as_dict(_as_dict(run.get("finalize_ctx")).get("diagnosis"))
        if not diag:
            continue
        status = str(run.get("status") or "")
        e = _entry(run, diag)
        if status == "cancelled":
            continue
        # Classified by the build's state NOW, not by what the diagnosis said then: a build the
        # grant resumed is no longer a person's problem.
        if status == "completed":
            if diag.get("acted") or _as_dict(run.get("finalize_ctx")).get("retried_after_upgrade"):
                out["fixed"].append(e)
            continue
        if status in LIVE_STATUSES:
            why = wait_reason(connection, run, now=now)
            if why and why["who"] in ("you", "stuck"):
                e["next"] = e["next"] or why["words"]
                out["needs_person"].append(e)
            elif why is None and diag.get("acted"):
                out["fixed"].append(e)
            else:
                out["trying"].append(e)
            continue
        person = diag.get("needs_person") or status in (WAITING_ON_PERSON, "blocked")
        out["needs_person" if person else "trying"].append(e)
    return {"owner": AGENT, "scans": "every project", "every_minutes": 1,
            "max_fixes": MAX_FIXES_PER_ITEM, **out,
            "counts": {k: len(v) for k, v in out.items()}}


# ───────────────────────────── 5. a workflow run with no build to explain it ─────────────────────────────


def workflow_wait_reason(connection: Any, wfr_id: str, *,
                         now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """What a workflow run waits for when no build carries it: a person's gate, a step whose
    worker went quiet, a failed step's own words, or plainly nothing. None while a step works."""
    now = now or _now()
    run = connection.execute(text(
        "SELECT id, status, error, blocked_reason, updated_at FROM workflow_runs WHERE id = :w"),
        {"w": wfr_id}).mappings().first()
    if run is None or run["status"] in ("completed", "cancelled"):
        return None
    steps = [dict(r) for r in connection.execute(text(
        "SELECT s.step_id, s.status, s.lease_expires_at, s.last_error, s.updated_at, "
        "d.status AS decision_status, d.question FROM workflow_step_runs s LEFT JOIN decisions d "
        "ON d.id = s.decision_id WHERE s.workflow_run_id = :w ORDER BY s.created_at"),
        {"w": wfr_id}).mappings().all()]

    def out(code: str, who: str, words: str, detail: str = "", since: Any = None) -> dict[str, Any]:
        s = _aware(since)
        return {"code": code, "who": who, "words": words, "detail": detail,
                "since": s.isoformat() if s else None, "eta_minutes": None}

    def err(v: Any) -> str:
        d = _as_dict(v)
        return str(d.get("message") or d.get("error") or (v if isinstance(v, str) else "") or "")

    if run["status"] in ("failed", "deadlettered", "blocked"):
        failed = next((s for s in reversed(steps) if s["status"] in ("failed", "deadlettered")), None)
        said = run["error"] or run["blocked_reason"] or (err(failed["last_error"]) if failed else "")
        where = f"step {failed['step_id']}: " if failed else ""
        return out(run["status"], "you", ("Blocked: " if run["status"] == "blocked" else "Failed: ")
                   + _clip(where + (said or "it stopped without saying why"), 140),
                   _clip(said, 300), run["updated_at"])
    from .decision_actions import OPEN_STATUSES

    gate = next((s for s in steps if s.get("decision_status") in OPEN_STATUSES), None)
    if gate:
        return out("person", "you", f"Waiting for you: {_clip(gate.get('question') or gate['step_id'], 120)}",
                   "", gate["updated_at"])
    for s in steps:
        lease = _aware(s.get("lease_expires_at"))
        if s["status"] in ("running", "leased") and lease is not None and lease < now:
            return out("lost", "boss", f"Step {s['step_id']}: its worker stopped answering "
                       f"{span_words(_mins((now - lease).total_seconds()))} ago", "", lease)
        if s["status"] in ("running", "leased"):
            return None
    quiet = _mins((now - (_aware(run["updated_at"]) or now)).total_seconds())
    waiting = [s["step_id"] for s in steps if s["status"] == "waiting"]
    if quiet >= STUCK_MINUTES * 4:
        return out("quiet", "stuck", f"Stuck: nothing has moved for {span_words(quiet)}",
                   f"Waiting at {', '.join(waiting)}" if waiting else "No step is running.",
                   run["updated_at"])
    if waiting:
        return out("waiting_step", "boss", f"Waiting at {', '.join(waiting[:2])}", "", run["updated_at"])
    return None
