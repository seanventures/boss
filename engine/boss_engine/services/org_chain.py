"""AN ORG CHART THAT CARRIES AUTHORITY (BOSSAPP-885).

`org_assignments.reports_to` was display-only: the operator could say "QA reports to the
Engineering Captain", the chart drew it, and nothing else in BOSS read it. A stalled build went
straight to a person, a retired manager left its reports starting work as if nothing had changed,
and a stuck agent's work had no automatic owner. This module makes the declared line mean
something, in four places and nowhere else:

1. THE CHAIN. ``chain_of_command(subject)`` walks ``reports_to`` up from an agent: the owner, its
   manager, the manager's manager … Cycle-safe (a loop ends the walk, marked), retired-aware (an
   agent whose ``roles.lifecycle`` is ``retired`` is still drawn, flagged), and it ends at a person
   (``reports_to = 'person:<id>'``). With no declared lines the chain is the agent alone.
2. ESCALATION WALKS THE CHAIN. Where a stall would interrupt a person, the responsible agent's
   manager — an AGENT — is offered the work first (``escalate_first``): one row in
   ``org_chain_escalations`` with a deadline. The manager takes one governed turn (take over, or
   pass); a pass or a deadline moves the offer one rung up; a chain with no agent left FALLS
   THROUGH to the exact person path the caller would have taken (``fallback``), so work is never
   stranded. Every run gets that chance once: a second escalation of the same run goes to the
   person. P1-5's execution-policy escalation to ``responsible_person`` is untouched — it stays the
   final arm, and the responsible person may always take over (``authority_for``).
3. TAKEOVER. ``take_over`` reassigns the work item (work_items.update_item: assignee) and stamps
   the running runs with the new assignee and the AUTHORITY PATH — who acted, under whose chain —
   on the run's own lineage record (``finalize_ctx.authority_path``, beside ``authority``). A run
   the chain was holding resumes under the manager.
4. RETIRED MANAGERS BLOCK DELEGATION. An agent whose chain passes through a retired manager cannot
   START new governed work (``refuse_if_under_retired`` → :class:`RetiredChainError`, naming the
   gap) until it is reassigned; work already running finishes.

THE DEFAULT IS BYTE-IDENTICAL: a project with no ``org_assignments`` rows (or no agent above the
stuck one) gets ``None`` from ``escalate_first`` after reads only — no row, no event, no change —
and the caller's person path runs exactly as before. The test suite enforces it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

from .core import emit_event, make_id
from .setup_service import SetupError

log = logging.getLogger("boss.org_chain")

AGENT_PREFIX = "agent:"
PERSON_PREFIX = "person:"
#: How long one manager rung holds an offer before it moves up the chain (or falls through).
TAKEOVER_MINUTES = 15
#: A chain deeper than this is a modelling error, not an org: the walk stops.
MAX_DEPTH = 24
#: The run phase while the chain (not a person) holds a paused run.
PHASE_AWAITING_MANAGER = "awaiting_manager"
#: person_wait reasons the chain may take first: a STALL (the run supervisor's reach-out). An
#: authority blocker (no grant, an always-on review) needs a person by definition — a manager
#: agent cannot give a grant — so it goes straight to the person, as before.
PERSON_WAIT_CHAIN_REASONS = frozenset({"supervisor_reach_out"})
ORIGINS = ("person_wait", "stuck_work")
TAKE_OVER = "take_over"
PASS = "pass"


class RetiredChainError(SetupError, ValueError):
    """The agent's chain of command passes through a retired manager: no new governed work
    until it is reassigned. A SetupError (the operator can fix it) and a ValueError (every start
    door already turns those into a 4xx with the sentence)."""


class TakeoverRefused(ValueError):
    """The one asking to take over has no authority over this work."""


# ── parties ──────────────────────────────────────────────────────────────────────────────────

def party(value: Any) -> str:
    """`agent:<role>` / `person:<id>`; a bare role key is an agent."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith(AGENT_PREFIX) or raw.startswith(PERSON_PREFIX):
        return raw
    return AGENT_PREFIX + raw


def _key(value: Any) -> str:
    p = party(value)
    return p[len(AGENT_PREFIX):] if p.startswith(AGENT_PREFIX) else p


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _edges(connection: Any, project_id: str) -> dict[str, Optional[str]]:
    rows = connection.execute(text(
        "SELECT agent_id, reports_to, display_name FROM org_assignments WHERE project_id = :p"),
        {"p": project_id}).mappings().all()
    return {r["agent_id"]: (r["reports_to"] or None) for r in rows}


def _roles(connection: Any, project_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT role_key, display_name, COALESCE(lifecycle, 'active') AS lifecycle FROM roles "
        "WHERE project_id = :p"), {"p": project_id}).mappings().all()
    return {r["role_key"]: dict(r) for r in rows}


def _names(connection: Any, project_id: str) -> dict[str, str]:
    rows = connection.execute(text(
        "SELECT agent_id, display_name FROM org_assignments WHERE project_id = :p "
        "AND display_name IS NOT NULL"), {"p": project_id}).mappings().all()
    return {r["agent_id"]: r["display_name"] for r in rows}


# ── 1. the chain ─────────────────────────────────────────────────────────────────────────────

def chain_of_command(connection: Any, project_id: str, subject: Any) -> list[dict[str, Any]]:
    """[owner, manager, manager's manager, …] — each rung `{party, kind, id, name, retired}`.

    Cycle-safe: a manager already on the chain ends the walk and the last rung says `cycle`.
    Retired-aware: a retired agent stays on the chain, flagged (callers decide what it means).
    A person (`reports_to = 'person:<id>'`) is the top: people are not walked further."""
    start = party(subject)
    if not start:
        return []
    edges = _edges(connection, project_id)
    roles = _roles(connection, project_id) if edges else {}
    names = _names(connection, project_id) if edges else {}

    def rung(p: str) -> dict[str, Any]:
        if p.startswith(PERSON_PREFIX):
            ident = p[len(PERSON_PREFIX):]
            return {"party": p, "kind": "person", "id": ident, "name": ident, "retired": False}
        key = _key(p)
        role = roles.get(key) or {}
        return {"party": p, "kind": "agent", "id": key,
                "name": names.get(key) or role.get("display_name") or key.replace("_", " ").title(),
                "retired": str(role.get("lifecycle") or "active") == "retired"}

    out = [rung(start)]
    if start.startswith(PERSON_PREFIX):
        return out
    seen = {_key(start)}
    cursor = _key(start)
    while len(out) <= MAX_DEPTH:
        parent = edges.get(cursor)
        if not parent:
            break
        if parent.startswith(PERSON_PREFIX):
            out.append(rung(parent))
            break
        if parent in seen:
            out[-1]["cycle"] = True
            break
        seen.add(parent)
        out.append(rung(AGENT_PREFIX + parent))
        cursor = parent
    return out


def acting_manager_for(connection: Any, project_id: str, agent: Any) -> Optional[dict[str, Any]]:
    """Who manages this agent NOW: the first rung above it that is not retired (a retired direct
    manager is passed over to the next one up), or None at the top of the chart."""
    for r in chain_of_command(connection, project_id, agent)[1:]:
        if not r["retired"]:
            return r
    return None


def is_under_retired_manager(connection: Any, project_id: str,
                             agent: Any) -> Optional[dict[str, Any]]:
    """The first retired manager on this agent's chain (the gap), or None. The agent itself
    being retired is its own lifecycle's business, not its chain's."""
    for r in chain_of_command(connection, project_id, agent)[1:]:
        if r["retired"]:
            return r
    return None


def retired_gap_words(agent: Any, gap: Mapping[str, Any]) -> str:
    who = _key(agent)
    return (f"'{who}' cannot start new work: its chain of command passes through "
            f"'{gap.get('id')}', which is retired. Reassign '{who}' to an active manager "
            "(Agents > Organization > Planned roles), or reinstate "
            f"'{gap.get('id')}'. Work it already has running finishes.")


def refuse_if_under_retired(connection: Any, project_id: str, agent: Any) -> None:
    """The start door's check (code tasks, a named workflow step). Raises RetiredChainError."""
    if not agent:
        return
    gap = is_under_retired_manager(connection, project_id, agent)
    if gap is not None:
        raise RetiredChainError(retired_gap_words(agent, gap))


def _work_item(connection: Any, project_id: str, work: Any) -> Optional[dict[str, Any]]:
    if isinstance(work, Mapping):
        return dict(work)
    if not work:
        return None
    row = connection.execute(text(
        "SELECT id, project_id, title, assignee, responsible_person FROM work_items "
        "WHERE id = :w AND project_id = :p"), {"w": str(work), "p": project_id}).mappings().first()
    return dict(row) if row else None


def authority_for(connection: Any, project_id: str, taker: Any,
                  work: Any) -> Optional[dict[str, Any]]:
    """Under whose chain `taker` may take this work over, or None.

    * a non-retired manager anywhere above the work's assignee on the assignee's chain — the path
      is the chain from the assignee up to the taker;
    * the work item's responsible person (P1-5: who answers for it) — always, as the final arm."""
    who = party(taker)
    item = _work_item(connection, project_id, work)
    if not who or not item:
        return None
    assignee = party(item.get("assignee"))
    if assignee and assignee != who:
        chain = chain_of_command(connection, project_id, assignee)
        for i, r in enumerate(chain[1:], start=1):
            if r["party"] == who:
                if r["retired"]:
                    return None
                return {"via": "chain", "owner": assignee,
                        "path": [c["party"] for c in chain[:i + 1]]}
    if item.get("responsible_person") and party(item["responsible_person"]) == who:
        return {"via": "responsible_person", "owner": assignee or None,
                "path": [p for p in (assignee, who) if p]}
    return None


def can_take_over(connection: Any, project_id: str, taker: Any, work: Any) -> bool:
    return authority_for(connection, project_id, taker, work) is not None


# ── 2. escalation walks the chain ────────────────────────────────────────────────────────────

def _minutes(settings: Any) -> int:
    try:
        return max(1, int(getattr(settings, "org_chain_takeover_minutes", 0) or TAKEOVER_MINUTES))
    except (TypeError, ValueError):
        return TAKEOVER_MINUTES


def _managers_above(chain: list[dict[str, Any]], after: Optional[str] = None) -> list[dict[str, Any]]:
    """The agent rungs that may be offered the work, above `after` (or above the owner): never a
    retired one, never past a person (people are the person path's)."""
    out: list[dict[str, Any]] = []
    passed = after is None
    for r in chain[1:]:
        if r["kind"] == "person":
            break
        if not passed:
            passed = r["party"] == after
            continue
        if not r["retired"]:
            out.append(r)
    return out


def _row(connection: Any, esc_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text("SELECT * FROM org_chain_escalations WHERE id = :i"),
                             {"i": esc_id}).mappings().first()
    return dict(row) if row else None


def open_for_run(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM org_chain_escalations WHERE run_id = :r AND status = 'open' LIMIT 1"),
        {"r": run_id}).mappings().first()
    return dict(row) if row else None


def subject_of_run(connection: Any, run_id: str) -> tuple[Optional[str], Optional[str]]:
    """(the agent responsible for a code task run, its work item): the item's assignee when that
    is an agent (P1-5: who works it), else the run's builder."""
    row = connection.execute(text(
        "SELECT r.spec, r.finalize_ctx, r.work_item_id, w.assignee FROM code_task_runs r "
        "LEFT JOIN work_items w ON w.id = r.work_item_id WHERE r.id = :r"),
        {"r": run_id}).mappings().first()
    if row is None:
        return None, None
    if str(row.get("assignee") or "").startswith(AGENT_PREFIX):
        return row["assignee"], row.get("work_item_id")
    from ..identity.runs import resolved_builder

    builder = resolved_builder(_json(row.get("spec"), {}) or {}, _json(row.get("finalize_ctx"), {}) or {})
    return (party(builder) if builder else None), row.get("work_item_id")


def escalate_first(connection: Any, settings: Any = None, *, project_id: str, origin: str,
                   run_id: str, reason: str, question: str, fallback: Mapping[str, Any],
                   subject: Any = None, work_item_id: Optional[str] = None,
                   now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Offer a stalled run's work to the responsible agent's manager before a person is asked.

    Returns the open offer (the caller must NOT interrupt a person now), or None — no agent
    manager above it, or this run already had its chance — and the caller takes its person path
    unchanged. `fallback` is what that person path needs when the chain falls through."""
    if origin not in ORIGINS:
        raise ValueError(f"origin is one of {', '.join(ORIGINS)}")
    held = connection.execute(text(
        "SELECT * FROM org_chain_escalations WHERE run_id = :r ORDER BY created_at DESC LIMIT 1"),
        {"r": run_id}).mappings().first()
    if held is not None:
        return dict(held) if held["status"] == "open" else None
    if subject is None:
        subject, found_item = subject_of_run(connection, run_id)
        work_item_id = work_item_id or found_item
    owner = party(subject)
    if not owner or owner.startswith(PERSON_PREFIX):
        return None
    chain = chain_of_command(connection, project_id, owner)
    managers = _managers_above(chain)
    if not managers:
        return None
    first = managers[0]
    now = now or _now()
    esc_id = make_id("orgesc")
    connection.execute(text(
        "INSERT INTO org_chain_escalations (id, project_id, origin, run_kind, run_id, "
        "work_item_id, subject, chain, rung, offered_to, reason, question, status, deadline, "
        "fallback, created_at, updated_at) VALUES (:id, :p, :o, 'code_task', :r, :w, :s, "
        "CAST(:c AS jsonb), :rung, :to, :why, :q, 'open', :d, CAST(:f AS jsonb), :now, :now)"),
        {"id": esc_id, "p": project_id, "o": origin, "r": run_id, "w": work_item_id, "s": owner,
         "c": json.dumps(chain), "rung": chain.index(first), "to": first["party"],
         "why": str(reason or "")[:200], "q": str(question or "")[:2000],
         "d": now + timedelta(minutes=_minutes(settings)),
         "f": json.dumps(dict(fallback), default=str), "now": now})
    emit_event(connection, project_id, "org.takeover_offered", "boss", "code_task_run", run_id,
               {"escalation_id": esc_id, "offered_to": first["party"], "owner": owner,
                "chain": [r["party"] for r in chain], "origin": origin, "reason": reason})
    return _row(connection, esc_id)


def _advance(connection: Any, settings: Any, esc: Mapping[str, Any], *, now: datetime,
             why: str) -> dict[str, Any]:
    """One rung up, or — no agent left — fall through to the person path."""
    chain = _json(esc.get("chain"), []) or []
    nxt = _managers_above(chain, after=esc["offered_to"])
    if nxt:
        to = nxt[0]
        connection.execute(text(
            "UPDATE org_chain_escalations SET offered_to = :to, rung = :rung, asked_at = NULL, "
            "deadline = :d, updated_at = :now WHERE id = :i AND status = 'open'"),
            {"to": to["party"], "rung": chain.index(to), "i": esc["id"], "now": now,
             "d": now + timedelta(minutes=_minutes(settings))})
        if esc["origin"] == "person_wait":
            # The run's wait reason names who holds it now.
            connection.execute(text(
                "UPDATE code_task_runs SET finalize_ctx = jsonb_set(finalize_ctx, "
                "'{person_wait,offered_to}', to_jsonb(CAST(:to AS text))) WHERE id = :r "
                "AND finalize_ctx->'person_wait' IS NOT NULL"),
                {"to": to["party"], "r": esc["run_id"]})
        emit_event(connection, esc["project_id"], "org.takeover_escalated", "boss",
                   "code_task_run", esc["run_id"],
                   {"escalation_id": esc["id"], "from": esc["offered_to"], "offered_to": to["party"],
                    "why": why})
        return {"escalation_id": esc["id"], "moved_to": to["party"]}
    return fall_through(connection, settings, esc, now=now, why=why)


def fall_through(connection: Any, settings: Any, esc: Mapping[str, Any], *,
                 now: Optional[datetime] = None, why: str = "") -> dict[str, Any]:
    """The chain is exhausted: close the offer and take the caller's person path, unchanged."""
    now = now or _now()
    changed = connection.execute(text(
        "UPDATE org_chain_escalations SET status = 'fell_through', outcome = :why, "
        "resolved_at = :now, updated_at = :now WHERE id = :i AND status = 'open'"),
        {"why": why[:400], "now": now, "i": esc["id"]}).rowcount
    if not changed:
        return {"escalation_id": esc["id"], "fell_through": False}
    emit_event(connection, esc["project_id"], "org.takeover_fell_through", "boss",
               "code_task_run", esc["run_id"],
               {"escalation_id": esc["id"], "last_offered_to": esc["offered_to"], "why": why})
    fallback = _json(esc.get("fallback"), {}) or {}
    _FALLBACKS[esc["origin"]](connection, settings, esc, fallback)
    return {"escalation_id": esc["id"], "fell_through": True}


def _fallback_person_wait(connection: Any, settings: Any, esc: Mapping[str, Any],
                          fallback: Mapping[str, Any]) -> None:
    from . import person_wait

    run = connection.execute(text("SELECT status, phase, task_id FROM code_task_runs WHERE id = :r"),
                             {"r": esc["run_id"]}).mappings().first()
    if run is None or run["status"] != person_wait.STATUS_WAITING_ON_PERSON \
            or run["phase"] != PHASE_AWAITING_MANAGER:
        return
    # The budget hold was released when the chain paused it: nothing more to release.
    noop = type("NoBudget", (), {"release": staticmethod(lambda **_: None)})()
    person_wait.enter_waiting_on_person(
        connection, noop, run_id=esc["run_id"], project_id=esc["project_id"],
        task_id=fallback.get("task_id") or run.get("task_id"),
        reason=str(fallback.get("reason") or esc.get("reason") or ""),
        question=str(fallback.get("question") or esc.get("question") or ""),
        blocker_type=str(fallback.get("blocker_type") or person_wait.BLOCKER_AUTHORITY_NO_GRANT),
        refused_grant_id=fallback.get("refused_grant_id"), chain=False)


def _fallback_stuck_work(connection: Any, settings: Any, esc: Mapping[str, Any],
                         fallback: Mapping[str, Any]) -> None:
    from . import stuck_work

    stuck_work.raise_card_after_chain(connection, esc["run_id"], fallback)


_FALLBACKS: dict[str, Callable[..., None]] = {
    "person_wait": _fallback_person_wait,
    "stuck_work": _fallback_stuck_work,
}


def _still_held(connection: Any, esc: Mapping[str, Any]) -> bool:
    """Is the run still where the chain left it? (A grant appeared and person_wait resumed it, a
    person cancelled it …: then the offer is moot.)"""
    run = connection.execute(text("SELECT status, phase FROM code_task_runs WHERE id = :r"),
                             {"r": esc["run_id"]}).mappings().first()
    if run is None:
        return False
    if esc["origin"] == "person_wait":
        return run["status"] == "waiting_on_person" and run["phase"] == PHASE_AWAITING_MANAGER
    return run["status"] in ("running", "queued", "repairing")


SITUATION = ("{agent} manages {owner} on this project's org chart. {owner}'s build has stalled "
             "and, before a person is interrupted, its manager is offered the work.")
REQUEST = ("Build {run_id} ({title}) owned by {owner} has stopped: {question} Take it over (you "
           "become its assignee and it resumes under your direction; say what to do differently) "
           "or pass it up the chain.")
MEANS = {
    TAKE_OVER: "take the work over: become its assignee and resume it with your direction",
    PASS: "pass: this needs someone above you (or a person); move it up the chain",
}


def _manager_turn(connection: Any, settings: Any, esc: Mapping[str, Any]) -> dict[str, Any]:
    """The offered manager's ONE governed turn through the one agent pipeline (AgentService
    .decide, BOSSAPP-759). Never raises; no decision leaves the offer open until its deadline."""
    try:
        from .agent_service import AgentService, Choice

        title = connection.execute(text("SELECT title FROM code_task_runs WHERE id = :r"),
                                   {"r": esc["run_id"]}).scalar() or ""
        manager = _key(esc["offered_to"])
        owner = _key(esc["subject"])
        choices = [Choice(TAKE_OVER, MEANS[TAKE_OVER], arguments={"properties": {
                       "note": {"type": "string", "description": "what to do differently"}}}),
                   Choice(PASS, MEANS[PASS])]
        got = AgentService(settings).decide(
            connection, project_id=esc["project_id"], actor=f"agent:{manager}", role=manager,
            agent=manager, external_ref=f"code_task_run:{esc['run_id']}",
            ref=f"org_takeover:{esc['id']}",
            situation=SITUATION.format(agent=manager, owner=owner),
            message=REQUEST.format(run_id=esc["run_id"], title=title, owner=owner,
                                   question=esc.get("question") or esc.get("reason") or ""),
            choices=choices, context={"chain": _json(esc.get("chain"), []),
                                      "reason": esc.get("reason")},
            read=None, min_confidence=0.0, context_limit=4000)
        if not got.decided:
            return {}
        return {"action": got.action, "note": (got.arguments or {}).get("note") or got.reason or ""}
    except Exception:  # noqa: BLE001 — no turn: the deadline still moves the offer on
        log.info("org takeover turn failed", exc_info=True)
        return {}


def poll_due(connection: Any, settings: Any = None, *, now: Optional[datetime] = None,
             manager_turn: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]] = None,
             ) -> dict[str, int]:
    """Move every open offer on: ask the offered manager once, act on its answer, and when its
    deadline passes move it up the chain or fall through. Returns counts."""
    from . import projects_archive

    now = now or _now()
    turn = manager_turn or (lambda esc: _manager_turn(connection, settings, esc))
    rows = connection.execute(text(
        "SELECT * FROM org_chain_escalations WHERE status = 'open' AND "
        + projects_archive.live_sql("project_id") + " ORDER BY created_at"),
    ).mappings().all()
    out = {"taken": 0, "moved": 0, "fell_through": 0, "superseded": 0, "waiting": 0}
    for row in rows:
        esc = dict(row)
        try:
            with connection.begin_nested():
                if not _still_held(connection, esc):
                    connection.execute(text(
                        "UPDATE org_chain_escalations SET status = 'superseded', outcome = "
                        "'the run moved on by itself', resolved_at = :now, updated_at = :now "
                        "WHERE id = :i AND status = 'open'"), {"i": esc["id"], "now": now})
                    out["superseded"] += 1
                    continue
                if esc.get("asked_at") is None:
                    connection.execute(text(
                        "UPDATE org_chain_escalations SET asked_at = :now, updated_at = :now "
                        "WHERE id = :i"), {"i": esc["id"], "now": now})
                    answer = dict(turn(esc) or {})
                    if answer.get("action") == TAKE_OVER:
                        take_over(connection, settings, project_id=esc["project_id"],
                                  taker=esc["offered_to"], run_id=esc["run_id"],
                                  actor=esc["offered_to"], note=str(answer.get("note") or ""),
                                  now=now)
                        out["taken"] += 1
                        continue
                    if answer.get("action") == PASS:
                        moved = _advance(connection, settings, esc, now=now,
                                         why=f"{esc['offered_to']} passed")
                        out["fell_through" if moved.get("fell_through") else "moved"] += 1
                        continue
                aware = esc["deadline"]
                if aware.tzinfo is None:
                    aware = aware.replace(tzinfo=timezone.utc)
                if aware <= now:
                    moved = _advance(connection, settings, esc, now=now,
                                     why=f"{esc['offered_to']} did not answer in time")
                    out["fell_through" if moved.get("fell_through") else "moved"] += 1
                else:
                    out["waiting"] += 1
        except Exception:  # noqa: BLE001 — one bad offer never stops the others moving
            log.warning("org chain offer %s not moved", esc.get("id"), exc_info=True)
    return out


# ── 3. takeover ──────────────────────────────────────────────────────────────────────────────

def _live_runs(connection: Any, item_id: Optional[str], run_id: Optional[str]) -> list[dict[str, Any]]:
    if item_id:
        rows = connection.execute(text(
            "SELECT id, project_id, status, phase, task_id FROM code_task_runs "
            "WHERE work_item_id = :w AND status IN ('running', 'queued', 'repairing', "
            "'waiting_on_person')"), {"w": item_id}).mappings().all()
        found = [dict(r) for r in rows]
        if run_id and all(r["id"] != run_id for r in found):
            found += _live_runs(connection, None, run_id)
        return found
    if not run_id:
        return []
    row = connection.execute(text(
        "SELECT id, project_id, status, phase, task_id FROM code_task_runs WHERE id = :r"),
        {"r": run_id}).mappings().first()
    return [dict(row)] if row else []


def _resume(connection: Any, run: Mapping[str, Any], esc: Optional[Mapping[str, Any]]) -> bool:
    """Put a run the chain was holding back in the queue, from the stage it reached."""
    from .jobs import JOB_PRIORITY_INTERACTIVE, enqueue_job

    if esc is None:
        return False
    if esc["origin"] == "person_wait":
        claimed = connection.execute(text(
            "UPDATE code_task_runs SET status = 'running', phase = 'queued', updated_at = now() "
            "WHERE id = :r AND status = 'waiting_on_person' AND phase = :ph"),
            {"r": run["id"], "ph": PHASE_AWAITING_MANAGER}).rowcount
        if not claimed:
            return False
    elif run["status"] in ("running", "queued", "repairing"):
        # A stalled live build: its current job is ended and a fresh one starts the step again
        # (stuck_work's restart_step), now under the manager.
        connection.execute(text(
            "UPDATE jobs SET status = 'cancelled', lease_owner = NULL, lease_token_hash = NULL, "
            "lease_expires_at = NULL, updated_at = now(), last_error = CAST(:e AS jsonb) "
            "WHERE kind = 'code.task' AND payload->>'run_id' = :r AND status IN "
            "('queued', 'leased', 'running', 'failed_retryable')"),
            {"r": run["id"], "e": json.dumps({"message": "replaced: its manager took it over"})})
    else:
        return False
    enqueue_job(connection, run["project_id"], "code.task", {"run_id": run["id"]},
                task_id=run.get("task_id"), priority=JOB_PRIORITY_INTERACTIVE)
    return True


def take_over(connection: Any, settings: Any = None, *, project_id: str, taker: Any,
              actor: Any, work_item_id: Optional[str] = None, run_id: Optional[str] = None,
              note: str = "", now: Optional[datetime] = None) -> dict[str, Any]:
    """A manager (agent or person) takes a report's work over.

    Authority: the taker must sit above the work's assignee on the assignee's chain (not
    retired), or be the item's responsible person, or be the manager an open offer names. The
    work item's assignee becomes the taker (work_items.update_item, the one assignee seam);
    each live run of it records the new assignee and the authority path on its lineage record
    (`finalize_ctx.authority_path`); a run the chain was holding resumes. Raises TakeoverRefused.
    """
    from . import work_items as wi

    now = now or _now()
    who = party(taker)
    by = party(actor) if str(actor or "").startswith((AGENT_PREFIX, PERSON_PREFIX)) \
        else str(actor or "")
    esc = open_for_run(connection, run_id) if run_id else None
    if run_id and not work_item_id:
        work_item_id = connection.execute(text(
            "SELECT work_item_id FROM code_task_runs WHERE id = :r AND project_id = :p"),
            {"r": run_id, "p": project_id}).scalar()
    if esc is None and work_item_id:
        row = connection.execute(text(
            "SELECT * FROM org_chain_escalations WHERE work_item_id = :w AND status = 'open' "
            "ORDER BY created_at LIMIT 1"), {"w": work_item_id}).mappings().first()
        esc = dict(row) if row else None
    item = _work_item(connection, project_id, work_item_id) if work_item_id else None

    authority: Optional[dict[str, Any]] = None
    if esc is not None and esc.get("offered_to") == who:
        chain = _json(esc.get("chain"), []) or []
        parties = [r["party"] for r in chain]
        upto = parties.index(who) + 1 if who in parties else len(parties)
        authority = {"via": "chain", "owner": esc["subject"], "path": parties[:upto]}
    elif item is not None:
        authority = authority_for(connection, project_id, who, item)
    elif esc is not None:
        # No work item: the run's own chain (its builder) is the authority.
        chain = chain_of_command(connection, project_id, esc["subject"])
        for i, r in enumerate(chain[1:], start=1):
            if r["party"] == who and not r["retired"]:
                authority = {"via": "chain", "owner": esc["subject"],
                             "path": [c["party"] for c in chain[:i + 1]]}
    if authority is None:
        raise TakeoverRefused(
            f"{who or 'That actor'} cannot take this work over: it is not above "
            f"{(item or {}).get('assignee') or (esc or {}).get('subject') or 'its assignee'} on "
            "the org chart, nor the person responsible for it.")

    record = {"at": now.isoformat(), "taken_by": who, "acted_by": by or who,
              "via": authority["via"], "owner": authority.get("owner"),
              "path": authority["path"], "note": str(note or "")[:1000],
              "escalation_id": (esc or {}).get("id")}
    if item is not None:
        wi.update_item(connection, settings, project_id=project_id, item_id=item["id"],
                       fields={"assignee": who}, actor=by or who)
    resumed: list[str] = []
    runs = _live_runs(connection, (item or {}).get("id"), run_id)
    for run in runs:
        steer = [f"{who} took this over: {note}"] if note else []
        connection.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = jsonb_set(jsonb_set(jsonb_set("
            "COALESCE(finalize_ctx, '{}'::jsonb), '{assignee}', to_jsonb(CAST(:who AS text))), "
            "'{authority_path}', COALESCE(finalize_ctx->'authority_path', '[]'::jsonb) || "
            "CAST(:rec AS jsonb)), '{steer_notes}', COALESCE(finalize_ctx->'steer_notes', "
            "'[]'::jsonb) || CAST(:steer AS jsonb)), updated_at = now() WHERE id = :r"),
            {"who": who, "rec": json.dumps([record]), "steer": json.dumps(steer), "r": run["id"]})
        held = esc if esc is not None and esc["run_id"] == run["id"] else None
        if _resume(connection, run, held):
            resumed.append(run["id"])
    if esc is not None:
        connection.execute(text(
            "UPDATE org_chain_escalations SET status = 'taken', taken_by = :who, "
            "authority_path = CAST(:rec AS jsonb), outcome = 'taken over', resolved_at = :now, "
            "updated_at = :now WHERE id = :i AND status = 'open'"),
            {"who": who, "rec": json.dumps(record), "now": now, "i": esc["id"]})
    emit_event(connection, project_id, "org.takeover", by or who,
               "work_item" if item else "code_task_run",
               (item or {}).get("id") or run_id or "",
               {**record, "runs": [r["id"] for r in runs], "resumed": resumed})
    return {"taken_by": who, "authority": record, "work_item_id": (item or {}).get("id"),
            "runs": [r["id"] for r in runs], "resumed": resumed,
            "escalation_id": (esc or {}).get("id")}


# ── reads for the console ────────────────────────────────────────────────────────────────────

def open_offers(connection: Any, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT e.id, e.run_id, e.work_item_id, e.subject, e.offered_to, e.reason, e.question, "
        "e.deadline, e.chain, e.origin, r.title FROM org_chain_escalations e "
        "LEFT JOIN code_task_runs r ON r.id = e.run_id WHERE e.project_id = :p "
        "AND e.status = 'open' ORDER BY e.created_at"), {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["deadline"] = d["deadline"].isoformat() if d.get("deadline") else None
        d["chain"] = [c.get("party") for c in (_json(d.get("chain"), []) or [])]
        out.append(d)
    return out


def chain_view(connection: Any, project_id: str, agent: Any) -> dict[str, Any]:
    chain = chain_of_command(connection, project_id, agent)
    gap = next((r for r in chain[1:] if r["retired"]), None)
    manager = next((r for r in chain[1:] if not r["retired"]), None)
    return {"agent": party(agent), "chain": chain, "acting_manager": manager,
            "retired_gap": gap,
            "blocked": retired_gap_words(agent, gap) if gap else None}


__all__ = ["RetiredChainError", "TakeoverRefused", "PHASE_AWAITING_MANAGER",
           "PERSON_WAIT_CHAIN_REASONS", "chain_of_command", "acting_manager_for",
           "is_under_retired_manager", "refuse_if_under_retired", "authority_for", "can_take_over",
           "escalate_first", "poll_due", "fall_through", "take_over", "open_offers", "chain_view",
           "open_for_run", "subject_of_run", "party"]
