"""Squads: who works an ambient item together, and who leads (BOSSAPP-908; design §13.1).

A SQUAD IS A ROSTER ON THE BOARD, NOT A NEW KIND OF AGENT
----------------------------------------------------------
Every BOSS agent is a `roles` row that runs through the one agent pipeline (BOSSAPP-759); default
agents differ only by the skills and MCP servers the console gave them. A squad adds nothing to
an agent. It is a decision about WHICH of the project's agents work one item and in which SEAT:

    lead        owns the item: holds the lease, proposes, convenes, hands off, resolves
    specialist  investigates and reports findings; may critique and vote
    critic      the checker of record: never the proposer, so maker-checker is never a lead
                agreeing with itself (design §11.4)

The roster is posted on the item's board as a `squad` message (append-only), so the seats, the
reason for each and any seat that could not be filled are part of the same record the Agent
Operations diagram draws. A lead hands the lead on with a `handoff` whose seat is `lead`.

TEMPLATES PER TASK CLASS (code, like the task-class catalogue)
--------------------------------------------------------------
A template names, per seat, the roles it prefers in order. Assembly takes the first candidate
that this project HAS, that is on duty (not paused or retired) and that has room (fewer than
`MAX_LIVE_ITEMS_PER_AGENT` live ambient items it already sits on). Among equal candidates the
one with the better track record on this task class goes first. A lead or critic seat no
template candidate can fill is filled from the rest of the workforce (the class's owner role
first for a lead); a specialist seat that cannot be filled is recorded as `unfilled` with the
reason, never invented. A squad needs at least a lead and a critic who are different agents.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board, collab
from . import task_classes as tc
from ._common import clean_text, party, party_id, party_kind

#: An agent already sitting on this many live ambient items is not seated on another.
MAX_LIVE_ITEMS_PER_AGENT = 3
#: Roles that are never seated: the person, and the coordinators that route rather than work.
NEVER_SEATED = frozenset({"human", "chief_of_staff", "metatron"})


class SquadError(RuntimeError):
    """A squad cannot be assembled (not enough agents on duty with room)."""


@dataclass(frozen=True)
class Seat:
    seat: str                      # lead | specialist | critic
    candidates: tuple[str, ...]    # role keys, preferred first
    why: str                       # ≤ 6 words: the badge on the diagram


@dataclass(frozen=True)
class SquadTemplate:
    key: str
    title: str
    task_classes: tuple[str, ...]
    seats: tuple[Seat, ...]
    #: The workflow a squad hands execution to by default (a squad deliberates; workflows
    #: execute — design §13.2). The lead may name another.
    execute_workflow: str = "fast_fix"


TEMPLATES: dict[str, SquadTemplate] = {t.key: t for t in (
    SquadTemplate(
        "incident", "Incident squad",
        ("incident_response", "incident_staging_mitigation", "perf_regression"),
        (Seat("lead", ("sre", "incident_commander", "operations_engineer"), "Runs the incident"),
         Seat("specialist", ("backend_engineer", "software_engineer"), "Reads the code path"),
         Seat("specialist", ("operations_engineer", "infra_steward"), "Reads the infra"),
         Seat("critic", ("adversarial_reviewer", "qa"), "Checks the mitigation"))),
    SquadTemplate(
        "failing_ci", "CI squad",
        ("ci_fix_boss_pr", "red_main", "flaky_test", "missing_tests", "workflow_failure",
         "platform_self_fix"),
        (Seat("lead", ("software_engineer", "backend_engineer"), "Owns the fix"),
         Seat("specialist", ("qa",), "Reads the tests"),
         Seat("specialist", ("sre", "operations_engineer"), "Reads the pipeline"),
         Seat("critic", ("adversarial_reviewer", "design_critic"), "Checks the fix"))),
    SquadTemplate(
        "dependency_cve", "CVE squad", ("security_patch", "dependency_upgrade"),
        (Seat("lead", ("security_reviewer", "software_engineer"), "Owns the patch"),
         Seat("specialist", ("software_engineer", "backend_engineer"), "Finds the call sites"),
         Seat("specialist", ("qa",), "Checks test cover"),
         Seat("critic", ("adversarial_reviewer", "chief_architect"), "Challenges the plan"))),
    SquadTemplate(
        "design_question", "Design squad", ("dead_code", "iac_drift"),
        (Seat("lead", ("chief_architect", "software_engineer"), "Frames the question"),
         Seat("specialist", ("software_engineer", "backend_engineer"), "Knows the code"),
         Seat("specialist", ("docs_writer", "pm"), "Knows the users"),
         Seat("critic", ("design_critic", "adversarial_reviewer"), "Argues the other side")),
        execute_workflow="minor_feature"),
    SquadTemplate(
        "general", "Squad", (),
        (Seat("lead", (), "Owns the item"),
         Seat("specialist", ("software_engineer", "chief_architect", "qa"), "Investigates"),
         Seat("critic", ("adversarial_reviewer", "qa", "design_critic"), "Checks the plan"))),
)}


def template_for(task_class: str, template: Optional[str] = None) -> SquadTemplate:
    if template:
        found = TEMPLATES.get(template)
        if found is None:
            raise ValueError(f"unknown squad template {template!r} (one of {', '.join(TEMPLATES)})")
        return found
    for t in TEMPLATES.values():
        if task_class in t.task_classes:
            return t
    return TEMPLATES["general"]


# ── the workforce ────────────────────────────────────────────────────────────────────────────

def workforce(connection: Connection, project_id: str, task_class: str) -> list[dict[str, Any]]:
    """Every agent this project has, with whether it can sit on a squad now and its record on
    this task class: [{role_key, name, on_duty, live_items, has_room, record}]."""
    rows = connection.execute(text("SELECT * FROM roles WHERE project_id = :p ORDER BY role_key"),
                              {"p": project_id}).mappings().all()
    load = {r["who"]: int(r["n"]) for r in connection.execute(text(
        "SELECT s.who, count(*) AS n FROM work_items w, "
        "LATERAL jsonb_array_elements_text(w.squad) AS s(who) WHERE w.project_id = :p "
        "AND w.ambient AND w.lifecycle = ANY(:live) GROUP BY s.who"),
        {"p": project_id, "live": list(board.LIVE)}).mappings()}
    record = {r["who"]: int(r["score"]) for r in connection.execute(text(
        "SELECT s.who, sum(CASE WHEN w.lifecycle = 'resolved' THEN 1 ELSE -1 END) AS score "
        "FROM work_items w, LATERAL jsonb_array_elements_text(w.squad) AS s(who) "
        "WHERE w.project_id = :p AND w.ambient AND w.task_class = :c "
        "AND w.lifecycle IN ('resolved', 'failed') GROUP BY s.who"),
        {"p": project_id, "c": task_class}).mappings()}
    out = []
    for row in rows:
        key = str(row["role_key"])
        if key in NEVER_SEATED:
            continue
        who = f"agent:{key}"
        lifecycle = str(dict(row).get("lifecycle") or "active")
        live = load.get(who, 0)
        out.append({"role_key": key, "party": who, "name": row["display_name"] or key,
                    "on_duty": lifecycle == "active", "lifecycle": lifecycle,
                    "live_items": live, "has_room": live < MAX_LIVE_ITEMS_PER_AGENT,
                    "record": record.get(who, 0)})
    return out


# ── assembling ───────────────────────────────────────────────────────────────────────────────

def assemble(connection: Connection, item_id: str, *, actor: str = "agent:chief_of_staff",
             template: Optional[str] = None, lease_minutes: int = 60,
             max_specialists: int = 2) -> dict[str, Any]:
    """Seat a squad on the item, record it on the board, and have the lead claim the item.

    Returns {"squad": roster, "lease_token", "message"}. Idempotent: an item that already has a
    squad returns it (with no token: the lead already holds the lease). Raises SquadError when
    the project cannot field a lead and a different critic, and board.Conflict when the item
    cannot be claimed (stopped, taken over, ended)."""
    item = board.get(connection, item_id)
    if item is None:
        raise LookupError(f"no ambient work item {item_id}")
    existing = roster(connection, item_id)
    if existing is not None:
        return {"squad": existing, "lease_token": None, "message": None, "outcome": "existing"}
    if item["lifecycle"] not in ("proposed", "claimed"):
        raise board.Conflict(f"the item is {item['lifecycle']}; a squad forms on a new item")
    klass = tc.task_class(item["task_class"])
    plan = template_for(klass.key, template)
    pool = workforce(connection, item["project_id"], klass.key)
    by_key = {a["role_key"]: a for a in pool}
    seated: list[dict[str, Any]] = []
    unfilled: list[dict[str, str]] = []
    taken: set[str] = set()

    def eligible(key: str) -> Optional[str]:
        agent = by_key.get(key)
        if agent is None:
            return "not in this project"
        if not agent["on_duty"]:
            return f"{agent['lifecycle']}"
        if not agent["has_room"]:
            return f"on {agent['live_items']} live items"
        if key in taken:
            return "already seated"
        return None

    def pick(candidates: Iterable[str]) -> Optional[str]:
        ranked = sorted(enumerate(candidates),
                        key=lambda ic: (ic[0], -(by_key.get(ic[1]) or {}).get("record", 0)))
        for _, key in ranked:
            if eligible(key) is None:
                return key
        return None

    def fallback(prefer: Iterable[str] = ()) -> Optional[str]:
        rest = list(prefer) + sorted((a["role_key"] for a in pool),
                                     key=lambda k: -by_key[k]["record"])
        return pick(dict.fromkeys(rest))

    specialists = 0
    for seat in plan.seats:
        if seat.seat == "specialist" and specialists >= max_specialists:
            continue
        key = pick(seat.candidates)
        if key is None and seat.seat == "lead":
            key = fallback((klass.owner_role,))
        if key is None and seat.seat == "critic":
            key = fallback()
        if key is None:
            reasons = "; ".join(f"{c}: {eligible(c)}" for c in seat.candidates) or "no candidate"
            unfilled.append({"seat": seat.seat, "reason": reasons[:200]})
            continue
        taken.add(key)
        specialists += seat.seat == "specialist"
        seated.append({"party": f"agent:{key}", "seat": seat.seat, "why": seat.why})
    seats = {m["seat"] for m in seated}
    if "lead" not in seats or "critic" not in seats:
        raise SquadError(
            "a squad needs a lead and a critic who are different agents on duty with room; "
            f"this project could not field them for {klass.title} "
            f"({'; '.join(u['seat'] + ': ' + u['reason'] for u in unfilled) or 'too few agents'})")
    # Seat order on the board is the turn order: lead, specialists, critic last.
    order = {"lead": 0, "specialist": 1, "critic": 2}
    seated.sort(key=lambda m: order[m["seat"]])
    lead = next(m["party"] for m in seated if m["seat"] == "lead")
    held = board.claim(connection, item_id, owner=lead, minutes=lease_minutes)
    for m in seated:
        board._squad_add(connection, item_id, m["party"])
    message = collab.post(connection, item_id, kind="squad", sender=party(actor), _internal=True,
                          body={"template": plan.key, "members": seated, "unfilled": unfilled})
    from . import squad_memory

    squad_memory.open(connection, item_id, goal=item["title"], actor=lead,
                      phase=f"{plan.title} seated")
    return {"squad": roster(connection, item_id), "lease_token": held["lease_token"],
            "message": message, "outcome": "assembled"}


def roster(connection: Connection, item_id: str) -> Optional[dict[str, Any]]:
    """The item's squad as it stands: the seated roster, with the CURRENT lead (a `handoff`
    whose seat is `lead` moves it). None when no squad was assembled."""
    rows = collab.messages(connection, item_id, kinds=("squad", "handoff"))
    squad = next((m for m in rows if m["kind"] == "squad"), None)
    if squad is None:
        return None
    body = dict(squad["body"] or {})
    members = [dict(m) for m in body.get("members") or ()]
    lead = body["lead"]
    for m in rows[rows.index(squad) + 1:]:
        b = m.get("body") or {}
        if m["kind"] == "handoff" and b.get("seat") == "lead" and \
                party_kind(str(b.get("to"))) == "agent":
            lead = b["to"]
    for m in members:
        if m["party"] == lead:
            m["seat"] = "lead"
        elif m["seat"] == "lead":
            m["seat"] = "specialist"
    if lead not in {m["party"] for m in members}:
        members.insert(0, {"party": lead, "seat": "lead", "why": "Took the lead"})
    template = TEMPLATES.get(body.get("template")) or TEMPLATES["general"]
    return {"message_id": squad["id"], "template": template.key, "title": template.title,
            "execute_workflow": template.execute_workflow, "lead": lead, "members": members,
            "critics": [m["party"] for m in members if m["seat"] == "critic"],
            "unfilled": list(body.get("unfilled") or ()), "assembled_by": squad["sender"]}


def seat_of(squad: Mapping[str, Any], who: str) -> Optional[str]:
    return next((m["seat"] for m in squad["members"] if m["party"] == who), None)


def hand_lead(connection: Connection, item_id: str, *, to: str, token: str,
              summary: str, lease_minutes: int = 60) -> dict[str, Any]:
    """The lead hands the lead (and the lease) to another squad member. Returns the new token."""
    squad = roster(connection, item_id)
    if squad is None:
        raise LookupError("this item has no squad")
    current = squad["lead"]
    to = party(to, allow_boss=False)
    if seat_of(squad, to) is None:
        raise ValueError(f"{to} is not on this item's squad")
    if to == current:
        raise ValueError("the lead hands the lead to someone else")
    board.assert_lease(connection, item_id, owner=current, token=token, lock=True)
    was = board.get(connection, item_id)["lifecycle"]
    connection.execute(text(
        "UPDATE work_items SET lease_owner = NULL, lease_kind = NULL, lease_token = NULL, "
        "lease_expires_at = NULL, lifecycle = CASE WHEN lifecycle = 'in_progress' THEN "
        "'claimed' ELSE lifecycle END WHERE id = :id"), {"id": item_id})
    held = board.claim(connection, item_id, owner=to, minutes=lease_minutes)
    if was == "in_progress":
        board.start(connection, item_id, owner=to, token=held["lease_token"])
    collab.post(connection, item_id, kind="handoff", sender=current,
                body={"to": to, "from": current, "seat": "lead",
                      "summary": clean_text(summary, 300) or "Handed the lead on"})
    return {"lead": to, "lease_token": held["lease_token"]}


def lead_token(connection: Connection, item_id: str, lead: str) -> Optional[str]:
    """The lead's fencing token, read by BOSS's runner acting for the lead (never serialised,
    never shown to a model). None when the lead no longer holds the item."""
    row = connection.execute(text(
        "SELECT lease_owner, lease_token FROM work_items WHERE id = :w"),
        {"w": item_id}).mappings().first()
    if row is None or row["lease_owner"] != lead:
        return None
    return row["lease_token"]


def label(who: str) -> str:
    return party_id(who).replace("_", " ")


__all__ = ["TEMPLATES", "Seat", "SquadTemplate", "SquadError", "template_for", "workforce",
           "assemble", "roster", "seat_of", "hand_lead", "lead_token", "label",
           "MAX_LIVE_ITEMS_PER_AGENT"]
