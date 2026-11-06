"""Repo & knowledge tab (contract §4): the repositories with their access checks, the dossier
Discovery wrote (BOSSAPP-909), a small knowledge-graph snippet, the docs and the decisions.

Reads project_dossiers (ambient/dossier.py), repo_index.status (how far the code is mapped),
kg_nodes / kg_edges, meeting_runs (PRDs) and decided plan/architecture approvals.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import as_json, href, iso, project_href, repos, words

GRAPH_NODES = 12


def _stack(facts: dict[str, Any]) -> list[str]:
    stack = facts.get("stack") or {}
    out = [w for w in (stack.get("language"), stack.get("framework"), stack.get("build")) if w]
    for lang in facts.get("languages") or []:
        name = lang.get("language") if isinstance(lang, dict) else None
        if name and name not in out:
            out.append(name)
    return out[:6]


def commands(facts: dict[str, Any], stack: Optional[dict[str, Any]] = None) -> dict[str, Optional[str]]:
    """{build, test, run}: the first command the repository declares for each (the dossier has
    no run command; the stack's usual one is said only when the build tool names it)."""
    cmds = facts.get("commands") or {}
    build = (cmds.get("build") or [None])[0]
    test = (cmds.get("test") or [None])[0]
    run = None
    tool = str(((stack or facts.get("stack") or {}).get("build")) or "")
    if tool in ("npm", "pnpm", "Yarn"):
        run = {"npm": "npm start", "pnpm": "pnpm start", "Yarn": "yarn start"}[tool]
    elif tool == "Maven":
        run = "mvn spring-boot:run" if (facts.get("stack") or {}).get("framework") == "Spring" else None
    elif tool == "Gradle":
        run = "./gradlew bootRun" if (facts.get("stack") or {}).get("framework") == "Spring" else None
    return {"build": build, "test": test, "run": run, "lint": (cmds.get("lint") or [None])[0]}


def _hotspots(facts: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for h in (facts.get("hotspots") or [])[:6]:
        path = str(h.get("file") or "")
        if not path:
            continue
        stem = PurePosixPath(path).stem.replace("_", " ").replace("-", " ")
        why = f"{h.get('churn', 0)} lines changed in 180 days, {h.get('lines', 0)} lines"
        if not h.get("has_tests"):
            why += ", no tests"
        out.append({"path": path, "label": words(stem.title()), "why": why,
                    "has_tests": bool(h.get("has_tests"))})
    return out


def dossier(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    from ..ambient import dossier as dossier_service

    rows = dossier_service.get(connection, project_id)
    if not rows:
        return None
    row = rows[0]
    facts = as_json(row.get("facts"), {}) or {}
    owners = facts.get("owners") or {}
    return {"repo": row.get("repo"), "stack": _stack(facts), "commands": commands(facts),
            "tests": {"files": int((facts.get("tests") or {}).get("files") or 0)},
            "files": facts.get("files"), "ci": facts.get("ci") or [],
            "hotspots": _hotspots(facts),
            "todo_clusters": [{"dir": c.get("dir"), "count": c.get("count"),
                               "label": words(f"{c.get('count')} TODOs {c.get('dir')}")}
                              for c in (facts.get("todos") or {}).get("clusters") or []][:5],
            "owners": ([{"name": p.get("name"), "commits": p.get("commits")}
                        for p in owners.get("top_committers") or []][:5]),
            "codeowners": owners.get("codeowners"),
            "highlights": row.get("highlights") or [], "read_from": row.get("read_from"),
            "built_at": row.get("built_at"), "version": row.get("version"),
            "href": project_href(project_id, tab="knowledge", drawer="dossier")}


#: The kinds a snippet draws, a few of each, newest first: the code's shape (modules, endpoints,
#: tables) and what agents learned on it (decisions, workarounds, fixes).
SNIPPET_KINDS = ("module", "endpoint", "table", "decision", "workaround", "fix")
PER_KIND = 3


def graph(connection: Connection, project_id: str, limit: int = GRAPH_NODES) -> dict[str, Any]:
    """A small snippet of the project's graph: a few current nodes of each kind that tells the
    story (each read through the (project, kind, last_seen) index), and the edges among them.
    Cheap on a graph of tens of thousands of nodes."""
    parts = " UNION ALL ".join(
        f"(SELECT id, kind, name, path FROM kg_nodes WHERE project_id = :p AND kind = :k{i} "
        f"AND valid_to IS NULL ORDER BY last_seen DESC NULLS LAST LIMIT {PER_KIND})"
        for i in range(len(SNIPPET_KINDS)))
    params: dict[str, Any] = {"p": project_id, **{f"k{i}": k for i, k in enumerate(SNIPPET_KINDS)}}
    nodes = [dict(r) for r in connection.execute(text(parts), params).mappings().all()][:limit]
    ids = [n["id"] for n in nodes]
    edges = [dict(r) for r in connection.execute(text(
        "SELECT src_id, dst_id, kind FROM kg_edges WHERE project_id = :p AND valid_to IS NULL "
        "AND src_id = ANY(:ids) AND dst_id = ANY(:ids) LIMIT 60"),
        {"p": project_id, "ids": ids}).mappings().all()] if ids else []
    total = int(connection.execute(text(
        "SELECT COALESCE(sum(nodes), 0) FROM kg_repo_index WHERE project_id = :p"),
        {"p": project_id}).scalar() or 0)
    return {"nodes": [{"id": n["id"], "kind": n["kind"], "label": words(n["name"]),
                       "detail": n["path"] or n["name"]} for n in nodes],
            "edges": [{"from": e["src_id"], "to": e["dst_id"], "kind": e["kind"]} for e in edges],
            "total_nodes": total,
            "href": href(project_id, area="knowledge")}


def mapping(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """How far each repository's code is mapped, from its index row (not a count of the graph)."""
    from ..start_project import mapping as mapped

    rows = connection.execute(text(
        "SELECT b.full_name, r.status, r.files, r.parsed, r.nodes, r.error, r.indexed_at "
        "FROM github_repository_bindings b LEFT JOIN kg_repo_index r ON r.binding_id = b.id "
        "AND r.project_id = b.project_id WHERE b.project_id = :p AND b.status = 'active' "
        "ORDER BY b.full_name"), {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        item = None if r["status"] is None else {
            "status": r["status"], "files": r["files"], "parsed": r["parsed"],
            "graph": {"nodes": r["nodes"] or 0}, "error": r["error"]}
        out.append({"repo": r["full_name"], **mapped(item), "files": r["files"],
                    "parsed": r["parsed"], "indexed_at": iso(r["indexed_at"])})
    return out


def docs(connection: Connection, project_id: str, facts_docs: dict[str, Any],
         repo: Optional[str]) -> list[dict[str, Any]]:
    out = []
    base = f"https://github.com/{repo}/blob/HEAD/" if repo else None
    for r in connection.execute(text(
            "SELECT id, COALESCE(title, directive) AS title, updated_at FROM meeting_runs "
            "WHERE project_id = :p AND status = 'completed' AND state ? 'prd_markdown' "
            "ORDER BY updated_at DESC LIMIT 6"), {"p": project_id}).mappings().all():
        out.append({"label": words(f"PRD: {r['title']}", 5), "detail": r["title"], "kind": "prd",
                    "at": iso(r["updated_at"]), "href": href(project_id, area="meetings",
                                                             meeting=r["id"])})
    if facts_docs.get("readme"):
        out.append({"label": "README", "kind": "readme", "detail": facts_docs["readme"],
                    "external_url": (base + facts_docs["readme"]) if base else None,
                    "href": project_href(project_id, tab="knowledge", drawer="doc:readme")})
    for path in facts_docs.get("runbooks") or []:
        out.append({"label": words(PurePosixPath(path).stem.replace("-", " ").title()),
                    "kind": "runbook", "detail": path,
                    "external_url": (base + path) if base else None,
                    "href": project_href(project_id, tab="knowledge", drawer=f"doc:{path}")})
    return out[:12]


def decisions(connection: Connection, project_id: str, limit: int = 8) -> list[dict[str, Any]]:
    """What people decided on this project's plans and designs (answered approvals of workflow
    plan / architecture steps), newest first."""
    rows = connection.execute(text(
        "SELECT id, question, status, approved_at, updated_at, recommendation->>'capability' AS cap "
        "FROM decisions WHERE project_id = :p AND status IN ('approved', 'executed') AND "
        "(recommendation->>'capability' IN ('spec_review', 'architecture', 'meeting') OR "
        " recommendation->>'action' = 'review_architecture') "
        "ORDER BY COALESCE(approved_at, updated_at) DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()
    return [{"label": words(r["question"], 5), "detail": r["question"],
             "at": iso(r["approved_at"] or r["updated_at"]),
             "href": href(project_id, area="decisions", decision=r["id"])} for r in rows]


def view(connection: Connection, project_id: str) -> dict[str, Any]:
    d = dossier(connection, project_id)
    facts_docs: dict[str, Any] = {}
    if d is not None:
        from ..ambient import dossier as dossier_service

        row = dossier_service.get(connection, project_id)[0]
        facts_docs = (as_json(row.get("facts"), {}) or {}).get("docs") or {}
    the_repos = repos(connection, project_id)
    primary = the_repos[0]["full_name"] if the_repos else None
    out = {"repos": the_repos, "mapping": mapping(connection, project_id), "dossier": d,
           "graph": graph(connection, project_id),
           "docs": docs(connection, project_id, facts_docs, primary),
           "decisions": decisions(connection, project_id)}
    if not the_repos:
        out["empty"] = {"label": "No repository yet",
                        "detail": "Add the project's repository and BOSS reads it: stack, "
                                  "commands, tests, owners and hot spots.",
                        "href": href(project_id, area="project", tab="settings",
                                     drawer="section:repos")}
    elif d is None:
        out["empty"] = {"label": "Reading the repository",
                        "detail": "The dossier appears once the code is mapped; onboarding "
                                  "builds it.", "href": project_href(project_id, tab="overview",
                                                                    drawer="onboarding")}
    return out


__all__ = ["commands", "decisions", "docs", "dossier", "graph", "mapping", "view"]
