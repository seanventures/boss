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
* no NEW squad is seated in a project whose hour of person-facing cards is used up
  (policy.ask_room): its proposals would ask again, so they wait for room;
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
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import asks, board, collab, policy, squad_plugins, squad_turns, squads
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
    "  w.lease_owner, w.lease_kind, w.lease_expires_at, w.cost_usd_micros, "
    "  w.squad_budget_usd_micros, "
    "  (SELECT max(e.created_at) FROM events e WHERE e.project_id = w.project_id "
    "     AND e.event_type = 'ambient.squad_turn' AND e.entity_id = w.id) AS squad_turn_at, "
    "  (SELECT max(e.created_at) FROM events e WHERE e.project_id = w.project_id "
    "     AND e.event_type = 'ambient.item_transitioned' AND e.entity_id = w.id "
    "     AND e.data->>'to' = 'proposed' AND e.data ? 'withdrawn') AS returned_at, "
    "  (SELECT min(h.created_at) FROM work_item_messages h WHERE h.work_item_id = w.id "
    "     AND h.kind = 'handoff' AND h.sender = :cos) AS handed_at, "
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


def owed_since(row: Mapping[str, Any]) -> Any:
    """When the item was put back to its owners and is OWED their next turn: a person answered
    it, or the Chief of Staff withdrew the question it waited on (board.unask, "withdrawn"),
    after the squad's last turn. None when nothing is owed."""
    last = row.get("squad_turn_at")
    back = [t for t in (row.get("answered_at"), row.get("returned_at")) if t is not None]
    since = max(back) if back else None
    return since if since is not None and (last is None or since > last) else None


#: AGING: a due item whose owners have had no turn on it for this long goes ahead of priority
#: (oldest first), so no item waits forever while higher-scored ones keep coming back.
STARVE_MINUTES = 120


def starving_since(row: Mapping[str, Any]) -> Any:
    """When its owners last worked the item (or it was handed over), if that is longer ago than
    STARVE_MINUTES — else None."""
    waited = row.get("squad_turn_at") or row.get("handed_at")
    if waited is None or waited >= now() - timedelta(minutes=STARVE_MINUTES):
        return None
    return waited


def _epoch(value: Any) -> float:
    return value.timestamp() if value is not None else 0.0


def waits_on_a_decision(connection: Connection, row: Mapping[str, Any]) -> Optional[str]:
    """PARKED: the open decision the work this item launched waits on (a finished build's merge
    card, a held step) — the same test as a stalled hand-off (chief_of_staff._run_asks). Nothing
    the squad does can change it until the person decides, so it is not given turns; it is due
    again once that decision is answered or withdrawn."""
    from .. import stuck_work
    from .chief_of_staff import _run_asks

    runs = [str(r["id"]) for r in stuck_work.runs_of_item(connection, row["id"])]
    runs += [str(x) for x in connection.execute(text(
        "SELECT ref FROM work_item_links WHERE work_item_id = :w "
        "AND kind IN ('workflow_run', 'code_task')"), {"w": row["id"]}).scalars()]
    return _run_asks(connection, str(row["project_id"]), list(dict.fromkeys(runs)))


def due_items(connection: Connection, *, project_id: Optional[str] = None,
              limit: int = DEFAULT_ITEMS_PER_TICK,
              held: Optional[list[dict[str, Any]]] = None,
              spent: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """The handed-over items the owners should work this tick: first every item OWED a turn
    (put back on the board since its squad's last turn), then the rest by priority.

    OWED FIRST, NEVER STARVED (boss-dogfood 2026-11-07): the Chief of Staff put the Infra
    Steward's item back on the board when it withdrew its question, and it sat an hour with no
    turn — five higher-scored helpdesk CVE items, each due again every 30 minutes, filled the
    three slots of every tick. An owed item now goes ahead of priority; one that still cannot
    be worked this tick is appended to `held` with the reason, for the ledger (`_work`)."""
    incident = set(_incident_classes())
    out: list[dict[str, Any]] = []
    room: dict[str, int] = {}
    graced: dict[str, bool] = {}
    from .. import projects_archive   # an archived project's items are never worked

    rows = connection.execute(text(_HANDED.format(live=projects_archive.live_sql("w.project_id"))),
                              {"p": project_id, "cos": COS,
                                              "nosquad": f"{NO_SQUAD}%"}).mappings().all()
    # ITS SQUAD'S BUDGET IS SPENT: no turn can run, so it takes no slot. Spend above a cap is on
    # the compliance boundary: it goes to `spent`, which `_work` puts in front of a person on
    # the project's one card ("give it $1 more, or close it?"; asks.ask_budget) — never parked
    # in silence (boss-dogfood 2026-11-07: $1.53 of $1.50, then every tick refused it).
    over = [r for r in rows if r["has_squad"] and r["task_class"] not in incident
            and int(r.get("cost_usd_micros") or 0) >= squad_turns.budget_of(r)]
    if spent is not None:
        spent.extend({"id": r["id"], "project_id": r["project_id"]} for r in over)
    rows = [r for r in rows if r not in over]
    # Owed items first; then STARVING ones (due, but no turn for STARVE_MINUTES), oldest first;
    # then the rest in priority order (the sort is stable).
    rows = sorted(rows, key=lambda r: (owed_since(r) is None, starving_since(r) is None,
                                       _epoch(starving_since(r))))

    def hold(r: Mapping[str, Any], why: str, *, always: bool = False) -> None:
        """An owed item held back is said on the ledger; so is one a guard holds for good
        (`always`), once since its last squad turn."""
        since = owed_since(r) or (r.get("squad_turn_at") or r.get("handed_at") if always
                                  else None)
        if held is not None and since is not None:
            held.append({"id": r["id"], "project_id": r["project_id"], "why": why,
                         "since": since})

    for r in rows:
        if len(out) >= limit:
            if owed_since(r) is None:
                break
            hold(r, f"the owners' turns this tick were full ({limit} items install-wide); "
                    "it is first in line for the next tick")
            continue
        if policy.is_stopped(connection, r["project_id"]):
            hold(r, "ambient work is stopped in this project")
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
        parked = waits_on_a_decision(connection, r)
        if parked:
            # Its build finished and waits on the person's own card (helpdesk 2026-11-07: five
            # CVE items, each built and waiting on its merge card, took a squad turn every 30
            # minutes and every owner slot). Parked until that decision is answered.
            hold(r, "the work it launched waits on your open decision; its owners go on "
                    "once that is answered")
            continue
        if r["no_squad"]:
            hold(r, "this project could not seat a squad for it")
            continue
        # ONE OWNER FOR EACH PIECE OF WORK: what onboarding's backlog already proposes is the
        # person's to decide there — left to it (its findings become the backlog item's
        # evidence), never worked a second time by a squad.
        if asks.defer_to_backlog(connection, r["id"]):
            continue
        # A NEW PROJECT'S GRACE: no squad takes up the kinds of work onboarding proposes until
        # the person has looked at its backlog (policy.grace).
        if r["task_class"] in policy.ONBOARDING_CLASSES:
            if r["project_id"] not in graced:
                graced[r["project_id"]] = policy.grace(connection, r["project_id"]) is not None
            if graced[r["project_id"]]:
                hold(r, "the project is in its onboarding grace: the backlog is looked at first")
                continue
        if not r["has_squad"]:
            # THE RATE GUARD reaches the owners too: a new squad's proposal asks a person, so
            # while this project's hour of cards is used up no new squad is seated (it is next
            # tick's, or next hour's). Squads already working go on.
            if r["project_id"] not in room:
                room[r["project_id"]] = policy.ask_room(connection, r["project_id"])
            if r["lifecycle"] == "proposed" and room[r["project_id"]] > 0:
                out.append({**r, "kind": "assemble"})
            else:
                hold(r, "this project's hour of cards is used up, so no new squad is seated")
            continue
        roster = squads.roster(connection, r["id"]) or {"members": []}
        members = {m["party"] for m in roster["members"]}
        expires = r.get("lease_expires_at")
        if (r.get("lease_kind") == "agent" and r.get("lease_owner") != roster.get("lead")
                and expires is not None and expires > now()):
            # Another agent holds it (the lead resumes only a lapsed lease): said, not silent.
            hold(r, f"it is still leased by {r['lease_owner']} until {expires:%H:%M} UTC",
                 always=True)
            continue
        last = r["squad_turn_at"]
        stale = last is None or last < now() - timedelta(minutes=SQUAD_RECHECK_MINUTES)
        if stale or owed_since(r) is not None or _newer_than(connection, r["id"], last, members):
            out.append({**r, "kind": "squad", "owed": owed_since(r) is not None})
    return out


#: Guards whose stop leaves the item with no next turn until something changes: always said.
GUARD_STOPS = frozenset({"budget", "turn_cap", "envelope", "lease_lost", "stopped",
                         "taken_over", "no_squad"})

#: The question an owed item's silence is recorded under (once per return to the board).
Q_WHY_NO_TURN = "Give this item its owners' next turn now?"


def say_why_no_turn(connection: Connection, item_id: str, why: str, since: Any) -> bool:
    """AN OWED ITEM IS NEVER SILENT: the reason it got no turn, on the ledger as the Chief of
    Staff's decision — once per return to the board (not every tick). True if recorded."""
    from . import ledger

    item = board.get(connection, item_id)
    if item is None or item["lifecycle"] in board.TERMINAL:
        return False
    if connection.execute(text(
            "SELECT 1 FROM agent_act_decisions WHERE work_item_id = :w AND decided_by = :c "
            "AND question = :q AND created_at >= :s LIMIT 1"),
            {"w": item_id, "c": COS, "q": Q_WHY_NO_TURN, "s": since}).first():
        return False
    ledger.record_decision(
        connection, project_id=item["project_id"], decided_by=COS, work_item_id=item_id,
        task_class=item["task_class"], question=Q_WHY_NO_TURN, chosen="wait",
        options=["turn", "wait"], reversibility="reversible",
        undo={"action": "reopen", "work_item_id": item_id},
        evidence=[{"kind": "work_item", "ref": item_id}],
        rationale=f"It is back with its owners but they took no turn on it: {why}.")
    return True


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
        held: list[dict[str, Any]] = []
        spent: list[dict[str, Any]] = []
        todo = due_items(c, project_id=project_id,
                         limit=item_limit(settings) if limit is None else limit, held=held,
                         spent=spent)
    for h in held:
        _say_why(engine, h["id"], h["why"], h["since"], report)
    for s in spent:
        # A spent budget asks a person, on the project's one card (asks.ask_budget).
        try:
            with engine.begin() as c:
                if policy.is_stopped(c, s["project_id"]):
                    continue
                got = asks.ask_budget(c, settings, s["id"])
            report.setdefault("asked", []).append({"work_item_id": s["id"], **got})
        except Exception:  # noqa: BLE001 — one item's ask never stops the others
            log.warning("could not ask about the spent budget of %s", s["id"], exc_info=True)
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
            stop = run.get("stopped") or {}
            if stop.get("guard") in GUARD_STOPS or (entry.get("owed") and not run["turns"]):
                # A guard refused the squad's turns (its spend or turn cap, the envelope, a
                # lease, a take-over): said on the ledger — once since its last squad turn —
                # owed or not. boss-dogfood 2026-11-07: the item passed its $1.50 squad
                # budget at 23:28 and every later tick refused it without a word.
                since = (owed_since(entry) or entry.get("squad_turn_at")
                         or entry.get("handed_at"))
                _say_why(engine, item_id, f"a squad guard stopped it ({stop.get('guard')}: "
                                          f"{stop.get('reason')})", since, report)
        except Exception as exc:  # noqa: BLE001 — one item's failure never stops the others
            log.warning("ambient owners failed on %s", item_id, exc_info=True)
            report["worked"].append({"work_item_id": item_id, "kind": "error",
                                     "reason": f"{type(exc).__name__}: {exc}"[:300]})
            if owed_since(entry) is not None:
                _say_why(engine, item_id, f"its turn failed ({type(exc).__name__})",
                         owed_since(entry), report)
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


def _say_why(engine: Any, item_id: str, why: str, since: Any, report: dict[str, Any]) -> None:
    try:
        with engine.begin() as c:
            if say_why_no_turn(c, item_id, why, since):
                report.setdefault("held", []).append({"work_item_id": item_id, "why": why})
    except Exception:  # noqa: BLE001 — recording the reason never stops the tick
        log.warning("could not record why %s got no turn", item_id, exc_info=True)


def projects_with_owner_work(connection: Connection) -> list[str]:
    """Projects with a handed-over item still live (the tick visits them even with no new
    signal)."""
    return [str(p) for p in connection.execute(text(
        "SELECT DISTINCT w.project_id FROM work_items w WHERE w.ambient "
        "AND w.lifecycle IN ('proposed', 'claimed', 'in_progress') AND EXISTS (SELECT 1 FROM "
        "work_item_messages h WHERE h.work_item_id = w.id AND h.kind = 'handoff' "
        "AND h.sender = :cos)"), {"cos": COS}).scalars()]


__all__ = ["work", "due_items", "owed_since", "say_why_no_turn", "Q_WHY_NO_TURN",
           "projects_with_owner_work", "enabled", "item_limit", "SQUAD_ROUNDS_PER_TICK",
           "SQUAD_RECHECK_MINUTES", "NO_SQUAD"]
