"""Project › Settings (contract §11): the project's own settings in one place, each section
listing what is set (names only, never a secret value) and, as `actions`, the EXISTING write
endpoint that changes it. No new write path: the old screens keep working and link here.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import captain as captain_service
from . import demo_logins as demo
from .common import as_json, href, iso, project_href, repos
from .health import env_label

STEP_UP = "step_up"


def _action(label: str, method: str, path: str, *, requires: str = "", body: Any = None,
            permission: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"label": label, "method": method, "href": path}
    if requires:
        out["requires"] = requires
    if permission:
        out["permission"] = permission
    if body is not None:
        out["body"] = body
    return out


def _demo_section(connection: Connection, project_id: str) -> dict[str, Any]:
    listing = demo.listing(connection, project_id)
    items = []
    for env in listing["environments"]:
        for row in env["logins"]:
            items.append({"env": env["key"], "env_label": env["label"], "user": row["user"],
                          "label": row["label"], "password_key": row["password_key"],
                          "reveal": row["reveal"]})
    return {"key": "demo_logins", "label": "Demo logins", "icon": "key", "items": items,
            "users_from": listing["source"],
            "hint": None if listing["users"] else
            "Name the demo users in boss.deploy.yaml: demo_logins: [{user, label}]",
            "actions": [_action(f"Reveal {e['label']}", "GET", demo.reveal_href(project_id, e["key"]),
                                requires=STEP_UP, permission="secret:manage")
                        for e in listing["environments"]]}


def _deploy_values(connection: Connection, project_id: str) -> dict[str, Any]:
    rows = connection.execute(text(
        "SELECT app_key, env, name, mode, set_by, set_at FROM app_secret_bindings "
        "WHERE project_id = :p ORDER BY app_key, name, env"), {"p": project_id}).mappings().all()
    items = [{"app_key": r["app_key"], "name": r["name"], "env": r["env"], "mode": r["mode"],
              "set_by": r["set_by"], "set_at": iso(r["set_at"]), "set": True,
              "actions": [_action("Set", "PUT", f"/v1/apps/{r['app_key']}/secrets/{r['name']}",
                                  requires=STEP_UP, permission="app_secret:write",
                                  body={"project_id": project_id, "env": r["env"]}),
                          _action("Remove", "DELETE",
                                  f"/v1/apps/{r['app_key']}/secrets/{r['name']}?project_id="
                                  f"{project_id}&env={r['env']}", requires=STEP_UP,
                                  permission="app_secret:write")]}
             for r in rows]
    have = {r["name"] for r in rows}
    found = connection.execute(text(
        "SELECT facts FROM project_onboarding_runs WHERE project_id = :p "
        "ORDER BY started_at DESC LIMIT 1"), {"p": project_id}).scalar()
    for name in ((as_json(found, {}) or {}).get("secrets") or {}).get("yours") or []:
        if name not in have:
            items.append({"app_key": None, "name": name, "env": "deploy", "mode": None,
                          "set": False, "needed": True, "actions": []})
    apps = sorted({r["app_key"] for r in rows})
    return {"key": "deploy_values", "label": "Deploy values", "icon": "lock", "items": items,
            "apps": apps,
            "actions": [_action("Open app secrets", "GET", f"/v1/apps/{a}/secrets?project_id="
                                f"{project_id}", permission="app_secret:read_metadata")
                        for a in apps]}


def _targets(connection: Connection, project_id: str) -> dict[str, Any]:
    from ..deployment_fleet import usable_hosts_sql

    rows = connection.execute(text(
        "SELECT t.id, t.app_name, t.environment, t.auto_deploy, t.paused, t.host_id, "
        "h.name AS host_name, h.address AS host_address FROM deployment_targets t "
        "LEFT JOIN deployment_hosts h ON h.id = t.host_id WHERE t.project_id = :p "
        "AND t.status = 'active' ORDER BY t.environment, t.app_name"),
        {"p": project_id}).mappings().all()
    hosts = connection.execute(text(
        f"SELECT id, name, address, status, project_id FROM deployment_hosts WHERE "
        f"status <> 'retired' AND {usable_hosts_sql()} ORDER BY name"),
        {"p": project_id}).mappings().all()
    items = [{"kind": "target", "id": r["id"], "app": r["app_name"], "env": r["environment"],
              "env_label": env_label(r["environment"]), "host": r["host_address"] or r["host_name"],
              "policy": "auto" if r["auto_deploy"] else "person", "paused": bool(r["paused"]),
              "actions": [_action("Edit", "PATCH", f"/v1/deployment-targets/{r['id']}",
                                  permission="deployment_target:manage"),
                          _action("Auto-deploy", "POST",
                                  f"/v1/deployment-targets/{r['id']}/auto-deploy",
                                  permission="deployment_target:manage")]}
             for r in rows]
    items += [{"kind": "host", "id": h["id"], "name": h["name"], "address": h["address"],
               "status": h["status"], "shared": h["project_id"] != project_id,
               "href": href(project_id, area="infra", view="hosts", host=h["id"])} for h in hosts]
    return {"key": "targets", "label": "Deploy targets", "icon": "server", "items": items,
            "actions": [_action("Add a target", "POST", "/v1/deployment-targets", requires=STEP_UP,
                                permission="deployment_target:manage",
                                body={"project_id": project_id}),
                        _action("Add a server", "POST", "/v1/deployment-hosts",
                                permission="host:manage", body={"project_id": project_id})]}


def _captain(connection: Connection, project_id: str) -> dict[str, Any]:
    cap = captain_service.view(connection, project_id, with_last_said=False)
    return {"key": "captain", "label": "Captain", "icon": "user", "captain": {
        k: cap.get(k) for k in ("agent_id", "name", "role_key", "avatar", "chosen")},
            "items": cap.get("squad") or [], "candidates": cap.get("candidates") or [],
            "actions": [_action("Change captain", "POST", f"/v1/projects/{project_id}/captain",
                                requires=STEP_UP, permission="project:update",
                                body={"agent_id": "…"})]}


def _autonomy(connection: Connection, project_id: str) -> dict[str, Any]:
    levels = connection.execute(text(
        "SELECT task_class, level, ceiling, set_by, updated_at FROM ambient_autonomy "
        "WHERE project_id = :p ORDER BY task_class"), {"p": project_id}).mappings().all()
    amb = connection.execute(text(
        "SELECT daily_cap_usd_micros, max_concurrent FROM ambient_settings WHERE project_id = :p"),
        {"p": project_id}).mappings().first()
    policy = connection.execute(text(
        "SELECT daily_cap_usd_micros, monthly_cap_usd_micros, operational_intensity "
        "FROM budget_policies WHERE scope_type = 'project' AND scope_id = :p "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
    items = [{"task_class": r["task_class"], "level": r["level"], "ceiling": r["ceiling"],
              "set_by": r["set_by"], "at": iso(r["updated_at"]),
              "actions": [_action("Set level", "PUT", f"/v1/ambient/autonomy/{r['task_class']}",
                                  requires=STEP_UP, permission="autonomy:manage",
                                  body={"project_id": project_id})]} for r in levels]
    return {"key": "autonomy", "label": "Autonomy & budget", "icon": "gauge", "items": items,
            "ambient": ({"daily_cap_usd": round(int(amb["daily_cap_usd_micros"]) / 1_000_000, 2),
                         "max_concurrent": amb["max_concurrent"]} if amb else None),
            "budget": ({"daily_cap_usd": round(int(policy["daily_cap_usd_micros"] or 0) / 1e6, 2),
                        "monthly_cap_usd": round(int(policy["monthly_cap_usd_micros"] or 0) / 1e6, 2),
                        "intensity": policy["operational_intensity"]} if policy else None),
            "actions": [_action("Ambient budget", "PUT", "/v1/ambient/settings",
                                permission="autonomy:manage", body={"project_id": project_id}),
                        _action("Spending cap", "POST", "/v1/budget/set_intensity",
                                permission="budget:manage", body={"project_id": project_id})]}


def _repos(connection: Connection, project_id: str) -> dict[str, Any]:
    items = []
    for r in repos(connection, project_id):
        items.append({**r, "actions": [_action(
            "Remove", "DELETE", f"/v1/projects/{project_id}/repos/{r['binding_id']}",
            requires=STEP_UP, permission="repository:manage")]})
    return {"key": "repos", "label": "Repositories", "icon": "git-branch", "items": items,
            "actions": [_action("Add a repository", "PUT",
                                f"/v1/projects/{project_id}/repos/{{repo_id}}", requires=STEP_UP,
                                permission="repository:manage"),
                        _action("Repository mode", "PUT", f"/v1/projects/{project_id}/repo-mode",
                                permission="repository:manage")]}


def _notifications(connection: Connection, project_id: str) -> dict[str, Any]:
    rows = connection.execute(text(
        "SELECT id, name, contact_point_ids, quiet_hours FROM notification_policies "
        "WHERE project_id = :p ORDER BY position, name"), {"p": project_id}).mappings().all()
    items = [{"id": r["id"], "name": r["name"] or "Default",
              "contact_points": len(as_json(r["contact_point_ids"], []) or []),
              "quiet_hours": as_json(r["quiet_hours"], None),
              "actions": [_action("Edit", "PUT", f"/v1/notification-policies/{r['id']}",
                                  permission="observability:manage")]} for r in rows]
    return {"key": "notifications", "label": "Notifications", "icon": "bell", "items": items,
            "actions": [_action("Add a policy", "POST", "/v1/notification-policies",
                                permission="observability:manage",
                                body={"project_id": project_id})],
            "href": href(project_id, area="monitoring", view="notifications")}


def view(connection: Connection, project_id: str) -> dict[str, Any]:
    return {"project_id": project_id,
            "sections": [_demo_section(connection, project_id), _deploy_values(connection, project_id),
                         _targets(connection, project_id), _captain(connection, project_id),
                         _autonomy(connection, project_id), _repos(connection, project_id),
                         _notifications(connection, project_id)],
            "href": project_href(project_id, tab="settings")}


__all__ = ["view"]
