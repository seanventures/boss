"""Resolve WHICH repository a piece of work targets, from the project's registry.

Why this exists
---------------
BOSS already had the whole multi-repo framework -- `github_connections` (per-project
credentials, held as a `credential_ref` pointing at a SecretRef, never a raw token) and
`github_repository_bindings` (per-project repositories, each with a role and default
branch), plus `GitHubConnectionService.adapter_for_binding()` which turns a binding into a
live, credentialed client.

What was missing is that NOTHING resolved a repository through it. Every code task used
`settings.target_github_owner` / `settings.target_github_repo` -- ONE globally hardcoded
repo per deployment. That is fine for a single-repo dogfood and wrong for any real
customer, who has many repos and expects them managed centrally with their own access.

This module is the missing seam: given a project and an optional selector, it answers
"which repo, via which connection". It falls back to the legacy global settings so that
existing single-repo deployments keep working unchanged while a registry is populated.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text


class RepositoryResolutionError(RuntimeError):
    """The requested repository could not be resolved for this project."""


def _row_to_target(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    return {
        "owner": row["owner"],
        "repo": row["name"],
        "full_name": row["full_name"],
        "binding_id": row["id"],
        "connection_id": row["connection_id"],
        "default_branch": row.get("default_branch") or "main",
        "role": row.get("role"),
        "source": source,
    }


def list_repositories(connection, project_id: str, *, limit: int = 50) -> list[dict]:
    """Every repository registered to this project, with its connection's health."""
    rows = connection.execute(
        text(
            """
            SELECT b.id, b.connection_id, b.owner, b.name, b.full_name, b.role,
                   b.default_branch, b.visibility, b.archived, b.status,
                   c.connection_label, c.status AS connection_status,
                   c.auth_method, c.account_login, c.last_test_status
            FROM github_repository_bindings b
            JOIN github_connections c ON c.id = b.connection_id
            WHERE b.project_id = :p
            ORDER BY (b.role = 'primary') DESC, b.full_name
            LIMIT :lim
            """
        ),
        {"p": project_id, "lim": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


def repository_activity(
    connection,
    project_id: str,
    *,
    days: int = 30,
    recent_limit: int = 10,
) -> list[dict[str, Any]]:
    """Per-repository KPIs for what BOSS actually DID in a developer's repo.

    Answers the questions a developer asks about their own repo -- how much work ran,
    how much of it shipped, how often it lands first time, how long it takes and what
    it cost -- rather than "is a connection configured".

    Derived from code_task_runs (grouped by the owner/repo recorded on the run spec)
    joined to llm_calls for real spend. Merge state is NOT in the DB (BOSS's own merges
    are recorded by the merge job; hand-merges are not), so `prs_merged` is filled in
    live by the caller where a healthy connection exists, and stays None otherwise.
    """
    rows = connection.execute(
        text(
            """
            WITH runs AS (
                SELECT r.id, r.spec->>'owner' AS owner, r.spec->>'repo' AS repo,
                       r.status, r.title, r.pull_request_number, r.pull_request_url,
                       r.created_at, r.updated_at,
                       EXTRACT(EPOCH FROM (r.updated_at - r.created_at))/60.0 AS minutes
                FROM code_task_runs r
                WHERE r.project_id = :p
                  AND r.created_at > now() - make_interval(days => :d)
            ),
            spend AS (
                SELECT l.code_task_run_id AS id,
                       sum(l.cost_usd_micros) AS micros,
                       sum(l.total_tokens)   AS tokens
                FROM llm_calls l GROUP BY l.code_task_run_id
            )
            SELECT runs.owner, runs.repo,
                   count(*)                                                   AS runs_total,
                   count(*) FILTER (WHERE status = 'completed')               AS runs_completed,
                   count(*) FILTER (WHERE status = 'failed')                  AS runs_failed,
                   count(*) FILTER (WHERE status IN ('running','repairing','queued','blocked'))
                                                                              AS runs_active,
                   count(pull_request_number)                                 AS prs_opened,
                   count(*) FILTER (WHERE status = 'completed'
                                    AND title NOT ILIKE '%%retry%%')          AS first_pass_ships,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY minutes)
                       FILTER (WHERE pull_request_number IS NOT NULL)         AS median_minutes_to_pr,
                   COALESCE(sum(spend.micros), 0)                             AS micros,
                   COALESCE(sum(spend.tokens), 0)                             AS tokens,
                   max(runs.updated_at)                                       AS last_activity_at
            FROM runs LEFT JOIN spend ON spend.id = runs.id
            WHERE runs.owner IS NOT NULL AND runs.repo IS NOT NULL
            GROUP BY runs.owner, runs.repo
            ORDER BY count(*) DESC
            """
        ),
        {"p": project_id, "d": days},
    ).mappings().all()

    out: list[dict[str, Any]] = []
    for r in rows:
        total = int(r["runs_total"] or 0)
        completed = int(r["runs_completed"] or 0)
        opened = int(r["prs_opened"] or 0)
        usd = round(int(r["micros"] or 0) / 1_000_000, 2)
        recent = connection.execute(
            text(
                """
                SELECT r.id, r.title, r.status, r.pull_request_number, r.pull_request_url,
                       r.created_at, r.updated_at,
                       EXTRACT(EPOCH FROM (r.updated_at - r.created_at))/60.0 AS minutes,
                       COALESCE((SELECT sum(l.cost_usd_micros) FROM llm_calls l
                                 WHERE l.code_task_run_id = r.id), 0) AS micros
                FROM code_task_runs r
                WHERE r.project_id = :p AND r.spec->>'owner' = :o AND r.spec->>'repo' = :r
                ORDER BY r.created_at DESC LIMIT :lim
                """
            ),
            {"p": project_id, "o": r["owner"], "r": r["repo"], "lim": recent_limit},
        ).mappings().all()
        out.append({
            "owner": r["owner"], "repo": r["repo"],
            "full_name": f"{r['owner']}/{r['repo']}",
            "window_days": days,
            "runs_total": total,
            "runs_completed": completed,
            "runs_failed": int(r["runs_failed"] or 0),
            "runs_active": int(r["runs_active"] or 0),
            "prs_opened": opened,
            "prs_merged": None,          # filled in live by the caller (not in the DB)
            "success_rate": round(completed / total * 100, 1) if total else None,
            "first_pass_rate": round(int(r["first_pass_ships"] or 0) / total * 100, 1) if total else None,
            "median_minutes_to_pr": round(float(r["median_minutes_to_pr"]), 1)
                                    if r["median_minutes_to_pr"] is not None else None,
            "spend_usd": usd,
            "spend_per_pr_usd": round(usd / opened, 2) if opened else None,
            "tokens": int(r["tokens"] or 0),
            "last_activity_at": r["last_activity_at"],
            "recent_runs": [
                {
                    "id": x["id"], "title": x["title"], "status": x["status"],
                    "pr": x["pull_request_number"], "pr_url": x["pull_request_url"],
                    "minutes": round(float(x["minutes"]), 1) if x["minutes"] is not None else None,
                    "cost_usd": round(int(x["micros"] or 0) / 1_000_000, 2),
                    "created_at": x["created_at"],
                }
                for x in recent
            ],
        })
    return out


def fill_merged_counts(activity: list[dict[str, Any]], *, token: str,
                       api_base_url: str = "https://api.github.com",
                       max_lookups: int = 25) -> None:
    """Fill `prs_merged` from GitHub. Best-effort: merge state is not in our DB.

    Bounded to `max_lookups` PRs per repo so a dashboard render can never fan out into
    an unbounded API sweep. Leaves prs_merged as None on any failure -- a wrong zero
    would read as "BOSS never lands anything", which is worse than an honest blank.
    """
    if not token:
        return
    import httpx

    for repo in activity:
        prs = [r["pr"] for r in repo.get("recent_runs", []) if r.get("pr")][:max_lookups]
        if not prs:
            repo["prs_merged"] = 0
            continue
        merged = 0
        try:
            with httpx.Client(timeout=10.0) as client:
                for number in prs:
                    resp = client.get(
                        f"{api_base_url}/repos/{repo['owner']}/{repo['repo']}/pulls/{number}",
                        headers={"Authorization": f"Bearer {token}",
                                 "Accept": "application/vnd.github+json"},
                    )
                    if resp.status_code == 200 and resp.json().get("merged"):
                        merged += 1
            repo["prs_merged"] = merged
            repo["merge_rate"] = (round(merged / len(prs) * 100, 1) if prs else None)
        except Exception:
            repo["prs_merged"] = None


def resolve_repository_target(
    connection,
    settings,
    project_id: str,
    *,
    selector: Optional[str] = None,
    allow_legacy_fallback: bool = True,
) -> dict[str, Any]:
    """Resolve the repository for a unit of work.

    selector may be a binding id, an "owner/name" full name, or a bare repo name.
    With no selector the project's `primary` binding wins, then a lone active binding.
    Falls back to the legacy global settings only when the project has no registry yet,
    so single-repo deployments keep working while multi-repo customers are onboarded.
    """
    bindings = [b for b in list_repositories(connection, project_id, limit=200)
                if b["status"] == "active"]

    if selector:
        wanted = selector.strip()
        lowered = wanted.lower()
        for b in bindings:
            if (b["id"] == wanted
                    or (b["full_name"] or "").lower() == lowered
                    or (b["name"] or "").lower() == lowered):
                return _row_to_target(b, source="registry:selector")
        # An explicit ask that cannot be honoured must NOT silently fall back to some
        # other repo -- that would write a customer's code into the wrong place.
        known = ", ".join(b["full_name"] for b in bindings) or "(none registered)"
        raise RepositoryResolutionError(
            f"No active repository {selector!r} registered for project {project_id}. "
            f"Registered: {known}. Register it first (github repository binding), "
            "or pass one of the names above."
        )

    primary = [b for b in bindings if (b["role"] or "").lower() == "primary"]
    if primary:
        return _row_to_target(primary[0], source="registry:primary")
    if len(bindings) == 1:
        return _row_to_target(bindings[0], source="registry:only")
    if len(bindings) > 1:
        names = ", ".join(b["full_name"] for b in bindings)
        raise RepositoryResolutionError(
            f"Project {project_id} has {len(bindings)} repositories and no primary: "
            f"{names}. Pass an explicit repository, or mark one binding as primary."
        )

    if allow_legacy_fallback:
        owner = getattr(settings, "target_github_owner", "") or ""
        repo = getattr(settings, "target_github_repo", "") or ""
        if owner and repo:
            return {
                "owner": owner, "repo": repo, "full_name": f"{owner}/{repo}",
                "binding_id": None, "connection_id": None,
                "default_branch": "main", "role": None,
                "source": "legacy_settings",
            }
    raise RepositoryResolutionError(
        f"No repository registered for project {project_id} and no legacy default "
        "configured. Register a GitHub connection and a repository binding first."
    )
