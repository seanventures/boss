"""Agent Operations read model: each work item as a FLOW DIAGRAM, lists of them, the scorecard.

VISIBILITY IS A DIAGRAM, NOT TEXT (design §11.0 — the operator's standing rule, repeated)
----------------------------------------------------------------------------------------
"whenever I ask for visibility in console there are too much of text … let's do something
different like a block diagram or flow chart how agents communicated and resolved the issue."
So this module never returns a narrative. `graph(...)` returns NODES (signal, agent, person,
policy, squad, BOSS, proposal/action, decision, run, needs-you, outcome) with labels of at most
four words, EDGES (raised, claimed, proposed, critique, vote, handoff, escalate, decided, settles,
overrides, acted, asks, resulted; a squad adds assembled, seated, returned) with a dissent flag the console draws as a split arrow, and
item BADGES (problem type, outcome, Needs you, autonomy level, cost). Every node carries a `ref`
the console opens in a drawer — the evidence, the rationale, the thread, the receipts are one
click away and never on the canvas. Each node has a `stage` (0 signal → 5 outcome) so the
diagram reads left to right in five seconds.

The list is a list of small diagrams: each item carries a `strip` (≤ 6 nodes, signal → owner →
decision → outcome) for the thumbnail and the counts for the filter tabs
(Needs you · Running · Resolved by agents · Overridden · Failed).

The scorecard (design §11.7) is COMPUTED from the ledger and the board, never estimated.
"""
from __future__ import annotations

from statistics import median
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board, collab, ledger, policy
from . import task_classes as tc
from ._common import party_id, party_kind, serialize_row, short_label

FILTERS = ("needs_you", "running", "resolved_by_agents", "overridden", "failed", "dismissed",
           "all")
STAGES = {"signal": 0, "boss": 1, "agent": 1, "person": 1, "policy": 1, "squad": 1,
          "learning": 2, "action": 2, "decision": 3, "needs_you": 3, "run": 4, "outcome": 5}
MAX_SIGNAL_NODES = 5
_ACRONYMS = {"Sre": "SRE", "Qa": "QA", "Pm": "PM", "Cto": "CTO", "Coo": "COO", "Cao": "CAO",
             "Vp": "VP", "Aml": "AML", "Dq": "DQ"}
_RUN_WORDS = {"workflow_run": "Run", "code_task": "Build", "pull_request": "Pull request",
              "meeting": "Panel", "incident": "Incident"}
#: An incident that ends resolved has RECOVERED: the outcome node says so ("Recovered 4m12s").
INCIDENT_CLASSES = frozenset({"incident_response", "incident_staging_mitigation"})

_FILTER_SQL = {
    "needs_you": "w.lifecycle = 'needs_you'",
    "running": "w.lifecycle IN ('proposed', 'claimed', 'in_progress')",
    "resolved_by_agents": ("w.lifecycle = 'resolved' AND COALESCE(w.resolved_by, '') NOT LIKE "
                           "'person:%' AND NOT EXISTS (SELECT 1 FROM agent_act_decisions d "
                           "WHERE d.work_item_id = w.id AND d.overrides_act_id IS NOT NULL)"),
    "overridden": ("EXISTS (SELECT 1 FROM agent_act_decisions d WHERE d.work_item_id = w.id "
                   "AND d.overrides_act_id IS NOT NULL)"),
    "failed": "w.lifecycle = 'failed'",
    "dismissed": "w.lifecycle = 'dismissed'",
    "all": "true",
}


def _party_label(who: str) -> str:
    kind = party_kind(who)
    if kind == "boss":
        return "BOSS"
    ident = party_id(who)
    if kind == "agent":
        if ident == "chief_of_staff":
            return "Chief of Staff"
        words = ident.replace("_", " ").title().split()
        return short_label(" ".join(_ACRONYMS.get(w, w) for w in words), 3)
    if kind == "policy":
        return "Policy"
    if kind == "squad":
        return "Squad"
    return short_label(ident, 2)


def _minutes(start: Any, end: Any) -> Optional[float]:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 60, 1)


def _span_words(minutes: Optional[float]) -> str:
    if minutes is None:
        return ""
    if minutes < 1:
        return f"{int(minutes * 60)}s"
    if minutes < 10:
        whole = int(minutes)
        return f"{whole}m{int(round((minutes - whole) * 60)):02d}s"
    if minutes < 90:
        return f"{int(minutes)}m"
    if minutes < 48 * 60:
        return f"{int(minutes // 60)}h"
    return f"{int(minutes // 1440)}d"


def bucket(item: dict[str, Any], *, overridden: bool) -> str:
    """The one outcome badge an item wears (and the filter tab it sits under)."""
    life = item.get("lifecycle")
    if life == "needs_you":
        return "needs_you"
    if life in ("proposed", "claimed", "in_progress"):
        return "running"
    if overridden:
        return "overridden"
    if life == "resolved":
        return ("resolved_by_person" if str(item.get("resolved_by") or "").startswith("person:")
                else "resolved_by_agents")
    return str(life)


def _badges(item: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    klass = tc.TASK_CLASSES.get(str(item.get("task_class") or ""))
    overridden = any(d.get("overrides_act_id") for d in decisions)
    dissent = any((d.get("consensus") or {}).get("dissent") for d in decisions)
    return {"problem_type": klass.title if klass else item.get("task_class"),
            "task_class": item.get("task_class"), "outcome": bucket(item, overridden=overridden),
            "needs_you": item.get("lifecycle") == "needs_you",
            "autonomy_level": item.get("autonomy_level"), "severity": item.get("severity"),
            "cost_usd": round(int(item.get("cost_usd_micros") or 0) / 1_000_000, 4),
            "dissent": dissent, "overridden": overridden}


def graph(connection: Connection, item_id: str, *, project_id: str) -> dict[str, Any]:
    """One work item as {item, badges, nodes, edges}: the flow chart the console draws."""
    item = board.get(connection, item_id, project_id=project_id)
    if item is None:
        raise LookupError(f"no ambient work item {item_id}")
    links = board.links(connection, item_id)
    signal_ids = [l["ref"] for l in links if l["kind"] == "signal"]
    signals = [serialize_row(r) for r in connection.execute(text(
        "SELECT id, kind, title, severity, source, occurrences, last_seen_at FROM signals "
        "WHERE id = ANY(:ids) ORDER BY first_seen_at"), {"ids": signal_ids}).mappings()]
    board_msgs = collab.messages(connection, item_id)
    decisions = ledger.list_decisions(connection, project_id, work_item_id=item_id, limit=500)

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def node(node_id: str, kind: str, label: str, **extra: Any) -> str:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": kind, "label": short_label(label, 4),
                              "stage": STAGES[kind], "badges": [], **extra}
        return node_id

    # SQUAD SEATS: a handoff may name the seat its receiver takes on this item ("SRE lead",
    # "Investigator"). The seat is the node's label — what the person does HERE — and the
    # party's own role name rides along as `role` for the tooltip and the drawer.
    # A generic squad seat ("lead", "specialist", "critic" — squads.assemble / a lead handing
    # the lead on) is a badge, not a label: the node keeps the party's role name.
    seats: dict[str, str] = {}
    for m in board_msgs:
        seat = str((m.get("body") or {}).get("seat") or "") if m["kind"] == "handoff" else ""
        if seat and seat not in ("lead", "specialist", "critic"):
            seats.setdefault(str(m["body"].get("to")), seat)

    def actor(who: str) -> str:
        kind = party_kind(who)
        role = _party_label(who)
        extra: dict[str, Any] = {"role": role}
        if who in seats:
            extra["seat"] = seats[who]
        return node(f"party:{who}", kind if kind in STAGES else "boss", seats.get(who, role),
                    ref={"kind": "party", "id": who}, **extra)

    def edge(src: str, dst: str, kind: str, label: str = "", dissent: bool = False,
             ref: Optional[dict[str, Any]] = None) -> None:
        edges.append({"from": src, "to": dst, "kind": kind,
                      "label": short_label(label or kind, 3), "dissent": bool(dissent),
                      **({"ref": ref} if ref else {})})

    creator = str(item.get("created_by") or "boss")
    try:
        origin = actor(creator)
    except (ValueError, IndexError):
        origin = actor("boss")
    for s in signals[:MAX_SIGNAL_NODES]:
        sid = node(f"signal:{s['id']}", "signal", s["title"] or s["kind"],
                   ref={"kind": "signal", "id": s["id"]})
        nodes[sid]["badges"] = [s["severity"]] + (
            [f"×{s['occurrences']}"] if int(s["occurrences"] or 1) > 1 else [])
        edge(sid, origin, "raised", "raised")
    if len(signals) > MAX_SIGNAL_NODES:
        more = node("signal:more", "signal", f"+{len(signals) - MAX_SIGNAL_NODES} more signals",
                    ref={"kind": "signals", "work_item_id": item_id})
        edge(more, origin, "raised", "raised")

    findings: dict[str, int] = {}
    last_actor = origin
    for m in board_msgs:
        sender = m["sender"]
        body = m.get("body") or {}
        if m["kind"] == "squad":
            # The roster: one squad node (the same node a squad:<item> decision comes from),
            # an edge to each member labelled with its seat.
            from .squads import TEMPLATES

            template = TEMPLATES.get(str(body.get("template")))
            sq = node(f"party:squad:{item_id}", "squad",
                      template.title if template else "Squad",
                      ref={"kind": "message", "id": m["id"]})
            members = list(body.get("members") or ())
            nodes[sq]["badges"] = [f"{len(members)} agents"] + (
                [f"{len(body.get('unfilled') or ())} unfilled"] if body.get("unfilled") else [])
            edge(origin, sq, "assembled", "assembled", ref={"kind": "message", "id": m["id"]})
            for member in members:
                try:
                    who = actor(member["party"])
                except (ValueError, IndexError, KeyError):
                    continue
                if member.get("seat") and member["seat"] not in nodes[who]["badges"]:
                    nodes[who]["badges"].append(member["seat"])
                edge(sq, who, "seated", str(member.get("seat") or "seated"))
        elif m["kind"] == "claim":
            who = actor(sender)
            if who != origin:
                edge(origin, who, "claimed", "claimed", ref={"kind": "message", "id": m["id"]})
            last_actor = who
        elif m["kind"] in ("finding", "hypothesis"):
            if sender == "boss":
                continue    # a run's or panel's result: drawn as its "reported back" edge
            who = actor(sender)
            findings[who] = findings.get(who, 0) + 1
        elif m["kind"] == "proposal":
            act = tc.ACTIONS.get(str(body.get("action")))
            pid = node(f"proposal:{m['id']}", "action", act.title if act else "Proposal",
                       ref={"kind": "message", "id": m["id"]})
            nodes[pid]["badges"] = [body.get("risk") or "low"]
            edge(actor(sender), pid, "proposed", "proposed")
        elif m["kind"] in ("critique", "vote") and m.get("reply_to"):
            target = f"proposal:{m['reply_to']}"
            if target in nodes:
                said = body.get("position") if m["kind"] == "critique" else body.get("vote")
                edge(actor(sender), target, m["kind"], str(said or m["kind"]),
                     dissent=said in ("object", "unsafe", "reject"),
                     ref={"kind": "message", "id": m["id"]})
        elif m["kind"] == "handoff" and (body.get("via") or body.get("returned_from")):
            # Work handed to a run or a panel, and its result handed back: the run is a node
            # between the agents, so the diagram shows deliberate → execute → report back.
            ref = body.get("via") or body.get("returned_from")
            word = _RUN_WORDS.get(ref["kind"], "Run")
            rid = node(f"{ref['kind']}:{ref['ref']}", "run", word,
                       ref={"kind": ref["kind"], "id": ref["ref"]})
            try:
                if body.get("via"):
                    edge(actor(sender), rid, "handoff", "handed off",
                         ref={"kind": "message", "id": m["id"]})
                else:
                    nodes[rid]["badges"] = [str((body.get("packet") or {}).get("status") or "")]
                    edge(rid, actor(body.get("to")), "returned", "reported back",
                         ref={"kind": "message", "id": m["id"]})
            except (ValueError, IndexError):
                continue
        elif m["kind"] in ("handoff", "escalate"):
            src = body.get("from") or sender
            try:
                src_node = actor(src) if src else actor(sender)
                dst_node = actor(body.get("to"))
            except (ValueError, IndexError):
                continue
            edge(src_node, dst_node, m["kind"],
                 "handed off" if m["kind"] == "handoff" else "escalated",
                 ref={"kind": "message", "id": m["id"]})
            last_actor = dst_node
    for who, n in findings.items():
        nodes[who]["badges"].append(f"{n} finding{'s' if n > 1 else ''}")

    # APPLIED LEARNING (design §12.4): a lesson from a past incident the squad reused is a node
    # of its own — "INC-1 lesson" — with an arrow into the decision that applied it.
    lesson_rows = connection.execute(text(
        "SELECT n.id, n.name, n.attrs->>'incident_ref' AS ref, e.attrs->>'agent_role' AS role "
        "FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes n ON n.id = e.dst_id "
        "WHERE e.project_id = :p AND e.kind = 'reused' AND s.kind = 'incident' "
        "AND s.attrs->>'work_item_id' = :w ORDER BY e.recorded_at"),
        {"p": project_id, "w": item_id}).mappings().all()
    lesson_nodes: list[str] = []
    for r in lesson_rows:
        ref = str(r["ref"] or "")
        lid = node(f"learning:{r['id']}", "learning",
                   f"{ref} lesson" if ref.startswith("INC-") else "Past lesson",
                   ref={"kind": "kg_node", "id": r["id"]},
                   detail=f"Applied learning: {r['name']}" + (f" from {ref}" if ref else ""))
        nodes[lid]["badges"] = ["applied"]
        lesson_nodes.append(lid)

    last_decision: Optional[str] = None
    for d in decisions:
        # The chosen OPTION's label when the decider gave one ("Hand to SRE"), then the action's
        # catalogue title, never a bare key ("route:sre"): the diagram speaks words (§11.0).
        said = next((o.get("label") for o in d.get("options") or []
                     if isinstance(o, dict) and o.get("key") == d["chosen"] and o.get("label")),
                    None)
        chosen = tc.ACTIONS[d["chosen"]].title if d["chosen"] in tc.ACTIONS else d["chosen"]
        did = node(f"decision:{d['act_id']}", "decision", said or chosen,
                   ref={"kind": "act", "id": d["act_id"]})
        badges = [d.get("autonomy_level") or "", d["decider_kind"]]
        if (d.get("consensus") or {}).get("dissent"):
            badges.append("dissent")
        if d.get("outcome"):
            badges.append(d["outcome"])
        nodes[did]["badges"] = [b for b in badges if b]
        edge(actor(d["decided_by"]), did, "decided", "decided")
        if lesson_nodes and d["question"] == "Have we seen this before?":
            for lid in lesson_nodes:
                edge(lid, did, "applied", "applied")
        if d.get("proposal_id") and f"proposal:{d['proposal_id']}" in nodes:
            edge(f"proposal:{d['proposal_id']}", did, "settles", "settles")
        if d.get("overrides_act_id"):
            edge(did, f"decision:{d['overrides_act_id']}", "overrides", "overrode",
                 dissent=True)
        last_decision = did

    for lid in lesson_nodes:
        if not any(e["from"] == lid for e in edges):
            edge(lid, last_actor, "applied", "applied")
    anchor = last_decision or last_actor
    for l in links:
        if l["kind"] in _RUN_WORDS:
            rid = node(f"{l['kind']}:{l['ref']}", "run", _RUN_WORDS[l["kind"]],
                       ref={"kind": l["kind"], "id": l["ref"]})
            if not any(e["to"] == rid for e in edges):
                edge(anchor, rid, "acted",
                     "opened" if l["kind"] == "pull_request" else "started")
            anchor = rid
    if item["lifecycle"] == "needs_you":
        nid = node("needs_you", "needs_you", "Needs you",
                   ref={"kind": "decision", "id": item.get("needs_you_decision_id")})
        nodes[nid]["badges"] = ["needs_you"]
        # A squad's card asks about ONE proposal: the arrow comes from it, not from the last
        # thing that happened.
        asked = connection.execute(text(
            "SELECT recommendation->>'proposal_id' FROM decisions WHERE id = :d"),
            {"d": item.get("needs_you_decision_id")}).scalar()
        edge(f"proposal:{asked}" if asked and f"proposal:{asked}" in nodes else anchor, nid,
             "asks", "asks")
    if item["lifecycle"] in board.TERMINAL:
        took = _minutes(_dt(item.get("created_at")), _dt(item.get("resolved_at")))
        words = {"resolved": "Recovered" if item.get("task_class") in INCIDENT_CLASSES
                 else "Resolved", "failed": "Failed", "dismissed": "Dismissed"}
        oid = node("outcome", "outcome", f"{words[item['lifecycle']]} {_span_words(took)}".strip(),
                   ref={"kind": "work_item", "id": item_id})
        nodes[oid]["badges"] = [item["lifecycle"]]
        edge(anchor, oid, "resulted", "resulted")

    ordered = sorted(nodes.values(), key=lambda n: (n["stage"], list(nodes).index(n["id"])))
    return {"item": _item_summary(item), "badges": _badges(item, decisions),
            "nodes": ordered, "edges": edges}


def _dt(value: Any) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _item_summary(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "project_id", "title", "lifecycle", "task_class", "autonomy_level", "severity",
            "subject_ref", "app", "lease_owner", "lease_kind", "lease_expires_at",
            "needs_you_decision_id", "outcome", "resolved_by", "created_at", "updated_at",
            "resolved_at", "squad", "priority_band", "priority_score", "fingerprint")
    return {k: item.get(k) for k in keys}


def owner_of(connection: Connection, item: Mapping[str, Any]) -> Optional[str]:
    """Who owns the item now: the squad's lead, else the last party it was handed to, else the
    agent holding it, else whoever ended it."""
    from .squads import roster

    squad = roster(connection, item["id"])
    if squad and squad.get("lead"):
        return str(squad["lead"])
    to = connection.execute(text(
        "SELECT body->>'to' FROM work_item_messages WHERE work_item_id = :w AND kind = 'handoff' "
        "AND body->>'to' IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 1"),
        {"w": item["id"]}).scalar()
    if to:
        return str(to)
    if item.get("lease_owner"):
        return str(item["lease_owner"])
    return str(item["resolved_by"]) if item.get("resolved_by") else None


def _strip(g: dict[str, Any], owner: Optional[str] = None) -> list[dict[str, Any]]:
    """The thumbnail: the first signal, the OWNER, the last decision, the end."""
    nodes = g["nodes"]
    pick: list[dict[str, Any]] = []
    by_id = {n["id"]: n for n in nodes}
    owner_node = by_id.get(f"party:{owner}") if owner else None
    for kind in ("signal", "owner", "person", "decision", "needs_you", "outcome"):
        if kind == "owner":
            agents = [n for n in nodes if n["type"] == "agent"]
            chosen = owner_node or (agents[-1] if agents else None)
        else:
            found = [n for n in nodes if n["type"] == kind]
            chosen = (found[-1] if kind == "decision" else found[0]) if found else None
        if chosen is not None and chosen not in pick:
            pick.append(chosen)
    return [{"type": n["type"], "label": n["label"], "badges": n["badges"][:2]}
            for n in pick[:6]]


def list_view(connection: Connection, project_id: str, *, filter_: str = "all",
              task_class: Optional[str] = None, limit: int = 30,
              before: Optional[str] = None) -> dict[str, Any]:
    """Agent Operations' list: items under one filter tab, each with badges and a strip."""
    if filter_ not in FILTERS:
        raise ValueError(f"filter is one of {', '.join(FILTERS)}")
    rows = connection.execute(text(
        f"SELECT w.id FROM work_items w WHERE w.project_id = :p AND w.ambient AND "
        f"{_FILTER_SQL[filter_]} AND (CAST(:c AS TEXT) IS NULL OR w.task_class = :c) "
        "AND (CAST(:b AS TIMESTAMPTZ) IS NULL OR w.updated_at < :b) "
        "ORDER BY w.updated_at DESC, w.id DESC LIMIT :n"),
        {"p": project_id, "c": task_class, "b": before,
         "n": max(1, min(int(limit), 100))}).scalars().all()
    items = []
    for item_id in rows:
        g = graph(connection, item_id, project_id=project_id)
        owner = owner_of(connection, g["item"])
        from .. import stuck_work

        # What its build waits for, and what the Shepherd found (stuck_work): "Running" alone
        # hid four builds crash-looping behind a squad's item (2026-11-07).
        items.append({**g["item"], "badges": g["badges"], "owner": owner,
                      "strip": _strip(g, owner), **stuck_work.item_status(connection, item_id)})
    counts = {f: int(connection.execute(text(
        f"SELECT count(*) FROM work_items w WHERE w.project_id = :p AND w.ambient AND "
        f"{_FILTER_SQL[f]} AND (CAST(:c AS TEXT) IS NULL OR w.task_class = :c)"),
        {"p": project_id, "c": task_class}).scalar() or 0) for f in FILTERS}
    return {"filter": filter_, "items": items, "counts": counts,
            "next_before": items[-1]["updated_at"] if len(items) == max(1, min(int(limit), 100))
            else None}


def scorecard(connection: Connection, project_id: str, *, days: int = 7,
              task_class: Optional[str] = None) -> dict[str, Any]:
    """Design §11.7, computed: decisions/day by level and decider, accepted/overridden/reverted %
    per class, time to a human decision, MTTR, noise, cost against the envelope, guard trips.
    `outcomes` (BOSSAPP-912) adds design §8's success metrics — optionally for one task class —
    and the Recommended roadmap's acceptance per detector (BOSSAPP-911)."""
    from . import outcomes, roadmap

    days = max(1, min(int(days), 90))
    window = {"p": project_id, "d": days}
    per_day = [serialize_row(r) for r in connection.execute(text(
        "SELECT to_char(date_trunc('day', created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD') AS day, "
        "count(*) FILTER (WHERE autonomy_level = 'A0') AS a0, "
        "count(*) FILTER (WHERE autonomy_level = 'A1') AS a1, "
        "count(*) FILTER (WHERE autonomy_level = 'A2') AS a2, "
        "count(*) FILTER (WHERE autonomy_level = 'A3') AS a3, "
        "count(*) FILTER (WHERE decided_by LIKE 'agent:%') AS agent, "
        "count(*) FILTER (WHERE decided_by LIKE 'policy:%') AS policy, "
        "count(*) FILTER (WHERE decided_by LIKE 'squad:%') AS squad, "
        "count(*) FILTER (WHERE decided_by LIKE 'person:%') AS person, count(*) AS total "
        "FROM agent_act_decisions WHERE project_id = :p AND created_at > now() - "
        "make_interval(days => :d) GROUP BY 1 ORDER BY 1"), window).mappings()]
    by_class = []
    for r in connection.execute(text(
            "SELECT task_class, count(*) AS decided, "
            "count(*) FILTER (WHERE outcome IN ('accepted', 'edited', 'merged', 'succeeded')) "
            "AS accepted, count(*) FILTER (WHERE outcome = 'overridden') AS overridden, "
            "count(*) FILTER (WHERE outcome IN ('reverted', 'incident_caused')) AS reverted "
            "FROM agent_act_decisions WHERE project_id = :p AND decided_by NOT LIKE 'person:%' "
            "AND outcome IS NOT NULL AND created_at > now() - make_interval(days => :d) "
            "GROUP BY task_class ORDER BY task_class"), window).mappings():
        n = int(r["decided"]) or 1
        by_class.append({"task_class": r["task_class"], "decided": int(r["decided"]),
                         "accepted_pct": round(100 * int(r["accepted"]) / n, 1),
                         "overridden_pct": round(100 * int(r["overridden"]) / n, 1),
                         "reverted_pct": round(100 * int(r["reverted"]) / n, 1)})
    waits = [float(x) for x in connection.execute(text(
        "SELECT EXTRACT(EPOCH FROM (answered_at - needs_you_at)) / 60 FROM work_items WHERE "
        "project_id = :p AND ambient AND answered_at IS NOT NULL AND needs_you_at IS NOT NULL "
        "AND answered_at > now() - make_interval(days => :d)"), window).scalars()]
    repairs = connection.execute(text(
        "SELECT task_class, EXTRACT(EPOCH FROM (resolved_at - created_at)) / 60 AS minutes "
        "FROM work_items WHERE project_id = :p AND ambient AND lifecycle = 'resolved' "
        "AND resolved_at > now() - make_interval(days => :d)"), window).mappings().all()
    incident_classes = {"incident_response", "incident_staging_mitigation", "perf_regression"}
    ends = connection.execute(text(
        "SELECT lifecycle, count(*) AS n, COALESCE(sum(cost_usd_micros), 0) AS cost FROM "
        "work_items WHERE project_id = :p AND ambient AND resolved_at > now() - "
        "make_interval(days => :d) GROUP BY lifecycle"), window).mappings().all()
    ended = {r["lifecycle"]: int(r["n"]) for r in ends}
    total_ended = sum(ended.values())
    spent = int(connection.execute(text(
        "SELECT COALESCE(sum(cost_usd_micros), 0) FROM work_items WHERE project_id = :p AND "
        "ambient AND created_at > now() - make_interval(days => :d)"), window).scalar() or 0)
    guard_rows = connection.execute(text(
        "SELECT event_type, count(*) AS n FROM events WHERE project_id = :p AND event_type IN "
        "('ambient.guard_refused', 'ambient.breaker_tripped', 'ambient.envelope_refused') "
        "AND created_at > now() - make_interval(days => :d) GROUP BY event_type"),
        window).mappings().all()
    resolved = ended.get("resolved", 0)
    return {
        "project_id": project_id, "days": days,
        "decisions_per_day": per_day,
        "by_class": by_class,
        "time_to_human_decision_minutes": _summary(waits),
        "mttr_minutes": _summary([float(r["minutes"]) for r in repairs]),
        "incident_mttr_minutes": _summary([float(r["minutes"]) for r in repairs
                                           if r["task_class"] in incident_classes]),
        "items_ended": ended,
        "noise_rate": round(ended.get("dismissed", 0) / total_ended, 4) if total_ended else None,
        "cost": {"spent_usd": round(spent / 1e6, 4),
                 "per_resolved_item_usd": round(spent / 1e6 / resolved, 4) if resolved else None,
                 "envelope_today": policy.envelope(connection, project_id)},
        "guards": {r["event_type"].split(".", 1)[1]: int(r["n"]) for r in guard_rows},
        "stopped": policy.stop_state(connection, project_id),
        "outcomes": {**outcomes.metrics(connection, project_id, days=days,
                                        task_class=task_class),
                     "recommendations": roadmap.metrics(connection, project_id,
                                                        days=max(days, 30))},
    }


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "p90": None}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {"n": len(values), "median": round(median(ordered), 1), "p90": round(p90, 1)}
