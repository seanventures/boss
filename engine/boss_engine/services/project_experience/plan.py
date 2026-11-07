"""The Roadmap's PLAN (screen 07, contract §6): Now · Next · Later as lanes.

  Now    the work moving now (running, waiting on a person, stalled), from the Work list;
  Next   roadmap items a person accepted that no run has started yet, lane `next` (the default);
  Later  the same, put on lane `later`.

Lanes and order live on the accepted item itself (`ambient_recommendations.facts.lane / rank`),
so accepting a recommendation, accepting the onboarding agent's proposed backlog
(onboarding.accept_backlog) and moving a card all write the same two fields. Nothing here starts
a build: `start` on a card is THE start door (POST /v1/projects/{id}/work with
`source_ref: rec:<id>`), and that door records the run on the item so it leaves the lane.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import face, agents, project_href

LANES = ("next", "later")
NOW_STATES = ("running", "needs_you", "stalled")
#: The work kind a roadmap item starts as through the start door.
_START_KIND = {"hardening": "bug", "design_fix": "bug"}
#: What New work's "Add to Next" keeps on the item and its later Start sends again.
_CARRIED = ("workflow_key", "acceptance_criteria", "priority", "notify_channel")
#: Where a priority puts new work on Next: urgent and high go before normal and low.
_PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _norm(value: Any) -> str:
    from .titles import short_title

    return " ".join(short_title(value).lower().split()) if value else ""


def _repo(connection: Connection, project_id: str) -> Optional[str]:
    return connection.execute(text(
        "SELECT full_name FROM github_repository_bindings WHERE project_id = :p AND status = "
        "'active' ORDER BY (role = 'primary') DESC, created_at LIMIT 1"), {"p": project_id}).scalar()


def _planned(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Accepted, not-yet-started roadmap items in Marshal's order: Next by rank, then Later."""
    from .new_work import issue_of
    from .onboarding import readable
    from .titles import short_title

    repo = _repo(connection, project_id)

    rows = connection.execute(text(
        "SELECT work_item_id, title, why, kind, detector, facts, decided_at FROM "
        "ambient_recommendations WHERE project_id = :p AND status = 'accepted' AND "
        "COALESCE(artifact->>'workflow_run_id', '') = '' AND "
        "COALESCE(artifact->>'meeting_run_id', '') = '' ORDER BY "
        "(COALESCE(facts->>'lane', 'next') = 'later'), "
        "COALESCE((facts->>'rank')::int, 1000000), decided_at NULLS LAST LIMIT 60"),
        {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        facts = r["facts"] if isinstance(r["facts"], dict) else json.loads(r["facts"] or "{}")
        lane = facts.get("lane") if facts.get("lane") in LANES else "next"
        kind = str(facts.get("work_kind") or "") or _START_KIND.get(str(r["kind"]), "feature")
        if kind not in ("feature", "bug", "ticket"):
            kind = "ticket"
        item_id = str(r["work_item_id"])
        r = {**r, "title": readable(r["title"], r["why"])[0],
             "why": readable(r["title"], r["why"])[1]}
        issue = issue_of(facts.get("ref"), facts.get("external_url"), repo)
        out.append({
            "id": item_id, "label": short_title(r["title"], limit=5), "title": r["title"],
            "lane": lane, "rank": facts.get("rank"), "state": "planned",
            "source": "onboarding" if r["detector"] == "onboarding" else "roadmap",
            # Where the item came from (the onboarding agent's candidate source): ci | bug |
            # ticket | roadmap; the issue it names; the file a failing check points at.
            "origin": str(facts.get("source") or "roadmap"), "issue": issue,
            "where": facts.get("where"), "why": r["why"], "rec_kind": r["kind"],
            "decided_at": r["decided_at"], "work_kind": kind,
            "external_url": facts.get("external_url") or (issue or {}).get("external_url"),
            "href": project_href(project_id, tab="roadmap", drawer=f"rec:{item_id}"),
            "move": {"href": f"/v1/projects/{project_id}/plan/{item_id}", "method": "POST"},
            "start": {"href": f"/v1/projects/{project_id}/work", "method": "POST",
                      "body": {"kind": kind, "text": str(facts.get("text") or
                                                         f"{r['title']}. {r['why'] or ''}")[:4000],
                               "title": short_title(r["title"]),
                               "source_ref": f"rec:{item_id}",
                               **({"source_url": issue["external_url"]} if issue else {}),
                               # Work planned from the New work modal keeps what it was given.
                               **{k: facts[k] for k in _CARRIED if facts.get(k)}}}})
    return out


def lanes(connection: Connection, project_id: str,
          work_items: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """{now, next, later}: each a list of cards {id, label, state, agent, href, start?, move?}."""
    from . import work as work_service

    items = work_items if work_items is not None else work_service.everything(connection,
                                                                              project_id)
    now = []
    for w in items:
        if w.get("state") not in NOW_STATES:
            continue
        agent = (w.get("agents") or [None])[-1]
        now.append({"id": w["id"], "label": w["label"], "state": w["state"],
                    "agent": agent, "href": w.get("href")})
    # ONE LANE PER ITEM: a planned item whose work is already moving (a run with the same name,
    # started before the start door linked runs back) shows once, in Now.
    moving = {_norm(w.get("title") or w.get("label")) for w in items
              if w.get("state") in NOW_STATES} | {_norm(c["label"]) for c in now}
    planned = [p for p in _planned(connection, project_id)
               if _norm(p["title"]) not in moving and _norm(p["label"]) not in moving]
    roster = agents(connection, project_id)
    planner = face(roster, "product_manager") if "product_manager" in roster else None
    return {"now": now[:6],
            "next": [p for p in planned if p["lane"] == "next"],
            "later": [p for p in planned if p["lane"] == "later"],
            "planner": planner,
            "counts": {"now": len(now), "next": sum(1 for p in planned if p["lane"] == "next"),
                       "later": sum(1 for p in planned if p["lane"] == "later")}}


def move(connection: Connection, project_id: str, item_id: str, *, lane: str,
         before: Optional[str] = None, actor: str = "boss") -> dict[str, Any]:
    """Put an accepted item on `lane`, before `before` (another item on that lane) or at its end.
    Only accepted, not-yet-started items move; ranks on the lane are rewritten 1..n."""
    if lane not in LANES:
        raise ValueError("lane is next or later")
    found = connection.execute(text(
        "SELECT status, artifact FROM ambient_recommendations WHERE work_item_id = :w "
        "AND project_id = :p"), {"w": item_id, "p": project_id}).mappings().first()
    if not found or found["status"] != "accepted":
        raise LookupError("Only an accepted roadmap item can move between Next and Later.")
    current = [p["id"] for p in _planned(connection, project_id) if p["lane"] == lane
               and p["id"] != item_id]
    at = current.index(before) if before in current else len(current)
    order = current[:at] + [item_id] + current[at:]
    for rank, wid in enumerate(order, start=1):
        connection.execute(text(
            "UPDATE ambient_recommendations SET facts = facts || CAST(:f AS JSONB), "
            "updated_at = now() WHERE work_item_id = :w AND project_id = :p"),
            {"w": wid, "p": project_id,
             "f": json.dumps({"lane": lane, "rank": rank} if wid == item_id else {"rank": rank})})
    from ..ambient._common import emit

    emit(connection, project_id, "project.roadmap_moved", actor, "work_item", item_id,
         {"lane": lane, "position": at + 1})
    return lanes(connection, project_id)


def add(connection: Connection, project_id: str, *, kind: str, title: str, text_in: str,
        actor: str, priority: str = "normal", workflow_key: Optional[str] = None,
        acceptance_criteria: Optional[list[str]] = None, notify_channel: Optional[str] = None,
        source_url: Optional[str] = None, why: str = "") -> dict[str, Any]:
    """NEW WORK › ADD TO NEXT: the work goes on the Roadmap's Next lane as an accepted item —
    planned work on the Work tab, a card in Roadmap › Next — and nothing starts. Its rank is by
    priority (urgent and high before normal and low; Marshal re-ranks from there). Its Start
    later sends what it was given here (kind, words, workflow, criteria, priority, who to tell).
    Returns {item_id, lane, position, href, plan}."""
    import hashlib
    import uuid

    from .titles import shorten

    title = " ".join((title or "").split())[:200] or shorten(text_in, 6) or "New work"
    priority = priority if priority in _PRIORITY_RANK else "normal"
    item_id = f"wi_{uuid.uuid4().hex[:16]}"
    fingerprint = f"new_work:{hashlib.sha1((item_id + title).encode()).hexdigest()[:16]}"
    connection.execute(text(
        "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, description, "
        "ambient, fingerprint, lifecycle, task_class, source, created_by, outcome, "
        "resolved_by, resolved_at) VALUES (:i, :p, 'roadmap', :s, :t, :d, true, :f, "
        "'resolved', 'roadmap_item', 'new_work', :a, 'planned from New work', :a, now())"),
        {"i": item_id, "p": project_id, "s": fingerprint, "t": title, "d": text_in[:4000],
         "f": fingerprint, "a": actor})
    facts = {"lane": "next", "rank": 1_000_000, "source": "new_work", "work_kind": kind,
             "text": text_in[:8000], "priority": priority,
             **({"workflow_key": workflow_key} if workflow_key else {}),
             **({"acceptance_criteria": list(acceptance_criteria)[:12]}
                if acceptance_criteria else {}),
             **({"notify_channel": notify_channel} if notify_channel else {}),
             **({"external_url": source_url, "ref": source_url} if source_url else {})}
    rec_kind = "hardening" if kind == "bug" else "feature"
    connection.execute(text(
        "INSERT INTO ambient_recommendations (work_item_id, project_id, detector, kind, "
        "fingerprint, subject, title, why, proposed_shape, facts, score, priority_score, "
        "status, accepted_as, decided_by, decided_at) VALUES (:w, :p, 'new_work', :k, :f, "
        ":sub, :t, :y, 'ticket', CAST(:facts AS JSONB), :sc, :sc, 'accepted', 'ticket', :a, "
        "now())"),
        {"w": item_id, "p": project_id, "k": rec_kind, "f": fingerprint, "sub": f"new_work:{kind}",
         "t": title, "y": (why or text_in)[:600], "facts": json.dumps(facts, default=str),
         "sc": float(4 - _PRIORITY_RANK[priority]) / 4.0, "a": actor})
    # Before the first item of a lower priority on Next, else at its end.
    current = [p for p in _planned(connection, project_id) if p["lane"] == "next"
               and p["id"] != item_id]
    prio = {r["work_item_id"]: str((r["facts"] if isinstance(r["facts"], dict) else
                                    json.loads(r["facts"] or "{}")).get("priority") or "normal")
            for r in connection.execute(text(
                "SELECT work_item_id, facts FROM ambient_recommendations WHERE project_id = :p "
                "AND work_item_id = ANY(:ids)"),
                {"p": project_id, "ids": [c["id"] for c in current] or [""]}).mappings()}
    before = next((c["id"] for c in current
                   if _PRIORITY_RANK.get(prio.get(c["id"], "normal"), 2)
                   > _PRIORITY_RANK[priority]), None)
    lanes_now = move(connection, project_id, item_id, lane="next", before=before, actor=actor)
    from ..ambient._common import emit

    emit(connection, project_id, "project.work_planned", actor, "work_item", item_id,
         {"lane": "next", "priority": priority, "kind": kind})
    position = next((i for i, c in enumerate(lanes_now.get("next") or [], start=1)
                     if c["id"] == item_id), None)
    return {"item_id": item_id, "lane": "next", "position": position,
            "href": project_href(project_id, tab="roadmap"), "plan": lanes_now}


def link_started(connection: Connection, project_id: str, source_ref: Optional[str],
                 run_id: Optional[str]) -> bool:
    """THE START DOOR started work from a roadmap item (`source_ref: rec:<id>`): record the run on
    the item, so it leaves Next/Later and shows as running work instead."""
    ref = str(source_ref or "")
    if not ref.startswith("rec:") or not run_id:
        return False
    changed = connection.execute(text(
        "UPDATE ambient_recommendations SET artifact = artifact || CAST(:a AS JSONB), "
        "updated_at = now() WHERE work_item_id = :w AND project_id = :p AND status = 'accepted'"),
        {"w": ref.split(":", 1)[1], "p": project_id,
         "a": json.dumps({"workflow_run_id": run_id})}).rowcount
    return bool(changed)


__all__ = ["LANES", "add", "lanes", "link_started", "move"]
