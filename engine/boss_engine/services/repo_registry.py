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
