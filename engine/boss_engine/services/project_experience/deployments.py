"""Deployments tab (contract §5): each environment of the project with its URL, health, last
deploy, demo logins and release policy, and whether production is set up.

Reads deployment_targets, deployment_health.health_view (the probe samples), stack_deployments
(the newest deploy, who and which commit), deployment_hosts (where it runs) and the demo-login
listing (names only). Production is never automatic: a target in production is `person`.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import demo_logins as demo
from .common import href, iso, project_href
from .health import env_label, health_word

PRODUCTION = ("production", "prod")


def _last_deploys(connection: Connection, target_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not target_ids:
        return {}
    rows = connection.execute(text(
        "SELECT DISTINCT ON (target_id) id, target_id, commit_sha, ref, status, created_by, "
        "created_at, finished_at, open_url, workflow_run_id FROM stack_deployments "
        "WHERE target_id = ANY(:ids) ORDER BY target_id, created_at DESC"),
        {"ids": target_ids}).mappings().all()
    return {str(r["target_id"]): dict(r) for r in rows}


def _history(connection: Connection, project_id: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT s.id, s.commit_sha, s.status, s.created_by, s.created_at, s.finished_at, "
        "t.environment, t.app_name FROM stack_deployments s JOIN deployment_targets t "
        "ON t.id = s.target_id WHERE s.project_id = :p ORDER BY s.created_at DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()
    return [{"id": r["id"], "label": f"{env_label(r['environment'])} {(r['commit_sha'] or '')[:7]}"
             .strip(), "env": r["environment"], "app": r["app_name"], "status": r["status"],
             "commit": (r["commit_sha"] or "")[:7] or None, "by": r["created_by"],
             "at": iso(r["finished_at"] or r["created_at"]),
             "href": href(project_id, area="infra", view="deploys", deploy=r["id"])}
            for r in rows]


def view(connection: Connection, project_id: str) -> dict[str, Any]:
    from ..deployment_health import health_view

    targets = [dict(r) for r in connection.execute(text(
        "SELECT t.id, t.app_name, t.environment, t.domains, t.public_url, t.auto_deploy, t.paused, "
        "t.host_id, h.name AS host_name, h.address AS host_address, h.status AS host_status "
        "FROM deployment_targets t LEFT JOIN deployment_hosts h ON h.id = t.host_id "
        "WHERE t.project_id = :p AND t.status = 'active' ORDER BY "
        "(lower(t.environment) IN ('production', 'prod')), t.environment, t.app_name"),
        {"p": project_id}).mappings().all()]
    health = {t["target_id"]: t for t in health_view(connection, project_id).get("targets") or []}
    last = _last_deploys(connection, [t["id"] for t in targets])
    logins = demo.listing(connection, project_id)
    by_env = {e["key"]: e["logins"] for e in logins["environments"]}
    apps = {t["app_name"] for t in targets}
    envs = []
    for t in targets:
        h = health.get(t["id"], {})
        d = last.get(t["id"])
        domains = t["domains"] if isinstance(t["domains"], list) else []
        url = (d or {}).get("open_url") or t.get("public_url") or (domains[0] if domains else None)
        label = env_label(t["environment"])
        if len(apps) > 1:
            label = f"{t['app_name']} {label}"
        is_prod = str(t["environment"] or "").lower() in PRODUCTION
        envs.append({
            "key": t["environment"], "label": label, "app": t["app_name"], "target_id": t["id"],
            "url": url, "health": health_word(h.get("status")), "status": h.get("status"),
            "uptime_pct": h.get("uptime_7d"), "p95_ms": h.get("p95_ms"),
            "host": t["host_address"] or t["host_name"], "host_status": t["host_status"],
            "last_deploy": ({"commit": (d["commit_sha"] or "")[:7] or None, "status": d["status"],
                             "at": iso(d["finished_at"] or d["created_at"]),
                             "by": d["created_by"],
                             "href": href(project_id, area="infra", view="deploys",
                                          deploy=d["id"])} if d else None),
            "demo_logins": by_env.get(t["environment"]) or [],
            "policy": "person" if is_prod or not t["auto_deploy"] else "auto",
            "paused": bool(t["paused"]),
            "href": project_href(project_id, tab="deployments", drawer=f"env:{t['id']}"),
            "manage_href": href(project_id, area="infra", target=t["id"]),
        })
    has_prod = any(str(e["key"] or "").lower() in PRODUCTION for e in envs)
    if has_prod:
        production: dict[str, Any] = {"state": "ready", "action": None}
    else:
        from ..deployment_fleet import usable_hosts_sql

        hosts = int(connection.execute(text(
            f"SELECT count(*) FROM deployment_hosts WHERE status = 'verified' AND "
            f"{usable_hosts_sql()}"), {"p": project_id}).scalar() or 0)
        production = ({"state": "needs_host",
                       "action": {"label": "Add a host", "href": href(project_id, area="infra",
                                                                       view="hosts")}}
                      if not hosts else
                      {"state": "none",
                       "action": {"label": "Set up production",
                                  "href": href(project_id, area="infra", view="targets",
                                               new="production")}})
    preview = next((e for e in logins["environments"] if e["key"] == "preview"), None)
    out = {"environments": envs, "production": production,
           "preview": ({"demo_logins": preview["logins"]} if preview else None),
           "demo_logins": logins, "history": _history(connection, project_id)}
    if not envs:
        out["empty"] = {"label": "Not deployed yet",
                        "detail": "The first merged work deploys to staging by the project's "
                                  "policy; production always waits for a person.",
                        "href": href(project_id, area="infra", view="targets")}
    return out


def live_url(envs: list[dict[str, Any]]) -> Optional[str]:
    """The URL the header chip opens: staging first, then any non-production, then prod."""
    ordered = sorted(envs, key=lambda e: (str(e["key"]).lower() not in ("staging", "stage"),
                                          str(e["key"]).lower() in PRODUCTION))
    return next((e["url"] for e in ordered if e.get("url")), None)


__all__ = ["PRODUCTION", "live_url", "view"]
