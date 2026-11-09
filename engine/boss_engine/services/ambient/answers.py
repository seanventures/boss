"""A person answered an ambient Needs-you card: ONE path carries the answer to its owner
(BOSSAPP-902 integration; spec §6.6).

Every ambient card is a `decisions` row raised by `board.needs_you`, so its recommendation
carries `work_item_id` and `asked_by`. The person answers it where every decision is answered
(Command Center / Approvals / the drawer → DecisionService.transition), and decisions.py calls
`on_answered` — here — for any decision whose recommendation names a work item. This module is
the only place that decides who acts on that answer:

    recommendation                       carried to
    action = incident_squad.mitigate     incident_squad.on_needs_you_answered (SRE squad resumes:
                                         approve = roll back + fix, revise = fix only, else hold)
    card = ambient (the project's one   asks.carry_card per entry by its kind, and
      card, asks.py)                     chief_of_staff.carry_card for its hand-offs
    asked_by = agent:chief_of_staff      chief_of_staff.carry_card (approve → handoff to the
                                         owner of EVERY item on the card, whose squad works it
                                         on the next tick; reject → dismissed, cools down)
    proposal_id (a squad's card)         squad_turns.person_answered (the person's ledger row;
                                         the squad carries it out at its next round)
    anything else                        board.answered (the item goes back to its holder)

Each carrier is idempotent (it checks the item is still waiting / the outcome is unset), so the
tick's safety net (`sweep`, for a card answered by a path that bypassed decisions.py — a hold
default expiring, a direct SQL fix-up) can call the same functions without double handling.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board

log = logging.getLogger(__name__)

COS = "agent:chief_of_staff"
#: The decision statuses that are an answer (pending / delegated still wait).
ANSWERED = ("approved", "executed", "rejected", "revision_requested", "expired")


def _verdict(status: str) -> str:
    return {"approved": "approve", "executed": "approve", "rejected": "reject",
            "revision_requested": "revise"}.get(status, "hold")


def on_answered(connection: Connection, settings: Any, decision: Mapping[str, Any], actor: str,
                verdict: str, note: Optional[str] = None) -> Optional[str]:
    """Carry one answered ambient card to its owner. `verdict` is approve | reject | revise
    (DecisionService's action). Returns which carrier took it, or None (not an ambient card)."""
    rec = dict(decision.get("recommendation") or {})
    item_id = rec.get("work_item_id")
    if not item_id or not rec.get("asked_by"):
        return None
    item = board.get(connection, str(item_id))
    if item is None:
        return None
    if rec.get("card") == "ambient":
        # THE PROJECT'S ONE CARD (asks.py): one answer reaches every entry, each by its own
        # carrier — the Chief of Staff's hand-offs, a squad's proposal, a stuck squad, the SRE
        # squad's escalation.
        from .asks import carry_card as carry_entries
        from .chief_of_staff import carry_card

        carry_card(connection, decision_id=str(decision["id"]), recommendation=rec)
        carry_entries(connection, settings, decision, actor, verdict, note)
        return "card"
    if rec.get("action") == "incident_squad.mitigate":
        from ..incident_squad import on_needs_you_answered

        if item["lifecycle"] != "needs_you":
            return "incident:already"
        on_needs_you_answered(connection, settings, decision, actor, verdict, note)
        return "incident"
    if rec.get("asked_by") == COS:
        from .chief_of_staff import carry_card

        # One card may ask about several items (a batched card): the answer reaches each.
        carry_card(connection, decision_id=str(decision["id"]), recommendation=rec)
        return "chief_of_staff"
    if item["lifecycle"] != "needs_you" or item.get("needs_you_decision_id") != decision["id"]:
        return "already"
    if rec.get("proposal_id"):
        from .squad_turns import person_answered

        person_answered(connection, str(item_id), actor=actor, approve=verdict == "approve",
                        rationale=note or "")
        return "squad"
    board.answered(connection, str(item_id), actor=actor)
    return "board"


def sweep(connection: Connection, settings: Any, project_id: str) -> list[dict[str, Any]]:
    """The tick's safety net: cards answered while their item still waits (an answer that did
    not come through DecisionService). Carried through the same `on_answered`."""
    rows = connection.execute(text(
        "SELECT d.id, d.project_id, d.status, d.recommendation, d.approved_by, d.executed_by, "
        "d.resolution_note FROM work_items w JOIN decisions d ON d.id = w.needs_you_decision_id "
        "WHERE w.project_id = :p AND w.ambient AND w.lifecycle = 'needs_you' "
        "AND d.status = ANY(:a) AND (COALESCE(d.recommendation->>'asked_by', '') <> :cos "
        "  OR d.recommendation->>'card' = 'ambient') "
        "ORDER BY d.updated_at LIMIT 50"),
        {"p": project_id, "a": list(ANSWERED), "cos": COS}).mappings().all()
    done = []
    for row in rows:
        who = str(row["approved_by"] or row["executed_by"] or "operator")
        try:
            with connection.begin_nested():
                took = on_answered(connection, settings, dict(row), who, _verdict(row["status"]),
                                   row["resolution_note"])
        except Exception:  # noqa: BLE001 — one card's failure is logged, the rest go on
            log.warning("carrying answer %s failed", row["id"], exc_info=True)
            continue
        done.append({"decision_id": row["id"], "carried_to": took})
    return done


__all__ = ["on_answered", "sweep", "ANSWERED"]
