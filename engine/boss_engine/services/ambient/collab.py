"""Collaboration primitives: typed board messages and consensus by risk (BOSSAPP-908 core).

THE BOARD, NOT A CHAT (design §2.2 pattern 8, §6, §13)
------------------------------------------------------
Agents coordinate by writing typed, validated messages onto the work item they share, not by
talking to each other in free text: blackboard systems beat agent dialogue on quality and token
cost, and a validated write is auditable (PatchBoard). A message is one of ten kinds, each with a
schema checked here; the board's own moves write theirs (board.claim writes `claim`,
ledger.record_decision writes `decision`, board.finish writes `resolve`). `a2a.py` stays for
"ask a colleague"; anything that changes the item's state goes through this module.

    finding     {summary, evidence[]}
    hypothesis  {summary, confidence 0..1, evidence[]}
    proposal    {action (catalogue), summary, verify?, undo?, confidence?, workflow?}
    critique    reply_to=<proposal>  {position: agree|object|concern|unsafe, summary}
    vote        reply_to=<proposal>  {vote: approve|reject|abstain, reason?}   one per voter
    handoff     {to: <party>, summary, seat?, via?: {kind, ref}, packet?: {...}}
                seat: the receiver's seat on this item, <=4 words ("lead" hands the lead on;
                "Investigator" labels an incident seat); work handed to a workflow or a panel
                `via` its run, with the packet it carries; a result handed back `returned_from` it
    escalate    {to: <party>, reason}
    claim / decision / resolve       written by board / ledger only
    squad       {template, lead, members[{party, seat, why}], unfilled[]}  squads.assemble only

NO HERDING (design §11.4): a vote never reads another agent's vote before casting its own.
`messages(..., viewer=...)` hides the votes on a proposal from a viewer who has not voted on it
yet, so an agent that reads the board before voting cannot copy the room.

CONSENSUS BY RISK (design §11.4; the operator's brief)
------------------------------------------------------
A panel on every small decision wastes tokens and time, so the mode is chosen by the risk of the
action, never by habit:

    low risk, reversible            single         the owner decides
    medium risk, or compensable     maker_checker  the proposer + a critic who is NOT the
                                                   proposer must agree; two objections → a person
    high risk                       squad_vote_and_person   meeting_consensus.tally_votes with
                                                   quorum ≥ 3 and ≥ 2/3 approve, AND a person
    irreversible / always a person  recommend_only the squad's verdict is the recommendation on a
                                                   Needs-you decision; a person decides

`tally` returns the consensus RECORD the ledger stores on the decision (participants, votes,
critiques, dissent, quorum rule, result), so the vote the diagram draws and the vote the decision
cites are the same rows.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..meeting_consensus import tally_votes
from . import task_classes as tc
from ._common import (clean_text, emit, new_id, party, party_kind, serialize_row,
                      short_label, verify_evidence)

KINDS = ("claim", "finding", "hypothesis", "proposal", "critique", "vote", "decision", "handoff",
         "escalate", "resolve", "squad")
#: What a handoff may travel through: a workflow run (execute) or a panel (deliberate).
VIA_KINDS = ("workflow_run", "meeting", "code_task")
SEATS = ("lead", "specialist", "critic")
#: Kinds anyone on the item may post; the rest are written by the board's own moves.
OPEN_KINDS = frozenset({"finding", "hypothesis", "proposal", "critique", "vote", "handoff",
                        "escalate"})
POSITIONS = ("agree", "object", "concern", "unsafe")
VOTES = ("approve", "reject", "abstain")
MODES = ("single", "maker_checker", "squad_vote_and_person", "recommend_only")
SQUAD_MIN_QUORUM = 3
SQUAD_THRESHOLD = 2 / 3


def consensus_mode(*, risk: str, reversibility: str, always_a_person: bool = False) -> str:
    if always_a_person or reversibility == "irreversible":
        return "recommend_only"
    if risk == "high":
        return "squad_vote_and_person"
    if risk == "medium" or reversibility == "compensable":
        return "maker_checker"
    return "single"


def satisfies(consensus: Optional[Mapping[str, Any]], required_mode: str) -> bool:
    """Does this consensus record let an agent/squad/policy decide under `required_mode`?
    Squad-vote and recommend-only modes always need a person, so no record satisfies them."""
    if required_mode == "single":
        return True
    if required_mode == "maker_checker":
        return bool(consensus) and consensus.get("mode") == "maker_checker" and \
            consensus.get("result") == "approved"
    return False


# ── messages ─────────────────────────────────────────────────────────────────────────────────

def _body(connection: Connection, project_id: str, kind: str, body: Mapping[str, Any],
          reply: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate a message body against its kind's schema; return the stored shape."""
    b = dict(body or {})

    def need(key: str, limit: int = 600) -> str:
        value = clean_text(b.get(key), limit)
        if not value:
            raise ValueError(f"a {kind} message needs {key}")
        return value

    if kind == "claim":
        return {"lease_minutes": int(b.get("lease_minutes") or 0)}
    if kind in ("finding", "hypothesis"):
        out: dict[str, Any] = {"summary": need("summary"),
                               "evidence": verify_evidence(connection, project_id,
                                                           b.get("evidence") or ())}
        if kind == "hypothesis":
            confidence = b.get("confidence")
            if confidence is None or not 0 <= float(confidence) <= 1:
                raise ValueError("a hypothesis carries its confidence (0..1)")
            out["confidence"] = round(float(confidence), 3)
        return out
    if kind == "proposal":
        act = tc.action(need("action", 60))
        out = {"action": act.key, "summary": need("summary"), "risk": act.risk,
               "reversibility": act.reversibility}
        if b.get("verify"):
            out["verify"] = clean_text(b["verify"], 300)
        if b.get("undo"):
            out["undo"] = dict(b["undo"]) if isinstance(b["undo"], Mapping) else {
                "action": clean_text(b["undo"], 120)}
        if b.get("confidence") is not None:
            out["confidence"] = round(float(b["confidence"]), 3)
        if b.get("workflow"):
            # The workflow a start_workflow / open_draft_pr proposal hands the work to.
            out["workflow"] = clean_text(b["workflow"], 80)
        return out
    if kind in ("critique", "vote"):
        if reply is None or reply["kind"] != "proposal":
            raise ValueError(f"a {kind} replies to a proposal on the same item")
        if kind == "critique":
            position = str(b.get("position") or "")
            if position not in POSITIONS:
                raise ValueError(f"a critique's position is one of {', '.join(POSITIONS)}")
            return {"position": position, "summary": need("summary")}
        vote = str(b.get("vote") or "")
        if vote not in VOTES:
            raise ValueError(f"a vote is one of {', '.join(VOTES)}")
        return {"vote": vote, "reason": clean_text(b.get("reason"), 300)}
    if kind == "decision":
        act_id = need("act_id", 60)
        found = connection.execute(text(
            "SELECT chosen FROM agent_act_decisions WHERE act_id = :a"), {"a": act_id}).first()
        if found is None:
            raise ValueError(f"no decision {act_id}")
        return {"act_id": act_id, "chosen": found[0]}
    if kind == "handoff":
        out = {"to": party(b.get("to")), "from": b.get("from"),
               "summary": clean_text(b.get("summary"), 300)}
        # The seat the receiver takes on this item ("lead", "Investigator"): the diagram's label
        # for that party on this item (read_model.graph), at most four words.
        seat = short_label(b.get("seat"), 4)
        if seat:
            out["seat"] = seat
        for key in ("via", "returned_from"):
            if b.get(key):
                ref = b[key] if isinstance(b[key], Mapping) else {}
                if ref.get("kind") not in VIA_KINDS or not str(ref.get("ref") or "").strip():
                    raise ValueError(f"a handoff's {key} is {{kind: {'|'.join(VIA_KINDS)}, ref}}")
                out[key] = {"kind": ref["kind"], "ref": clean_text(ref["ref"], 120)}
        if b.get("packet"):
            if not isinstance(b["packet"], Mapping):
                raise ValueError("a handoff packet is an object")
            out["packet"] = json.loads(json.dumps(dict(b["packet"]), default=str))
        return out
    if kind == "escalate":
        return {"to": party(b.get("to")), "reason": need("reason", 300)}
    if kind == "resolve":
        return {"outcome": need("outcome", 60), "summary": clean_text(b.get("summary"), 300)}
    if kind == "squad":
        members = []
        for m in b.get("members") or ():
            if m.get("seat") not in SEATS:
                raise ValueError(f"a squad seat is one of {', '.join(SEATS)}")
            members.append({"party": party(m.get("party"), allow_boss=False), "seat": m["seat"],
                            "why": clean_text(m.get("why"), 120)})
        leads = [m["party"] for m in members if m["seat"] == "lead"]
        if len(leads) != 1:
            raise ValueError("a squad has exactly one lead")
        return {"template": need("template", 60), "lead": leads[0], "members": members,
                "unfilled": [{"seat": clean_text(u.get("seat"), 20),
                              "reason": clean_text(u.get("reason"), 200)}
                             for u in b.get("unfilled") or ()]}
    raise ValueError(f"a message kind is one of {', '.join(KINDS)}")


def post(connection: Connection, item_id: str, *, kind: str, sender: str,
         body: Mapping[str, Any], reply_to: Optional[str] = None,
         act_id: Optional[str] = None, _internal: bool = False) -> dict[str, Any]:
    """Post one typed message on a live item. Returns the stored message."""
    if kind not in KINDS:
        raise ValueError(f"a message kind is one of {', '.join(KINDS)}")
    if kind not in OPEN_KINDS and not _internal:
        raise ValueError(f"{kind} messages are written by the board's own moves")
    who = party(sender)
    item = connection.execute(text(
        "SELECT id, project_id, lifecycle FROM work_items WHERE id = :w AND ambient"),
        {"w": item_id}).mappings().first()
    if item is None:
        raise LookupError(f"no ambient work item {item_id}")
    if item["lifecycle"] in ("resolved", "dismissed", "failed") and kind not in ("resolve",
                                                                     "decision"):
        raise ValueError(f"the item is {item['lifecycle']}; the board is closed")
    reply = None
    if reply_to:
        reply = connection.execute(text(
            "SELECT id, kind, sender FROM work_item_messages WHERE id = :m AND work_item_id = :w"),
            {"m": reply_to, "w": item_id}).mappings().first()
        if reply is None:
            raise LookupError(f"no message {reply_to} on this item")
    stored = _body(connection, item["project_id"], kind, body, reply)
    if kind == "vote":
        dup = connection.execute(text(
            "SELECT 1 FROM work_item_messages WHERE reply_to = :r AND sender = :s AND "
            "kind = 'vote'"), {"r": reply_to, "s": who}).first()
        if dup is not None:
            raise ValueError(f"{who} already voted on this proposal; post a critique to add to it")
    message_id = new_id("wim")
    row = connection.execute(text(
        "INSERT INTO work_item_messages (id, project_id, work_item_id, kind, sender, body, "
        "reply_to, act_id) VALUES (:id, :p, :w, :k, :s, CAST(:b AS JSONB), :r, :a) RETURNING *"),
        {"id": message_id, "p": item["project_id"], "w": item_id, "k": kind, "s": who,
         "b": json.dumps(stored, default=str), "r": reply_to, "a": act_id}).mappings().one()
    if party_kind(who) in ("agent", "person"):
        connection.execute(text(
            "UPDATE work_items SET squad = CASE WHEN squad @> CAST(:one AS JSONB) THEN squad "
            "ELSE squad || CAST(:one AS JSONB) END, updated_at = now() WHERE id = :w"),
            {"one": json.dumps([who]), "w": item_id})
    emit(connection, item["project_id"], "ambient.message_posted", who, "work_item", item_id,
         {"message_id": message_id, "kind": kind, "reply_to": reply_to})
    return serialize_row(row)


def messages(connection: Connection, item_id: str, *, kinds: Iterable[str] = (),
             viewer: Optional[str] = None) -> list[dict[str, Any]]:
    """The item's board, oldest first. With `viewer` (a party), votes on proposals the viewer has
    not voted on yet are withheld (no herding); a person sees everything."""
    rows = [serialize_row(r) for r in connection.execute(text(
        "SELECT * FROM work_item_messages WHERE work_item_id = :w "
        "AND (cardinality(CAST(:k AS TEXT[])) = 0 OR kind = ANY(CAST(:k AS TEXT[]))) "
        "ORDER BY created_at, id"), {"w": item_id, "k": [k for k in kinds if k in KINDS]}
    ).mappings()]
    if viewer and party_kind(viewer) not in ("person",):
        voted = {r["reply_to"] for r in rows if r["kind"] == "vote" and r["sender"] == viewer}
        rows = [r for r in rows if r["kind"] != "vote" or r["reply_to"] in voted]
    return rows


# ── consensus ────────────────────────────────────────────────────────────────────────────────

def tally(connection: Connection, item_id: str, proposal_id: str, *,
          mode: Optional[str] = None) -> dict[str, Any]:
    """The consensus record for one proposal, under the mode its risk requires.

    result: approved | rejected | no_quorum | pending | needs_person. `needs_person` is True when
    a person must still decide (high risk, irreversible, two objections, an 'unsafe' critique).
    """
    from . import policy

    proposal = connection.execute(text(
        "SELECT m.*, w.project_id AS item_project, w.task_class, w.squad FROM work_item_messages m "
        "JOIN work_items w ON w.id = m.work_item_id WHERE m.id = :m AND m.work_item_id = :w "
        "AND m.kind = 'proposal'"), {"m": proposal_id, "w": item_id}).mappings().first()
    if proposal is None:
        raise LookupError(f"no proposal {proposal_id} on {item_id}")
    body = proposal["body"] or {}
    if mode is None:
        verdict = policy.gate(connection, proposal["item_project"], proposal["task_class"],
                              body["action"])
        mode = verdict["consensus_mode"]
    if mode not in MODES:
        raise ValueError(f"consensus mode is one of {', '.join(MODES)}")
    replies = connection.execute(text(
        "SELECT sender, kind, body, created_at FROM work_item_messages WHERE reply_to = :m "
        "AND kind IN ('critique', 'vote') ORDER BY created_at, id"),
        {"m": proposal_id}).mappings().all()
    proposer = proposal["sender"]
    critiques = [{"critic": r["sender"], "position": r["body"].get("position"),
                  "summary": r["body"].get("summary")} for r in replies if r["kind"] == "critique"]
    votes = [{"voter": r["sender"], "vote": r["body"].get("vote"),
              "reason": r["body"].get("reason")} for r in replies if r["kind"] == "vote"]
    participants = list(dict.fromkeys([proposer] + [c["critic"] for c in critiques]
                                      + [v["voter"] for v in votes]))
    checkers = [c for c in critiques if c["critic"] != proposer]
    agree = [c for c in checkers if c["position"] == "agree"] + [
        {"critic": v["voter"], "position": "agree"} for v in votes
        if v["voter"] != proposer and v["vote"] == "approve"]
    objections = [c for c in checkers if c["position"] in ("object", "unsafe")] + [
        {"critic": v["voter"], "position": "object", "summary": v.get("reason")} for v in votes
        if v["voter"] != proposer and v["vote"] == "reject"]
    unsafe = [c for c in critiques if c["position"] == "unsafe"]
    dissent = [{"party": c["critic"], "position": c["position"],
                "reason": c.get("summary") or "objected"} for c in objections]
    counted = None
    if mode == "single":
        rule, result, needs_person = "the owner decides at or above the class threshold", \
            "approved", False
    elif mode == "maker_checker":
        rule = "the proposer and a critic who is not the proposer agree; two objections go to a " \
               "person"
        if unsafe or len(objections) >= 2:
            result, needs_person = "needs_person", True
        elif agree:
            result, needs_person = "approved", False
        elif objections:
            result, needs_person = "rejected", False
        else:
            result, needs_person = "pending", False
    else:
        squad = [s for s in (proposal["squad"] or []) if party_kind(s) in ("agent",)]
        eligible = max(len(set(squad) | {v["voter"] for v in votes}), SQUAD_MIN_QUORUM)
        counted = tally_votes(votes, eligible_count=eligible,
                              quorum=max(SQUAD_MIN_QUORUM, -(-eligible // 2)),
                              threshold=SQUAD_THRESHOLD, default_action="ask a person")
        rule = (f"quorum ≥ {counted['quorum']}, ≥ 2/3 approve, then a person decides"
                if mode == "squad_vote_and_person" else
                "the squad recommends; only a person may decide")
        result = "needs_person" if unsafe else counted["outcome"]
        needs_person = True
        dissent += [{"party": d["role"], "position": d["position"], "reason": d["reason"]}
                    for d in counted["dissent_records"]
                    if d["role"] not in {x["party"] for x in dissent}]
    return {"mode": mode, "proposal_id": proposal_id, "proposer": proposer,
            "action": body.get("action"), "participants": participants, "votes": votes,
            "critiques": critiques, "dissent": dissent, "quorum_rule": rule, "result": result,
            "needs_person": needs_person, "tally": counted}
