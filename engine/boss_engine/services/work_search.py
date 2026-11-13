"""ONE SEARCH OVER WORK (BOSSAPP-881): runs, builds, decisions, PRD panels, artifacts, work items
and what agents wrote into the knowledge graph — "the PRD that mentioned Y" in one box.

BOSS already had good engines, each behind its own door: ⌘K jump finds things by NAME (runs,
PRs, builds, deploys, agents, documents), the knowledge graph finds what agents and people
WROTE (knowledge/find.find_written: ids, phrases, ranked words), the activity log finds lines.
Nothing searched the CONTENT of work in one place: a decision's question and summary, a PRD's
markdown, a build's instructions and summary, an artifact's content.

WHAT IT DOES. One query, fanned out per source, each source within ONE project and only the
sources the caller may read (the route passes ``kinds`` from the caller's permissions:
workflow_run:read, code_task:read, decision:read, meeting:read, artifact:read, work_item:read,
knowledge:read). A hit in a title ranks before a hit in the body; inside each, newest first.
Every hit carries a snippet around the match and an ``href`` into the console.

WHAT IT DOES NOT (kept honest). It is text matching (ILIKE on the phrase, then every word), not
meaning: no vectors here. The knowledge source reuses find_written's ranking as is. No new index
is added: each source reads at most ``PER_SOURCE`` rows of one project, newest first, so a very
large project may miss an old hit that only matches in its body.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Every source, and the permission a caller needs to see it.
SOURCES: dict[str, str] = {
    "run": "workflow_run:read",
    "code_task": "code_task:read",
    "decision": "decision:read",
    "meeting": "meeting:read",
    "artifact": "artifact:read",
    "work_item": "work_item:read",
    "knowledge": "knowledge:read",
}
PER_SOURCE = 8
MAX_LIMIT = 60
MIN_QUERY = 2
_SNIPPET = 160


def _like(term: str) -> str:
    return "%" + re.sub(r"([%_\\])", r"\\\1", term) + "%"


def _words(q: str) -> list[str]:
    return [w for w in re.findall(r"[\w#.-]{2,}", q.lower())][:6]


def snippet(body: Any, q: str, width: int = _SNIPPET) -> str:
    """The text around the first match of the phrase (else of any word), on one line."""
    flat = re.sub(r"\s+", " ", str(body or "")).strip()
    if not flat:
        return ""
    low = flat.lower()
    at = low.find(q.lower())
    if at < 0:
        at = min((low.find(w) for w in _words(q) if low.find(w) >= 0), default=0)
    start = max(0, at - width // 3)
    out = flat[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


#: Per source: SQL returning id, title, body, status, created_at for one project; the search
#: matches the phrase, or every word, in the title and body together.
_QUERIES: dict[str, str] = {
    "run": (
        "SELECT r.id, COALESCE(r.input->>'title', d.name, r.id) AS title, "
        "COALESCE(r.input->>'directive', '') || ' ' || COALESCE(r.blocked_reason, '') AS body, "
        "r.status, r.created_at FROM workflow_runs r "
        "JOIN workflow_definitions d ON d.id = r.workflow_definition_id WHERE r.project_id = :p"),
    "code_task": (
        "SELECT c.id, c.title, COALESCE(c.spec->>'instructions', '') || ' ' || "
        "COALESCE(c.state->>'summary', '') || ' ' || COALESCE(c.pull_request_url, '') || ' ' || "
        "COALESCE(c.error, '') AS body, c.status, c.created_at FROM code_task_runs c "
        "WHERE c.project_id = :p"),
    "decision": (
        "SELECT x.id, x.question AS title, COALESCE(x.recommendation->>'summary', '') || ' ' || "
        "COALESCE(x.resolution_note, '') AS body, x.status, x.created_at FROM decisions x "
        "WHERE x.project_id = :p"),
    "meeting": (
        "SELECT m.id, COALESCE(m.title, left(m.directive, 200)) AS title, "
        "COALESCE(m.directive, '') || ' ' || COALESCE(m.state->>'prd_markdown', '') || ' ' || "
        "COALESCE(m.state->>'synthesis', '') AS body, m.status, m.created_at FROM meeting_runs m "
        "WHERE m.project_id = :p"),
    "artifact": (
        # A restricted or local-only artifact is found by its title only: its content is never
        # matched, nor shown in a snippet.
        "SELECT a.id, a.title, CASE WHEN a.data_boundary IN ('restricted', 'local-only') "
        "THEN '' ELSE left(COALESCE(a.content::text, ''), 20000) || ' ' || "
        "COALESCE(a.uri, '') END AS body, a.artifact_type AS status, a.created_at FROM artifacts a "
        "WHERE a.project_id = :p"),
    "work_item": (
        "SELECT w.id, COALESCE(w.human_key || ' · ', '') || w.title AS title, "
        "COALESCE(w.description, '') AS body, w.status, w.created_at FROM work_items w "
        "WHERE w.project_id = :p AND NOT COALESCE(w.ambient, false)"),
}


def _href(kind: str, project_id: str, row_id: str) -> str:
    from .project_experience.common import href, project_href

    return {
        "run": lambda: project_href(project_id, tab="work", drawer=f"run:{row_id}"),
        "code_task": lambda: href(project_id, area="code_tasks", view="apps", build=row_id),
        "decision": lambda: href(project_id, area="decisions", decision=row_id),
        "meeting": lambda: href(project_id, area="meetings", meeting=row_id),
        "artifact": lambda: href(project_id, area="work", artifact=row_id),
        "work_item": lambda: project_href(project_id, tab="work"),
        "knowledge": lambda: href(project_id, area="knowledge", node=row_id),
    }[kind]()


def _source(connection: Connection, kind: str, project_id: str, q: str) -> list[dict[str, Any]]:
    base = _QUERIES[kind]
    words = _words(q) or [q.lower()]
    params: dict[str, Any] = {"p": project_id, "phrase": _like(q), "n": PER_SOURCE}
    every = []
    for i, w in enumerate(words):
        params[f"w{i}"] = _like(w)
        every.append(f"q.hay ILIKE :w{i}")
    sql = (f"SELECT q.*, (q.title ILIKE :phrase) AS in_title FROM ("
           f"SELECT s.*, COALESCE(s.title, '') || ' ' || COALESCE(s.body, '') AS hay "
           f"FROM ({base}) s) q WHERE q.hay ILIKE :phrase OR ({' AND '.join(every)}) "
           f"ORDER BY (q.title ILIKE :phrase) DESC, q.created_at DESC NULLS LAST LIMIT :n")
    try:
        with connection.begin_nested():
            rows = connection.execute(text(sql), params).mappings().all()
    except Exception:  # noqa: BLE001 — a table an install lacks is a source with no hits
        return []
    return [{"kind": kind, "id": str(r["id"]), "title": str(r["title"] or r["id"])[:200],
             "snippet": snippet(r["body"], q), "status": r["status"],
             "created_at": _iso(r["created_at"]), "match": "title" if r["in_title"] else "body",
             "href": _href(kind, project_id, str(r["id"]))} for r in rows]


def _knowledge(connection: Connection, project_id: str, q: str) -> list[dict[str, Any]]:
    from .knowledge.find import find_written

    try:
        with connection.begin_nested():
            found = find_written(connection, project_id, q, limit=PER_SOURCE)
            if not found:
                return []
            rows = {str(r["id"]): r for r in connection.execute(text(
                "SELECT id, kind, name, body, attrs, recorded_at AS created_at FROM kg_nodes WHERE id = ANY(:ids)"),
                {"ids": [f["id"] for f in found]}).mappings()}
    except Exception:  # noqa: BLE001 — no graph on this install: no knowledge hits
        return []
    out = []
    for f in found:
        r = rows.get(str(f["id"]))
        if r is None:
            continue
        attrs = r["attrs"] if isinstance(r["attrs"], dict) else json.loads(r["attrs"] or "{}")
        out.append({"kind": "knowledge", "id": str(r["id"]), "title": str(r["name"])[:200],
                    "snippet": snippet((r["body"] or "") + " " + json.dumps(
                        {k: v for k, v in attrs.items() if k != "provenance"}, default=str), q),
                    "status": r["kind"], "created_at": _iso(r.get("created_at")),
                    "match": "title" if q.lower() in str(r["name"]).lower() else "body",
                    "href": _href("knowledge", project_id, str(r["id"]))})
    return out


def search(connection: Connection, *, project_id: str, q: str,
           kinds: Optional[Iterable[str]] = None, limit: int = 30) -> dict[str, Any]:
    """Hits across the sources the caller may read, title matches first, then newest."""
    q = re.sub(r"\s+", " ", str(q or "")).strip()[:200]
    wanted = [k for k in SOURCES if kinds is None or k in set(kinds)]
    if len(q) < MIN_QUERY:
        return {"q": q, "items": [], "counts": {}, "searched": wanted,
                "note": f"type at least {MIN_QUERY} characters"}
    items: list[dict[str, Any]] = []
    for kind in wanted:
        items.extend(_knowledge(connection, project_id, q) if kind == "knowledge"
                     else _source(connection, kind, project_id, q))
    items.sort(key=lambda h: (h["match"] != "title", _neg(h.get("created_at"))))
    counts: dict[str, int] = {}
    for h in items:
        counts[h["kind"]] = counts.get(h["kind"], 0) + 1
    return {"q": q, "items": items[:max(1, min(int(limit), MAX_LIMIT))], "counts": counts,
            "searched": wanted}


def _neg(value: Any) -> str:
    """Newest first under a stable string sort: invert each digit of an ISO time."""
    s = str(value or "")
    return "".join(chr(ord("9") - (ord(ch) - ord("0"))) if ch.isdigit() else ch for ch in s) or "~"


__all__ = ["SOURCES", "search", "snippet"]
