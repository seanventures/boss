"""⌘K jump (contract §9, screen 11): one keystroke to any run, PR, build, deploy, agent, doc or
project.

Searches only the projects the caller may read (the route passes them). Within a project:
workflow runs by title or number ("run 4", "run #4": the run's number in its project, as the Work
drawer counts it), builds by title, pull requests by title or number ("pr 3", "PR #3", "#3",
"3"), deploys by environment or commit (every deploy, not only the newest: "deploys" lists them
all), agents by name or callsign, and the project's documents exactly as Repo & knowledge lists
them (PRDs, README, runbooks, ADRs: "prd", "readme", "docs" or a word of the name). With no
query it returns the project's newest things, so the palette opens on something useful.

Every item reads like screen 11: a short human label ("PR #3 · Visual refresh finish", the one
title helper, titles.short_title), `needs_you` when a person's answer is what it waits for, and
`hint` — where Enter takes it ("Workflows", "Code Tasks", "open ↗"). A pull request carries its
`external_url` (⌘↵ opens GitHub); a live deploy's `external_url` is the app itself. With a
project in scope, its captain comes back as one `captain` item ("Ask Helm about …").
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..decision_actions import attention
from ..local_repo_host import is_local_pr_url
from ..repo_links import pr_link
from .common import href, iso, pr_number, project_href, words

LIMIT = 20
_DEPLOY_WORDS = frozenset({"deploy", "deploys", "deployed", "deployment", "deployments", "release",
                           "releases", "ship", "shipped", "live", "environment", "environments"})
_NUMBER = re.compile(r"^(?:pr|pull)?\s*#?\s*(\d{1,7})$", re.IGNORECASE)
_RUN_NUMBER = re.compile(r"^(?:run|workflow run)\s*#?\s*(\d{1,7})$", re.IGNORECASE)
_PR_WORDS = frozenset({"pr", "prs", "pull request", "pull requests", "pull"})
#: a word that names a KIND of document lists every document of that kind ("prd", "readme", "docs").
_DOC_WORDS = {"doc": None, "docs": None, "document": None, "documents": None,
              "prd": "prd", "prds": "prd", "spec": "prd", "specs": "prd", "readme": "readme",
              "runbook": "runbook", "runbooks": "runbook", "adr": "adr", "adrs": "adr"}
#: how many projects' documents an unscoped search reads (each is a dossier read).
_DOC_PROJECTS = 8


def _like(q: str) -> str:
    return "%" + re.sub(r"([%_\\])", r"\\\1", q) + "%"


def _short(title: Any, fallback: str, brief: Any = None) -> str:
    from .titles import short_title

    return short_title(title, brief, limit=5) or fallback


def _env_word(env: Any) -> str:
    from .health import env_label

    return env_label(env)


def _bare_url(url: Any) -> str:
    return re.sub(r"^https?://", "", str(url or "")).rstrip("/")


def _waiting_runs(connection: Connection, run_ids: list[str]) -> set[str]:
    """The runs with a decision waiting on a person, by decision_actions.attention."""
    from ..decision_actions import ATTENTION_CANDIDATE_STATUSES

    if not run_ids:
        return set()
    rows = connection.execute(text(
        "SELECT * FROM decisions WHERE recommendation->>'workflow_run_id' = ANY(:r) "
        "AND status = ANY(:c)"), {"r": run_ids, "c": list(ATTENTION_CANDIDATE_STATUSES)}
    ).mappings().all()
    return {str((r["recommendation"] or {}).get("workflow_run_id")) for r in rows
            if attention(dict(r)) == "needs_action"}


def search(connection: Connection, *, q: str, project_ids: list[str],
           project_id: Optional[str] = None, limit: int = LIMIT) -> list[dict[str, Any]]:
    q = str(q or "").strip()[:100]
    found_run = _RUN_NUMBER.match(q)
    run_n = int(found_run.group(1)) if found_run else None
    found_number = None if found_run else _NUMBER.match(q)
    number = int(found_number.group(1)) if found_number else None
    bare = found_number is not None and not re.match(r"^(?:pr|pull)", q, re.IGNORECASE)
    if run_n is None and bare:
        run_n = number            # "4" / "#4": PR #4 and Run #4 both
    all_prs = q.lower() in _PR_WORDS
    term = "" if all_prs or found_run else \
        re.sub(r"^(?:pr|pull)\s+", "", q, flags=re.IGNORECASE).lstrip("#").strip()
    like = _like(term) if term else "%"
    scope = [project_id] if project_id else project_ids
    out: list[dict[str, Any]] = []
    names = {str(r["id"]): r["name"] for r in connection.execute(text(
        "SELECT id, name FROM projects WHERE id = ANY(:ids)"), {"ids": project_ids}).mappings()}

    # Projects (always across every readable project: ⌘K "go to project"), listed last so a
    # project's own records are never cut by the limit.
    projects: list[dict[str, Any]] = []
    for pid, name in names.items():
        if not q or q.lower() in name.lower() or q.lower() in pid.lower():
            projects.append({"label": words(name), "kind": "project", "sub": pid, "hint": "Project",
                        "href": project_href(pid), "project_id": pid})
    if not scope:
        return projects[:limit]

    pr_sql = ("SELECT c.id, c.project_id, c.title, c.pull_request_url, c.pull_request_number, "
              "c.status, d.status AS decision_status, d.id AS decision_id, d.default_action, "
              "d.deadline_at, d.question, d.recommendation FROM code_task_runs c "
              "LEFT JOIN decisions d ON d.id = c.decision_id "
              "WHERE c.project_id = ANY(:ids) AND COALESCE(c.pull_request_url, '') <> '' AND ")
    if found_run:
        prs = []
    elif number is not None:
        prs = connection.execute(text(
            pr_sql + "(c.pull_request_number = :n OR c.pull_request_url LIKE :tail) "
            "ORDER BY c.created_at LIMIT 12"),
            {"ids": scope, "n": number, "tail": f"%/pull/{number}"}).mappings().all()
    else:
        prs = connection.execute(text(
            pr_sql + "COALESCE(c.title, '') ILIKE :q ORDER BY c.created_at DESC LIMIT 12"),
            {"ids": scope, "q": like}).mappings().all()
    # ONE row per pull request: every retry and follow-up of a build carries the same PR.
    by_pr: dict[tuple[str, Any], dict[str, Any]] = {}
    for r in prs:
        n = r["pull_request_number"] or pr_number(r["pull_request_url"])
        key = (str(r["project_id"]), n or r["pull_request_url"])
        first = by_pr.get(key)
        # Waiting on you by the one rule every surface uses (decision_actions.attention).
        waiting = bool(r["decision_id"]) and attention(
            {**dict(r), "status": r["decision_status"]}) == "needs_action"
        if first is None:
            by_pr[key] = {"label": (f"PR #{n} · {_short(r['title'], 'pull request')}" if n
                                    else _short(r["title"], "Pull request")),
                          "kind": "pr", "sub": names.get(str(r["project_id"]), r["project_id"]),
                          "needs_you": waiting,
                          # A local change set opens BOSS's own view, never http://boss-local/.
                          "hint": ("↵ drawer · ⌘↵ change set" if is_local_pr_url(r["pull_request_url"])
                                   else "↵ drawer · ⌘↵ GitHub"),
                          "external_url": pr_link(r["pull_request_url"]) or "",
                          "href": project_href(str(r["project_id"]), tab="work",
                                               drawer=f"build:{r['id']}"),
                          "project_id": r["project_id"]}
        elif waiting:
            first["needs_you"] = True
    out.extend(list(by_pr.values())[:5])

    seen_runs: set[str] = set()
    # "Run #43": the run's number in its project, counted as the Work drawer counts it
    # (work.everything / work_story._run_number: every run of the project up to this one).
    run_rows = [dict(r) for r in connection.execute(text(
            "SELECT * FROM (SELECT r.id, r.project_id, r.status, r.created_at, "
            "COALESCE(r.input->>'title', d.name) AS title, r.input->>'directive' AS brief, d.name AS dname, "
            "d.namespace, r.mode, n.run_number "
            "FROM workflow_runs r JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
            # count(*) over created_at (peers included) = work.py's "runs up to this one".
            "JOIN (SELECT id, count(*) OVER (PARTITION BY project_id ORDER BY created_at) AS run_number "
            "      FROM workflow_runs WHERE project_id = ANY(:ids)) n ON n.id = r.id "
            "WHERE r.project_id = ANY(:ids)) r WHERE r.namespace = 'product' AND "
            "COALESCE(r.mode, 'normal') <> 'test' AND ("
            " (CAST(:rn AS INT) IS NOT NULL AND r.run_number = :rn) OR "
            " (NOT :only_rn AND (COALESCE(r.title, '') ILIKE :q OR r.dname ILIKE :q OR r.id = :exact "
            "  OR (CAST(:n AS INT) IS NOT NULL AND EXISTS ("
            "   SELECT 1 FROM code_task_runs c WHERE c.finalize_ctx->>'workflow_run_id' = r.id "
            "   AND (c.pull_request_number = :n OR c.pull_request_url LIKE :tail)))))) "
            "ORDER BY (CAST(:rn AS INT) IS NOT NULL AND r.run_number = :rn) DESC, r.created_at DESC LIMIT 12"),
            {"ids": scope, "q": like if number is None and not all_prs else "%__none__%",
             "exact": q, "n": number, "rn": run_n, "only_rn": bool(found_run),
             "tail": f"%/pull/{number}" if number is not None else ""}).mappings()]
    waiting_runs = _waiting_runs(connection, [str(r["id"]) for r in run_rows])
    for r in run_rows:
        name = _short(r["title"], "workflow run", r["brief"])
        if name in seen_runs and not (run_n is not None and int(r["run_number"] or 0) == run_n):
            continue   # a retry of the same work is the same row (unless it is the one asked for)
        seen_runs.add(name)
        num = int(r["run_number"] or 0)
        out.append({"label": f"Run #{num} · {name}" if num else f"Run · {name}",
                    "kind": "run", "sub": str(r["status"] or "").replace("_", " "),
                    "needs_you": str(r["id"]) in waiting_runs, "hint": "Workflows",
                    "href": project_href(str(r["project_id"]), tab="work", drawer=f"run:{r['id']}"),
                    "full_href": href(str(r["project_id"]), area="delivery_workflows", wfr=r["id"]),
                    "project_id": r["project_id"]})
    if found_run:
        return _with_captain(connection, out, project_id)

    seen_builds: set[str] = set()
    for r in connection.execute(text(
            "SELECT id, project_id, status, title FROM code_task_runs WHERE project_id = ANY(:ids) "
            "AND (COALESCE(title, '') ILIKE :q OR id = :exact) "
            "AND (CAST(:n AS INT) IS NULL OR pull_request_number = :n "
            "     OR pull_request_url LIKE :tail) "
            "ORDER BY created_at DESC LIMIT 8"),
            {"ids": scope, "q": like if number is None else "%", "exact": q, "n": number,
             "tail": f"%/pull/{number}" if number is not None else ""}).mappings():
        label = f"Build · {_short(r['title'], 'build')}"
        if label in seen_builds:
            continue
        seen_builds.add(label)
        out.append({"label": label, "kind": "build",
                    "sub": str(r["status"] or "").replace("_", " "), "hint": "Code Tasks",
                    "href": href(str(r["project_id"]), area="code_tasks", view="apps",
                                 build=r["id"]), "project_id": r["project_id"]})
        if len(seen_builds) >= 4:
            break

    # DEPLOYS: with no query, each environment's newest (what is live where); asked for
    # ("deploys", an environment, a commit), EVERY deploy, one row per environment and commit,
    # dated — a redeploy of the same commit is the same row.
    asked_deploys = bool(term) and (term.lower() in _DEPLOY_WORDS or number is None)
    seen_env: set[tuple[str, str]] = set()
    seen_dep: set[tuple[str, str, str]] = set()
    newest_env: set[tuple[str, str]] = set()
    # WHAT IS LIVE WHERE (11-08): each environment's newest HEALTHY deploy — the app the header
    # opens — so "Staging · 203.0.113.20:8004 ↗" is offered even when a newer deploy is still
    # running or failed (that one is its own dated row).
    live_at = {(str(r["project_id"]), str(r["environment"] or "")): r["url"] for r in
               connection.execute(text(
                   "SELECT DISTINCT ON (s.project_id, t.environment) s.project_id, t.environment, "
                   "COALESCE(NULLIF(s.open_url, ''), NULLIF(t.public_url, ''), t.domains->>0) AS url "
                   "FROM stack_deployments s "
                   "JOIN deployment_targets t ON t.id = s.target_id WHERE s.project_id = ANY(:ids) "
                   "AND s.status = 'healthy' AND t.status = 'active' "
                   "ORDER BY s.project_id, t.environment, s.created_at DESC"),
                   {"ids": scope}).mappings() if r["url"]}
    for r in connection.execute(text(
            "SELECT s.id, s.project_id, s.commit_sha, s.status, s.open_url, s.created_at, "
            "t.environment, t.public_url FROM stack_deployments s "
            "JOIN deployment_targets t ON t.id = s.target_id "
            "WHERE s.project_id = ANY(:ids) AND NOT :skip AND (t.environment ILIKE :q OR t.app_name ILIKE :q OR "
            "COALESCE(s.commit_sha, '') ILIKE :prefix OR :blank OR :numbered OR :deploys) "
            "ORDER BY s.created_at DESC LIMIT :n"),
            {"ids": scope, "q": like, "prefix": (term + "%") if term else "%",
             "blank": not q, "numbered": number is not None, "skip": all_prs,
             "deploys": term.lower() in _DEPLOY_WORDS, "n": 40 if asked_deploys else 12}).mappings():
        env_key = (str(r["project_id"]), str(r["environment"] or ""))
        if not asked_deploys and env_key in seen_env:
            continue
        seen_env.add(env_key)
        dep_key = (env_key[0], env_key[1], str(r["commit_sha"] or r["id"]))
        if dep_key in seen_dep:
            continue
        seen_dep.add(dep_key)
        first_of_env = env_key not in newest_env
        newest_env.add(env_key)
        live_url = live_at.get(env_key) if first_of_env else None
        if live_url and r["status"] != "healthy":
            # The newest deploy is not the live one: the live app first, then this deploy dated.
            out.append({"label": f"{_env_word(r['environment'])} · {_bare_url(live_url)}",
                        "kind": "deploy", "sub": "live", "hint": "open ↗",
                        "href": project_href(str(r["project_id"]), tab="deployments"),
                        "external_url": live_url if re.match(r"^https?://", live_url)
                        else f"http://{live_url}", "project_id": r["project_id"]})
            live_url = None
        if live_url:
            where = _bare_url(live_url)
        else:
            # The day is the VIEWER's (`at`, worded by the console), never a UTC "Nov 6" here.
            where = (r["commit_sha"] or "")[:7]
        item = {"label": f"{_env_word(r['environment'])} · {where}".rstrip(" ·"),
                "at": None if live_url else iso(r["created_at"]),
                "kind": "deploy", "sub": str(r["status"] or "").replace("_", " "),
                "hint": "open ↗" if live_url else "Infra",
                "href": href(str(r["project_id"]), area="infra", view="deploys", deploy=r["id"]),
                "project_id": r["project_id"]}
        if live_url:
            item["external_url"] = live_url if re.match(r"^https?://", live_url) \
                else f"http://{live_url}"
        out.append(item)
        if len(seen_dep) >= 12:
            break

    _with_captain(connection, out, project_id)

    for r in connection.execute(text(
            "SELECT project_id, role_key, display_name, spec->'persona'->>'callsign' AS callsign "
            "FROM roles WHERE project_id = ANY(:ids) AND (display_name ILIKE :q OR "
            "COALESCE(spec->'persona'->>'callsign', '') ILIKE :q OR role_key ILIKE :q) "
            "AND :searching ORDER BY (spec->'persona'->>'callsign') IS NULL, display_name LIMIT 5"),
            {"ids": scope, "q": like, "searching": bool(q) and number is None}).mappings():
        name = r["callsign"] or r["display_name"]
        out.append({"label": words(f"Ask {name}" if r["callsign"] else name), "kind": "agent",
                    "sub": r["display_name"], "hint": "Agent Studio",
                    "href": href(str(r["project_id"]), area="workforce", role=r["role_key"]),
                    "project_id": r["project_id"], "role_key": r["role_key"]})

    if number is None and not all_prs:
        out.extend(_docs(connection, scope, names, term))
    room = max(5, limit - len(out))
    return (out + projects[:room])[:max(limit, len(out))]


def _with_captain(connection: Connection, out: list[dict[str, Any]],
                  project_id: Optional[str]) -> list[dict[str, Any]]:
    """With a project in scope, its captain as one `captain` item ("Ask Helm about …")."""
    if project_id:
        from .captain import chosen_role
        from .common import agents, face

        roster = agents(connection, project_id)
        key, _how = chosen_role(connection, project_id, roster)
        cap = face(roster, key) if key else None
        if cap:
            out.append({"label": cap["name"], "kind": "captain", "sub": "captain",
                        "hint": "captain", "role_key": cap["role_key"],
                        "agent_id": cap.get("agent_id"),
                        "href": project_href(project_id, drawer="captain"),
                        "project_id": project_id})
    return out


def _docs(connection: Connection, scope: list[str], names: dict[str, Any],
          term: str) -> list[dict[str, Any]]:
    """The documents Repo & knowledge lists (knowledge.project_docs), found by kind ("prd",
    "readme", "docs") or by a word of their name; with no query, the newest three."""
    from .knowledge import project_docs

    want = term.lower().strip()
    kind = _DOC_WORDS.get(want, "") if want in _DOC_WORDS else ""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pid in scope[:_DOC_PROJECTS]:
        for d in project_docs(connection, pid):
            label = str(d.get("label") or "")
            hay = label.lower() + " " + ("" if d.get("kind") == "prd" else str(d.get("detail") or "").lower())
            if want and want not in _DOC_WORDS and not all(w in hay for w in want.split()):
                continue
            if kind and d.get("kind") != kind:
                continue
            key = (pid, label)
            if key in seen:
                continue
            seen.add(key)
            out.append({"label": label, "kind": "doc",
                        "sub": d.get("sub") or names.get(pid, pid),
                        "hint": "Meetings" if d.get("kind") == "prd" else "Repo & knowledge",
                        "href": d.get("href") or project_href(pid, tab="knowledge"),
                        "external_url": d.get("external_url") or "", "project_id": pid})
    return out[:3] if not want else out[:8]


__all__ = ["search"]
