"""No host deploy of an app built from an `insecure_by_design` template (APP_TEMPLATES_AND_SECURITY_LOOP.md
§3.6: "a hard check in governed_host_deploy.py, not a UI hide").

The insecure banking variant carries planted, catalogued flaws for training: it is previewed, never
deployed. The check is per PROJECT: an insecure template is applied into a lab project, and a deploy
target names its app freely (an app name, a repository), so matching one app by name could be
sidestepped by naming the target differently. Any project that holds an application of an
`insecure_by_design` template (in any state but `abandoned`) deploys nothing to a host.

`deploy_refusal` is called by every deploy planner (resource_plane.plan_deployment_run,
governed_host_deploy.plan_host_deployment, stack_deploy.plan_stack_deploy) and again when a
governed host deploy applies, so an approval recorded before the template was applied still
cannot ship it. It fails CLOSED: if BOSS cannot read the applications, it refuses.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from .manifest import INSECURE_BY_DESIGN

ACTION = ("This project holds an app built from a template that is insecure by design (a training "
          "app with planted flaws). Preview it; deploy a separate project's app instead.")


def insecure_applications(connection: Any, project_id: str) -> list[dict]:
    rows = connection.execute(text(
        "SELECT a.app_key, t.template_key, t.version FROM app_template_applications a "
        "JOIN app_templates t ON t.id = a.template_id "
        "WHERE a.project_id = :p AND a.status <> 'abandoned' "
        "AND COALESCE(t.manifest -> 'labels', '[]'::jsonb) ? :label "
        "ORDER BY a.created_at"), {"p": project_id, "label": INSECURE_BY_DESIGN}).mappings().all()
    return [dict(r) for r in rows]


def deploy_refusal(connection: Any, project_id: Optional[str]) -> Optional[str]:
    """The headline refusing a deploy from `project_id`, or None when it may deploy."""
    if not project_id:
        return None
    try:
        with connection.begin_nested():
            found = insecure_applications(connection, str(project_id))
    except Exception as exc:  # noqa: BLE001 — unknown is refused, never shipped
        return ("BOSS could not check this project's templates for one that is insecure by design "
                f"({type(exc).__name__}), so it does not deploy.")
    if not found:
        return None
    first = found[0]
    return (f"{first['app_key']} was built from {first['template_key']} {first['version']}, a template "
            "that is insecure by design: BOSS never deploys it to a host.")
