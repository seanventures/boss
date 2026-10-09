"""No host deploy of an app built from an `insecure_by_design` template (APP_TEMPLATES_AND_SECURITY_LOOP.md
§3.6: "a hard check in governed_host_deploy.py, not a UI hide").

The insecure banking variant carries planted, catalogued flaws for training: it is previewed, never
deployed. THE CHECK IS PER APP (operator decision, 2026-10-09; it was per project):

  * an insecure template is applied only into its own LAB project (templates.lab), so a lab
    deploys nothing, whatever its target is called — the app cannot be renamed out of the rule;
  * in any other project (an install that applied one before labs existed), the app the deploy
    target names is refused when that app holds an insecure application; the project's other
    apps deploy as usual;
  * a caller that cannot say which app it deploys (no target) is refused whenever the project
    holds an insecure application: unknown is refused, never shipped.

`deploy_refusal` is called by every deploy planner (resource_plane.plan_deployment_run,
governed_host_deploy.plan_host_deployment, stack_deploy.plan_stack_deploy) and again when a
governed host deploy applies, so an approval recorded before the template was applied still
cannot ship it. It fails CLOSED: if BOSS cannot read the applications, it refuses.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text

from .manifest import INSECURE_BY_DESIGN

ACTION = ("This app was built from a template that is insecure by design (a training app with "
          "planted flaws). Preview it; it is never deployed.")


def insecure_applications(connection: Any, project_id: str) -> list[dict]:
    rows = connection.execute(text(
        "SELECT a.app_key, t.template_key, t.version FROM app_template_applications a "
        "JOIN app_templates t ON t.id = a.template_id "
        "WHERE a.project_id = :p AND a.status <> 'abandoned' "
        "AND COALESCE(t.manifest -> 'labels', '[]'::jsonb) ? :label "
        "ORDER BY a.created_at"), {"p": project_id, "label": INSECURE_BY_DESIGN}).mappings().all()
    return [dict(r) for r in rows]


def _target_app_keys(target: Mapping[str, Any]) -> set[str]:
    """The app keys a deploy target can mean: its app name, and its repository's name."""
    from ..services.agent_assignment import app_key

    out = set()
    for raw in (target.get("app_name"), str(target.get("repo") or "").rsplit("/", 1)[-1]):
        if raw:
            out.add(app_key(str(raw)))
    return {k for k in out if k}


def deploy_refusal(connection: Any, project_id: Optional[str],
                   target: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """The headline refusing this deploy, or None when it may deploy. `target` is the deployment
    target row (its app name and repository say which app ships)."""
    if not project_id:
        return None
    try:
        with connection.begin_nested():
            from .lab import lab_of

            lab = lab_of(connection, str(project_id))
            found = insecure_applications(connection, str(project_id))
    except Exception as exc:  # noqa: BLE001 — unknown is refused, never shipped
        return ("BOSS could not check this app's templates for one that is insecure by design "
                f"({type(exc).__name__}), so it does not deploy.")
    if lab:
        return (f"{lab.get('app_name') or lab.get('app_key') or 'This app'} is in a lab project for "
                f"{lab.get('template_key') or 'a template'}, a template that is insecure by design: "
                "BOSS never deploys it to a host.")
    if not found:
        return None
    if target is not None:
        from ..services.agent_assignment import app_key

        keys = _target_app_keys(target)
        found = [a for a in found if app_key(str(a["app_key"])) in keys]
        if not found:
            return None
    first = found[0]
    return (f"{first['app_key']} was built from {first['template_key']} {first['version']}, a template "
            "that is insecure by design: BOSS never deploys it to a host.")
