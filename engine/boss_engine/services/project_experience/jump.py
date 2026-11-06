"""⌘K jump (contract §9): one keystroke to any run, PR, build, deploy, agent, doc or project.

Searches only the projects the caller may read (the route passes them). Within a project:
workflow runs and builds by title, pull requests by title or number, deploys by environment or
commit, agents by name or callsign, PRDs by title. With no query it returns the project's
newest things, so the palette opens on something useful.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import href, pr_number, project_href, words

LIMIT = 20


def _like(q: str) -> str:
    return "%" + re.sub(r"([%_\\])", r"\\\1", q) + "%"


def search(connection: Connection, *, q: str, project_ids: list[str],
           project_id: Optional[str] = None, limit: int = LIMIT) -> list[dict[str, Any]]:
    q = str(q or "").strip()[:100]
    number = int(q.lstrip("#")) if re.fullmatch(r"#?\d{1,7}", q) else None
    like = _like(q.lstrip("#")) if q else "%"
    scope = [project_id] if project_id else project_ids
    out: list[dict[str, Any]] = []
    names = {str(r["id"]): r["name"] for r in connection.execute(text(
        "SELECT id, name FROM projects WHERE id = ANY(:ids)"), {"ids": project_ids}).mappings()}

    # Projects (always across every readable project: ⌘K "go to project").
    for pid, name in names.items():
        if not q or q.lower() in name.lower() or q.lower() in pid.lower():
            out.append({"label": words(name), "kind": "project", "sub": pid,
                        "href": project_href(pid), "project_id": pid})
    if not scope:
        return out[:limit]

    if number is not None:
        prs = connection.execute(text(
            "SELECT id, project_id, title, pull_request_url, pull_request_number FROM code_task_runs "
            "WHERE project_id = ANY(:ids) AND COALESCE(pull_request_url, '') <> '' AND "
            "(pull_request_number = :n OR pull_request_url LIKE :tail) "
            "ORDER BY created_at DESC LIMIT 5"),
            {"ids": scope, "n": number, "tail": f"%/pull/{number}"}).mappings().all()
    else:
        prs = connection.execute(text(
            "SELECT id, project_id, title, pull_request_url, pull_request_number FROM code_task_runs "
            "WHERE project_id = ANY(:ids) AND COALESCE(pull_request_url, '') <> '' AND "
            "COALESCE(title, '') ILIKE :q ORDER BY created_at DESC LIMIT 5"),
            {"ids": scope, "q": like}).mappings().all()
    for r in prs:
        n = r["pull_request_number"] or pr_number(r["pull_request_url"])
        out.append({"label": words(f"PR #{n} {r['title'] or ''}", 5), "kind": "pr",
                    "sub": names.get(str(r["project_id"]), r["project_id"]),
                    "external_url": r["pull_request_url"],
                    "href": project_href(str(r["project_id"]), tab="work",
                                         drawer=f"build:{r['id']}"),
                    "project_id": r["project_id"]})

    for r in connection.execute(text(
            "SELECT r.id, r.project_id, r.status, COALESCE(r.input->>'title', d.name) AS title "
            "FROM workflow_runs r JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
            "WHERE r.project_id = ANY(:ids) AND d.namespace = 'product' AND "
            "COALESCE(r.mode, 'normal') <> 'test' AND (COALESCE(r.input->>'title', '') ILIKE :q "
            "OR d.name ILIKE :q OR r.id = :exact) ORDER BY r.created_at DESC LIMIT 6"),
            {"ids": scope, "q": like, "exact": q}).mappings():
        out.append({"label": words(r["title"], 5), "kind": "run", "sub": r["status"],
                    "href": project_href(str(r["project_id"]), tab="work", drawer=f"run:{r['id']}"),
                    "full_href": href(str(r["project_id"]), area="delivery_workflows", wfr=r["id"]),
                    "project_id": r["project_id"]})

    for r in connection.execute(text(
            "SELECT id, project_id, status, title FROM code_task_runs WHERE project_id = ANY(:ids) "
            "AND COALESCE(finalize_ctx->>'workflow_run_id', '') = '' AND "
            "(COALESCE(title, '') ILIKE :q OR id = :exact) ORDER BY created_at DESC LIMIT 4"),
            {"ids": scope, "q": like, "exact": q}).mappings():
        out.append({"label": words(r["title"] or "Build", 5), "kind": "build", "sub": r["status"],
                    "href": href(str(r["project_id"]), area="code_tasks", view="apps",
                                 build=r["id"]), "project_id": r["project_id"]})

    for r in connection.execute(text(
            "SELECT s.id, s.project_id, s.commit_sha, s.status, t.environment FROM stack_deployments s "
            "JOIN deployment_targets t ON t.id = s.target_id WHERE s.project_id = ANY(:ids) AND "
            "(t.environment ILIKE :q OR COALESCE(s.commit_sha, '') ILIKE :prefix OR :blank) "
            "ORDER BY s.created_at DESC LIMIT 4"),
            {"ids": scope, "q": like, "prefix": (q + "%") if q else "%", "blank": not q}).mappings():
        out.append({"label": words(f"Deploy {r['environment']} {(r['commit_sha'] or '')[:7]}"),
                    "kind": "deploy", "sub": r["status"],
                    "href": href(str(r["project_id"]), area="infra", view="deploys",
                                 deploy=r["id"]), "project_id": r["project_id"]})

    for r in connection.execute(text(
            "SELECT project_id, role_key, display_name, spec->'persona'->>'callsign' AS callsign "
            "FROM roles WHERE project_id = ANY(:ids) AND (display_name ILIKE :q OR "
            "COALESCE(spec->'persona'->>'callsign', '') ILIKE :q OR role_key ILIKE :q) "
            "AND :searching ORDER BY (spec->'persona'->>'callsign') IS NULL, display_name LIMIT 5"),
            {"ids": scope, "q": like, "searching": bool(q)}).mappings():
        name = r["callsign"] or r["display_name"]
        out.append({"label": words(f"Ask {name}" if r["callsign"] else name), "kind": "agent",
                    "sub": r["display_name"],
                    "href": href(str(r["project_id"]), area="workforce", role=r["role_key"]),
                    "project_id": r["project_id"], "role_key": r["role_key"]})

    for r in connection.execute(text(
            "SELECT id, project_id, COALESCE(title, directive) AS title FROM meeting_runs "
            "WHERE project_id = ANY(:ids) AND status = 'completed' AND state ? 'prd_markdown' AND "
            "COALESCE(title, directive) ILIKE :q ORDER BY updated_at DESC LIMIT 3"),
            {"ids": scope, "q": like}).mappings():
        out.append({"label": words(f"PRD {r['title']}", 5), "kind": "doc", "sub": "PRD",
                    "href": href(str(r["project_id"]), area="meetings", meeting=r["id"]),
                    "project_id": r["project_id"]})
    return out[:limit]


__all__ = ["search"]
