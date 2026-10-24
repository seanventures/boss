"""Creating a project: the ONE path the API route (POST /v1/projects) and the agent's
`create_project` tool both take, so a project made by a press and one made by asking BOSS are the
same project, seeded and audited the same way.

The decision (project:create, its step-up) is made before this runs: by the route's @permit, or by
the tool registry's identity gate with the caller's own context (a person's session, a service
account's key; an agent run token never holds project:create, which is not delegable).
"""
from __future__ import annotations

from typing import Any, Optional

from ..bootstrap import ProjectExists, create_project as _seed_new_project
from .core import emit_event

__all__ = ["ProjectExists", "create"]


def create(connection, settings, *, auth: Any, name: Any, slug: Any = None,
           description: Any = None, actor: str, principal: Optional[str] = None,
           entitlements: Any = None, members: Any = None, specpack: Any = None) -> dict:
    """Make the project in the caller's org (the default org when there is no verified identity),
    seed it like the default project, and record who made it: an `events` row in the new project
    and, for an identity principal, `project.created` in the org's security log.

    Raises ProjectExists (the slug is in use) or ValueError (a bad name or slug)."""
    from .entitlements import EntitlementBrokerService
    from .members import MemberDirectoryService
    from .specpack import SpecPackService

    org_id = str(auth.principal.org_id) if auth is not None else str(settings.default_org_id)
    created = _seed_new_project(
        connection, settings, org_id=org_id, name=name, slug=slug, description=description,
        actor=actor,
        entitlements=entitlements or EntitlementBrokerService(settings),
        members=members or MemberDirectoryService(settings),
        specpack=specpack or SpecPackService(settings),
    )
    project_id = created["project_id"]
    what = {"name": created["name"], "slug": created["slug"], "org_id": org_id,
            "workspace_id": created["workspace_id"]}
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
    return created
