"""The meeting-room records the console's Panels screens read (GET /v1/meeting-rooms[/{id}]).

One builder for the list, a page of it, one meeting by id and the meeting held for a task, so
every door shows the same record. Each record carries, besides the rounds and the PRD:

* ``brief`` — the operator's own words, and ``context`` — what BOSS added to the prompt, as
  readable markdown (meeting_brief.split_brief). Prompt scaffolding never reaches a title, a
  card or a hero line.
* ``meeting_type`` — prd_panel | go_no_go | standup, so each renders its own stages and result;
  ``verdict``, ``dissent`` and ``action_items`` for the typed fixtures.
* ``decision_id`` and ``build`` — where the PRD stands past the panel: awaiting approval,
  approved, published, building.
* ``error`` — why a failed panel stopped.

ONE MEETING, ONE RECORD. A typed fixture writes a meeting_runs row AND a meetings row; the
meetings row is shown only when no run represents it (by its meeting_id or its task).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text

from .meeting_brief import panel_title, split_brief
from .meeting_gist import room_gists
from .meeting_service import (
    FACILITATOR_TITLE, load_role_map, meeting_title, meeting_type_of, normalize_meeting_type,
    prd_state_of, resolve_roles,
)

_TYPED_PREFIX = {"go/no-go": "go_no_go", "go_no_go": "go_no_go", "standup": "standup",
                 "stand-up": "standup"}


def _j(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _pr_for_task(connection, tid):
    if not tid:
        return None, None
    pub = connection.execute(
        text("SELECT content FROM artifacts WHERE task_id = :t AND artifact_type = 'published_document' "
             "ORDER BY created_at DESC LIMIT 1"), {"t": tid},
    ).mappings().first()
    if not pub:
        return None, None
    pc = _j(pub["content"]) or {}
    return pc.get("pull_request_url"), pc.get("pull_request_number")


def _task_info(connection, tid):
    if not tid:
        return None, None
    tr = connection.execute(text("SELECT title, status FROM tasks WHERE id = :t"), {"t": tid}).mappings().first()
    return (tr["title"], tr["status"]) if tr else (None, None)


def _build_for_task(connection, tid) -> Optional[dict]:
    """The build the PRD fed, when one started: what "Build" means on the panel's stepper."""
    if not tid:
        return None
    row = connection.execute(
        text("SELECT id, status, phase, pull_request_url FROM code_task_runs WHERE task_id = :t "
             "ORDER BY created_at DESC LIMIT 1"), {"t": tid},
    ).mappings().first()
    return dict(row) if row else None


def _type_from_title(title: str) -> Optional[str]:
    head = str(title or "").split(":", 1)[0].strip().lower()
    return _TYPED_PREFIX.get(head)


def typed_result(meeting_type: str, state: dict) -> dict:
    """What a go/no-go or a stand-up decided, as data: the verdict, the dissent, the actions."""
    out: dict = {"verdict": None, "dissent": [], "action_items": []}
    if meeting_type == "prd_panel":
        return out
    for d in state.get("dissent_records") or []:
        if isinstance(d, dict):
            out["dissent"].append({"role": d.get("role"), "position": d.get("position"),
                                   "reason": d.get("reason")})
    for a in state.get("action_items") or []:
        if isinstance(a, dict) and a.get("title"):
            out["action_items"].append({"title": a.get("title"), "description": a.get("description")})
    if meeting_type == "go_no_go":
        for item in state.get("decision_items") or []:
            v = str(((item or {}).get("recommendation") or {}).get("verdict") or "").lower()
            if v in ("go", "no_go", "no-go", "nogo"):
                out["verdict"] = "go" if v == "go" else "no_go"
        if not out["verdict"]:
            said = str(state.get("synthesis") or "") + "\n" + str(state.get("prd_markdown") or "")
            m = re.search(r"\b(NO[- ]?GO|GO)\b", said)
            if m:
                out["verdict"] = "go" if m.group(1) == "GO" else "no_go"
    return out


def _title(raw_title: Optional[str], directive: Optional[str], prefix: bool) -> str:
    """The record's title: a dossier-promoted panel renamed for its outcome, else meeting_title."""
    better = panel_title(raw_title, directive)
    if better:
        return f"PRD meeting: {better}" if prefix else better
    name = meeting_title(raw_title, directive)
    return f"PRD meeting: {name}" if prefix and not name.startswith("PRD meeting:") else name


def meeting_room_items(connection, project_id: str, *, limit: int = 25, offset: int = 0,
                       only_id: Optional[str] = None, task_id: Optional[str] = None) -> list[dict]:
    """The records, newest first; or the one named `only_id`; or those held for `task_id`."""
    limit = max(1, min(int(limit or 25), 100))
    offset = max(0, int(offset or 0))
    items: list[dict] = []
    covered_tasks: set = set()
    covered_meetings: set = set()
    runs = connection.execute(
        text("""SELECT id, task_id, status, phase, directive, url, role_keys, constraints, state, usage,
                       created_at, updated_at, title, decision_id, meeting_id, error
                FROM meeting_runs WHERE project_id = :p
                  AND (CAST(:only AS text) IS NULL OR id = :only)
                  AND (CAST(:task AS text) IS NULL OR task_id = :task)
                ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"""),
        {"p": project_id, "only": only_id, "task": task_id, "limit": limit, "offset": offset},
    ).mappings().all()
    role_map = load_role_map(connection, project_id) if runs else {}
    for r in runs:
        state = _j(r["state"]) or {}
        usage = _j(r["usage"]) or {}
        constraints = _j(r["constraints"]) or {}
        personas = resolve_roles(_j(r["role_keys"]) or [], role_map)
        if r["task_id"]:
            covered_tasks.add(r["task_id"])
        if r["meeting_id"]:
            covered_meetings.add(r["meeting_id"])
        try:
            mtype = normalize_meeting_type(state.get("meeting_type")) if state.get("meeting_type") \
                else meeting_type_of(constraints)
        except ValueError:
            mtype = meeting_type_of(constraints)
        pr_url, pr_num = _pr_for_task(connection, r["task_id"])
        t_title, t_status = _task_info(connection, r["task_id"])
        research = state.get("research") or {}
        split = split_brief(r["directive"], constraints.get("brief"))
        items.append({
            "id": r["id"], "task_id": r["task_id"], "task_title": t_title, "task_status": t_status,
            "role_keys": [p["key"] for p in personas],
            "title": _title(r["title"], r["directive"], prefix=True),  # via meeting_title
            "purpose": "Cross-functional PRD review and drafting",
            "meeting_type": mtype,
            "status": r["status"], "phase": r["phase"], "error": r["error"],
            **prd_state_of(connection, run_status=r["status"], decision_id=r["decision_id"],
                           task_id=r["task_id"], pull_request_url=pr_url),
            "decision_id": r["decision_id"],
            # A vote is recorded against the meeting's minutes, which a run writes when it ends.
            "can_vote": bool(r["meeting_id"]),
            "participants": [p["title"] for p in personas] + [FACILITATOR_TITLE],
            "created_at": _iso(r["created_at"]),
            "completed_at": _iso(r["updated_at"]) if r["status"] in ("completed", "simulated") else None,
            "directive": r["directive"], "brief": split["brief"], "context": split["context"],
            "url": r["url"],
            "model": usage.get("model"), "cost_usd_micros": usage.get("cost_usd_micros"),
            "research_summary": {"sitemap": research.get("sitemap", []),
                                 "pages_studied": len(research.get("pages", []))},
            "round1": state.get("round1") or {}, "round2": state.get("round2") or {},
            # Each turn's headline and key points: the seat's own when it wrote one, else cut
            # deterministically from the reply (meeting_gist).
            **room_gists(state.get("round1"), state.get("round2"), state.get("synthesis"),
                         state.get("round1_gists"), state.get("round2_gists")),
            "synthesis": state.get("synthesis"), "prd_markdown": state.get("prd_markdown"),
            "pull_request_url": pr_url, "pull_request_number": pr_num,
            "build": _build_for_task(connection, r["task_id"]),
            **typed_result(mtype, state),
        })
    if only_id and items:
        return items
    if task_id and items:
        return items

    # Legacy meetings rows — only those no run already represents.
    rows = connection.execute(
        text("""SELECT id, title, purpose, status, participants, minutes_artifact_id, completed_at, created_at
                FROM meetings WHERE project_id = :p AND (CAST(:only AS text) IS NULL OR id = :only)
                ORDER BY created_at DESC LIMIT :limit OFFSET :offset"""),
        {"p": project_id, "only": only_id, "limit": limit, "offset": offset},
    ).mappings().all()
    if rows and not only_id:
        # A run anywhere in the project may represent the meeting, not only one on this page.
        covered_meetings |= {x["meeting_id"] for x in connection.execute(
            text("SELECT meeting_id FROM meeting_runs WHERE project_id = :p AND meeting_id = ANY(:ids)"),
            {"p": project_id, "ids": [m["id"] for m in rows]},
        ).mappings().all()}
    for m in rows:
        if m["id"] in covered_meetings and not only_id:
            continue
        content: dict = {}
        tid = None
        if m["minutes_artifact_id"]:
            art = connection.execute(text("SELECT task_id, content FROM artifacts WHERE id = :a"),
                                     {"a": m["minutes_artifact_id"]}).mappings().first()
            if art:
                tid = art["task_id"]
                content = _j(art["content"]) or {}
        if task_id and tid != task_id:
            continue
        if tid and tid in covered_tasks and not only_id:
            continue
        task_title, task_status = _task_info(connection, tid)
        pr_url, pr_num = _pr_for_task(connection, tid)
        mtype = content.get("meeting_type") or _type_from_title(m["title"]) or "prd_panel"
        split = split_brief(content.get("directive"), None)
        items.append({
            "id": m["id"], "task_id": tid, "task_title": task_title, "task_status": task_status,
            "role_keys": content.get("role_keys") or [],
            "title": _title(m["title"], content.get("directive"), prefix=False),
            "purpose": m["purpose"], "meeting_type": mtype,
            "status": m["status"], "error": None,
            **prd_state_of(connection, run_status=m["status"], decision_id=None, task_id=tid,
                           pull_request_url=pr_url),
            "decision_id": None, "can_vote": True,
            "phase": "done",
            "participants": m["participants"] or [],
            "created_at": _iso(m["created_at"]), "completed_at": _iso(m["completed_at"]),
            "directive": content.get("directive"), "brief": split["brief"], "context": split["context"],
            "url": content.get("url"),
            "model": content.get("model"), "cost_usd_micros": content.get("cost_usd_micros"),
            "research_summary": content.get("research_summary") or {},
            "round1": content.get("round1_perspectives") or {},
            "round2": content.get("round2_debate") or {},
            **room_gists(content.get("round1_perspectives"), content.get("round2_debate"),
                         content.get("synthesis"), content.get("round1_gists"),
                         content.get("round2_gists")),
            "synthesis": content.get("synthesis"), "prd_markdown": content.get("prd_markdown"),
            "pull_request_url": pr_url, "pull_request_number": pr_num,
            "build": _build_for_task(connection, tid),
            **typed_result(mtype, content),
        })
    return items
