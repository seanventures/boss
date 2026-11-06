"""Owners at work: after the Chief of Staff hands an item over, the owners work it TOGETHER
(BOSSAPP-902 integration of 904 → 908 / 910; spec §6.5).

ONE PATH FROM A SIGNAL TO A SQUAD
---------------------------------
    signal ─► Chief of Staff (chief_of_staff.triage_group)
                 ├─ A0            observe (nobody works it)
                 ├─ propose       Needs-you "Hand to <owner>?" ─► a person answers (answers.py)
                 └─ route         handoff ─────────────────────────┐
                                                                    ▼
              this module, on the same worker tick, for every handed-over item:
                 incident class   incident_squad.route_incident  (SRE lead, Investigator,
                                  Engineer, Reviewer; mitigation gated; resume on the answer)
                 any other class  squads.assemble (the project's own agents, by template)
                                  + squad_turns.collaborate (turns through the one agent path,
                                  consensus at round end, Needs you when a person must decide)

WHEN AN ITEM IS WORKED (bounded, in code, never a prompt)
---------------------------------------------------------
* only items the Chief of Staff (or the incident intake) HANDED OVER: a `handoff` from
  agent:chief_of_staff on the board; never an item a person holds, never one waiting on a
  person, never a Recommended-roadmap item (a person accepts those);
* at most `BOSS_AMBIENT_SQUAD_ITEMS` items per tick install-wide (default 3), highest priority
  first, each for at most SQUAD_ROUNDS_PER_TICK rounds;
* a squad that already took turns runs again only when something NEW happened since its last
  turn (a person answered, a run or panel it started came back, a non-member posted), or after
  SQUAD_RECHECK_MINUTES — so an idle squad costs nothing per minute;
* the kill switch (env, Stop all, the project's Stop), `BOSS_AMBIENT_COS=off` and
  `BOSS_AMBIENT_SQUADS=0` each stop owner turns; squad_turns' own guards (turn cap, spend cap,
  envelope, proposal cap, lease) apply on every turn.

A project that cannot field a squad (no lead, or no critic different from the lead) gets ONE
`escalate` on the item saying why, and the item is not retried until the board changes.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board, collab, policy, squad_plugins, squad_turns, squads
from ._common import now

log = logging.getLogger(__name__)

COS = "agent:chief_of_staff"
#: Rounds one squad may take in one tick (each round = one turn per member + the settle).
SQUAD_ROUNDS_PER_TICK = 2
#: An idle squad (nothing new on its board) looks again after this long.
SQUAD_RECHECK_MINUTES = 30
#: Default items worked per tick, install-wide (BOSS_AMBIENT_SQUAD_ITEMS overrides).
DEFAULT_ITEMS_PER_TICK = 3
NO_SQUAD = "No squad could be seated"
#: pg advisory lock key for the owners' turn ("ambient owners").
LOCK_KEY = 0x0A0B5E


def enabled(settings: Any) -> bool:
    return bool(int(getattr(settings, "ambient_squads", 1) or 0))


def item_limit(settings: Any) -> int:
    return max(0, int(getattr(settings, "ambient_squad_items", DEFAULT_ITEMS_PER_TICK)
                      if getattr(settings, "ambient_squad_items", None) is not None
                      else DEFAULT_ITEMS_PER_TICK))


def _incident_classes() -> tuple[str, ...]:
    from ..incident_squad import INCIDENT_CLASSES

    return tuple(INCIDENT_CLASSES)


#: Items handed over and still the owners' to work. `squad_turn_at` is the last squad turn.
_HANDED = (
    "SELECT w.id, w.project_id, w.task_class, w.lifecycle, w.answered_at, w.effective_score, "
    "  (SELECT max(e.created_at) FROM events e WHERE e.project_id = w.project_id "
    "     AND e.event_type = 'ambient.squad_turn' AND e.entity_id = w.id) AS squad_turn_at, "
    "  EXISTS (SELECT 1 FROM work_item_messages m WHERE m.work_item_id = w.id "
    "     AND m.kind = 'squad') AS has_squad, "
    "  EXISTS (SELECT 1 FROM work_item_messages m WHERE m.work_item_id = w.id "
    "     AND m.kind = 'escalate' AND m.body->>'reason' LIKE :nosquad) AS no_squad "
    "FROM work_items w WHERE w.ambient AND w.task_class <> 'roadmap_item' "
    "AND (CAST(:p AS TEXT) IS NULL OR w.project_id = :p) AND {live} "
    "AND w.lifecycle IN ('proposed', 'claimed', 'in_progress') "
    "AND COALESCE(w.lease_kind, '') <> 'person' "
    "AND EXISTS (SELECT 1 FROM work_item_messages h WHERE h.work_item_id = w.id "
    "     AND h.kind = 'handoff' AND h.sender = :cos) "
    "ORDER BY w.effective_score DESC NULLS LAST, w.updated_at LIMIT 200")


def _newer_than(connection: Connection, item_id: str, since: Any,
                members: set[str]) -> bool:
    """Something happened on the board since the squad's last turn that the squad did not
    write itself (a person, BOSS posting a run's result, another agent)."""
    if since is None:
        return True
    row = connection.execute(text(
        "SELECT 1 FROM work_item_messages WHERE work_item_id = :w AND created_at > :s "
        "AND NOT (sender = ANY(:m)) LIMIT 1"),
        {"w": item_id, "s": since, "m": sorted(members) or [""]}).first()
    return row is not None


def due_items(connection: Connection, *, project_id: Optional[str] = None,
              limit: int = DEFAULT_ITEMS_PER_TICK) -> list[dict[str, Any]]:
    """The handed-over items the owners should work this tick, highest priority first."""
    incident = set(_incident_classes())
    out: list[dict[str, Any]] = []
    from .. import projects_archive   # an archived project's items are never worked

    rows = connection.execute(text(_HANDED.format(live=projects_archive.live_sql("w.project_id"))),
                              {"p": project_id, "cos": COS,
                                              "nosquad": f"{NO_SQUAD}%"}).mappings().all()
    for r in rows:
        if len(out) >= limit:
            break
        if policy.is_stopped(connection, r["project_id"]):
            continue
        if r["task_class"] in incident:
            # The SRE squad's first pass; later passes are its own (resume on the answer,
            # incident_squad.tick while it watches recovery).
            triaged = connection.execute(text(
                "SELECT 1 FROM agent_act_decisions WHERE work_item_id = :w "
                "AND question = :q LIMIT 1"),
                {"w": r["id"], "q": _q_triage()}).first()
            if triaged is None and r["lifecycle"] == "proposed":
                out.append({**r, "kind": "incident"})
            continue
        if r["no_squad"]:
            continue
        if not r["has_squad"]:
            if r["lifecycle"] == "proposed":
                out.append({**r, "kind": "assemble"})
            continue
        roster = squads.roster(connection, r["id"]) or {"members": []}
        members = {m["party"] for m in roster["members"]}
        last = r["squad_turn_at"]
        stale = last is None or last < now() - timedelta(minutes=SQUAD_RECHECK_MINUTES)
        answered = r["answered_at"] is not None and (last is None or r["answered_at"] > last)
        if stale or answered or _newer_than(connection, r["id"], last, members):
            out.append({**r, "kind": "squad"})
    return out


def _q_triage() -> str:
    from ..incident_squad import Q_TRIAGE

    return Q_TRIAGE


def _assemble(engine: Any, item_id: str) -> Optional[str]:
    """Seat the squad. None when seated; the reason when the project cannot field one."""
    try:
        with engine.begin() as c:
            squads.assemble(c, item_id, actor=COS)
        return None
    except squads.SquadError as exc:
        with engine.begin() as c:
            collab.post(c, item_id, kind="escalate", sender=COS,
                        body={"to": COS, "reason": f"{NO_SQUAD}: {exc}"[:300]})
        return str(exc)
    except board.Conflict as exc:
        return str(exc)


def work(engine: Any, settings: Any, *, project_id: Optional[str] = None,
         limit: Optional[int] = None, model: Any = None, decider: Any = None,
         launchers: Optional[squad_plugins.Launchers] = None,
         incident_mind: Any = None, incident_effects: Any = None,
         rounds: int = SQUAD_ROUNDS_PER_TICK) -> dict[str, Any]:
    """The owners' turn on the tick. Returns {"worked": [...], "watching": {...}}.

    `model`/`decider`/`launchers` reach squad_turns (tests and demos pass scripted ones; None =
    the project's routed model through the one agent path and the real tool doors);
    `incident_mind`/`incident_effects` reach the incident squad the same way."""
    report: dict[str, Any] = {"worked": [], "watching": {}}
    if not enabled(settings):
        return {**report, "skipped": "BOSS_AMBIENT_SQUADS=0"}
    # ONE OWNERS' TURN AT A TIME, install-wide. The worker knocks every minute over HTTP with a
    # 30 s timeout while a squad's turns may take longer: a second tick must not run the same
    # squad beside the first (both would hold the lead's token). A session-level advisory lock
    # on a connection of its own, held for the whole turn; a tick that cannot take it skips.
    with engine.connect() as guard:
        if not guard.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            return {**report, "skipped": "busy"}
        try:
            return _work(engine, settings, report, project_id=project_id, limit=limit,
                         model=model, decider=decider, launchers=launchers,
                         incident_mind=incident_mind, incident_effects=incident_effects,
                         rounds=rounds)
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            guard.commit()


def _work(engine: Any, settings: Any, report: dict[str, Any], *, project_id: Optional[str],
          limit: Optional[int], model: Any, decider: Any,
          launchers: Optional[squad_plugins.Launchers], incident_mind: Any,
          incident_effects: Any, rounds: int) -> dict[str, Any]:
    from .. import incident_squad

    with engine.begin() as c:
        if policy.stop_state(c, None)["stopped"]:
            return {**report, "skipped": "stopped"}
        todo = due_items(c, project_id=project_id,
                         limit=item_limit(settings) if limit is None else limit)
    for entry in todo:
        item_id = entry["id"]
        pid = entry["project_id"]
        try:
            if entry["kind"] == "incident":
                with engine.begin() as c:
                    out = incident_squad.route_incident(
                        c, project_id=pid, item_id=item_id, settings=settings,
                        mind=incident_mind, effects=incident_effects)
                report["worked"].append({"work_item_id": item_id, "kind": "incident",
                                         "stage": out.get("stage")})
                continue
            if entry["kind"] == "assemble":
                why = _assemble(engine, item_id)
                if why is not None:
                    report["worked"].append({"work_item_id": item_id, "kind": "no_squad",
                                             "reason": why[:200]})
                    continue
            run = squad_turns.collaborate(engine, settings, item_id, rounds=rounds, model=model,
                                          decider=decider, launchers=launchers)
            report["worked"].append({
                "work_item_id": item_id, "kind": "squad", "rounds": run["rounds"],
                "turns": len(run["turns"]), "settled": [s.get("state") for s in run["settled"]],
                "stopped": (run.get("stopped") or {}).get("guard")})
        except Exception as exc:  # noqa: BLE001 — one item's failure never stops the others
            log.warning("ambient owners failed on %s", item_id, exc_info=True)
            report["worked"].append({"work_item_id": item_id, "kind": "error",
                                     "reason": f"{type(exc).__name__}: {exc}"[:300]})
    # Incidents the SRE squad is watching recover: look again (no model unless it has a verdict).
    with engine.begin() as c:
        pids = [project_id] if project_id else [str(p) for p in c.execute(text(
            "SELECT DISTINCT project_id FROM work_items WHERE ambient AND task_class = ANY(:c) "
            "AND lifecycle = 'proposed'"), {"c": list(_incident_classes())}).scalars()]
    for pid in pids:
        try:
            with engine.begin() as c:
                if policy.is_stopped(c, pid):
                    continue
                moved = incident_squad.tick(c, project_id=pid, settings=settings,
                                            mind=incident_mind, effects=incident_effects)
            if moved:
                report["watching"][pid] = moved
        except Exception:  # noqa: BLE001
            log.warning("incident watch failed for %s", pid, exc_info=True)
    return report


def projects_with_owner_work(connection: Connection) -> list[str]:
    """Projects with a handed-over item still live (the tick visits them even with no new
    signal)."""
    return [str(p) for p in connection.execute(text(
        "SELECT DISTINCT w.project_id FROM work_items w WHERE w.ambient "
        "AND w.lifecycle IN ('proposed', 'claimed', 'in_progress') AND EXISTS (SELECT 1 FROM "
        "work_item_messages h WHERE h.work_item_id = w.id AND h.kind = 'handoff' "
        "AND h.sender = :cos)"), {"cos": COS}).scalars()]


__all__ = ["work", "due_items", "projects_with_owner_work", "enabled", "item_limit",
           "SQUAD_ROUNDS_PER_TICK", "SQUAD_RECHECK_MINUTES", "NO_SQUAD"]
