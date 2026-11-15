"""Creating a project: the ONE path the API route (POST /v1/projects) and the agent's
`create_project` tool both take, so a project made by a press and one made by asking BOSS are the
same project, seeded and audited the same way.

The decision (project:create, its step-up) is made before this runs: by the route's @permit, or by
the tool registry's identity gate with the caller's own context (a person's session, a service
account's key; an agent run token never holds project:create, which is not delegable).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import text

from ..bootstrap import (
    PROJECT_TYPES, ProjectExists, create_project as _seed_new_project, free_project_name,
)
from .core import emit_event

__all__ = ["PROJECT_TYPES", "ProjectExists", "create"]

#: Try a sample (BOSSAPP-1014). `hello-world` is the one the console sends.
_SAMPLE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def _sample_key(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    if _SAMPLE_KEY.fullmatch(key) is None:
        raise ValueError("The sample key is lowercase letters, digits and hyphens "
                         "(for example hello-world).")
    return key


def _live_sample(connection, org_id: str, key: str) -> dict | None:
    """The org's live project for this sample, or None. An archived row gives the key up so a
    deleted sample can be made again. Held under the org+key lock for the rest of the transaction."""
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                       {"k": f"project.sample:{org_id}:{key}"})
    row = connection.execute(text(
        """
        SELECT p.id, p.workspace_id, w.org_id, p.name, p.slug, p.description, p.project_type
        FROM projects p
        JOIN workspaces w ON w.id = p.workspace_id
        WHERE w.org_id = :o AND p.sample_key = :k AND p.archived_at IS NULL
        ORDER BY p.created_at, p.id
        LIMIT 1
        """), {"o": org_id, "k": key}).mappings().first()
    if row is not None:
        return {"project_id": str(row["id"]), "workspace_id": str(row["workspace_id"]),
                "org_id": str(row["org_id"]), "name": row["name"], "slug": row["slug"],
                "description": row["description"], "project_type": row["project_type"],
                "sample_key": key, "existed": True}
    connection.execute(text(
        """
        UPDATE projects AS p SET sample_key = NULL
        FROM workspaces w
        WHERE w.id = p.workspace_id AND w.org_id = :o AND p.sample_key = :k
          AND p.archived_at IS NOT NULL
        """), {"o": org_id, "k": key})
    return None


def create(connection, settings, *, auth: Any, name: Any, slug: Any = None,
           description: Any = None, actor: str, principal: Optional[str] = None,
           entitlements: Any = None, members: Any = None, specpack: Any = None,
           project_type: Any = None, sample_key: Any = None) -> dict:
    """Make the project in the caller's org (the default org when there is no verified identity),
    seed it like the default project, and record who made it: an `events` row in the new project
    and, for an identity principal, `project.created` in the org's security log.

    `project_type` is `app` (the default: it builds apps) or `knowledge_graph` (a knowledge graph
    for an existing codebase). A knowledge-graph project starts with the project's knowledge
    graph switched on for its agents (project setting `knowledge_graph` = on, pinned here so an
    organisation-wide default cannot leave it off); its guided setup is
    services/knowledge/project_setup.py, the same steps the console and the agent walk.

    `sample_key` finds that sample in the org, or creates it (the next free name when the slug
    is taken) and stamps the key. A second call returns the same project and writes nothing new.

    Raises ProjectExists (the slug is in use) or ValueError (a bad name, slug, sample key or type)."""
    from .entitlements import EntitlementBrokerService
    from .members import MemberDirectoryService
    from .specpack import SpecPackService

    key = _sample_key(sample_key)
    org_id = str(auth.principal.org_id) if auth is not None else str(settings.default_org_id)
    if key:
        found = _live_sample(connection, org_id, key)
        if found is not None:
            return found
    explicit_slug = slug is not None and str(slug).strip() != ""
    chosen = free_project_name(connection, org_id, str(name or "")) if key and not explicit_slug else name
    entitlements = entitlements or EntitlementBrokerService(settings)
    members = members or MemberDirectoryService(settings)
    specpack = specpack or SpecPackService(settings)
    created = None
    clash: ProjectExists | None = None
    # The sample lock serializes the same key. A few tries cover a plain project taking the name
    # between the free-name read and the insert.
    for _try in range(5 if key and not explicit_slug else 1):
        try:
            created = _seed_new_project(
                connection, settings, org_id=org_id, name=chosen,
                slug=None if key and not explicit_slug else slug, description=description,
                actor=actor, entitlements=entitlements, members=members, specpack=specpack,
                project_type=project_type,
            )
            break
        except ProjectExists as error:
            clash = error
            if not key or explicit_slug:
                raise
            chosen = free_project_name(connection, org_id, str(name or ""))
    if created is None:
        raise clash if clash is not None else ProjectExists(str(slug or ""), None)
    if key:
        connection.execute(text("UPDATE projects SET sample_key = :k WHERE id = :id"),
                           {"k": key, "id": created["project_id"]})
        created["sample_key"] = key
    project_id = created["project_id"]
    if created["project_type"] == "knowledge_graph":
        from . import project_settings

        project_settings.set_one(connection, project_id, "knowledge_graph", "on", actor=actor,
                                 scope="project")
    what = {"name": created["name"], "slug": created["slug"], "org_id": org_id,
            "workspace_id": created["workspace_id"], "project_type": created["project_type"]}
    emit_event(connection, project_id, "project.created", actor, "project", project_id,
               {**what, "description": created["description"],
                "principal_id": auth.principal.id if auth is not None else principal})
    if auth is not None:
        from ..identity import audit
        from ..identity.permissions_gen import P

        audit.record(audit.SecurityEvent.from_context(
            auth, "project.created", audit.Outcome.SUCCESS, target_type="project",
            target_id=project_id, permission=P.PROJECT_CREATE.value, scope=f"org:{org_id}",
            details=what), db=connection)
    _take_over(connection, project_id, created["project_type"], actor=actor)
    return created


def _take_over(connection, project_id: str, project_type: str, *, actor: str) -> None:
    """THE CAPTAIN TAKES THE PROJECT OVER (BOSSAPP-913): the project's default captain is
    recorded and the onboarding agent starts (an app project; a knowledge-graph project has its
    own guided setup). Its own savepoint: onboarding is a help, it never fails a creation."""
    import logging

    from .project_experience import captain, onboarding

    try:
        with connection.begin_nested():
            captain.ensure_default(connection, project_id, actor=actor)
            if project_type != "knowledge_graph":
                onboarding.start(connection, project_id, trigger="created", actor=actor)
    except Exception:  # noqa: BLE001 — recorded in the log; the project is made either way
        logging.getLogger(__name__).warning("onboarding did not start for %s", project_id,
                                            exc_info=True)
