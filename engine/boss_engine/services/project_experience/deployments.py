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
from .common import href, iso, project_href, web_url
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


#: A deploy's state in words (the raw status stays in `status`): Infra's own reading
#: (infra_view.deploy_status), so the two screens never disagree.
def _state_words(status: Any, decision_status: Any) -> tuple[str, str]:
    from ..infra_view import deploy_status

    try:
        got = deploy_status(status, decision_status=decision_status)
        return str(got.get("word") or status), str(got.get("tone") or "grey")
    except Exception:  # noqa: BLE001 — words are a nicety; the raw state is still there
        return str(status or "").replace("_", " ").capitalize(), "grey"


def _pr_by_commit(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    """commit → {number, title}: the pull request whose recorded merge it is, and its short
    title (titles.short_title of the build's title and brief)."""
    from .common import pr_number
    from .titles import short_title

    out: dict[str, dict[str, Any]] = {}
    for r in connection.execute(text(
            "SELECT finalize_ctx->'merge'->>'sha' AS sha, pull_request_number AS n, "
            "pull_request_url AS u, title, spec->>'instructions' AS brief FROM code_task_runs "
            "WHERE project_id = :p AND COALESCE(finalize_ctx->'merge'->>'sha', '') <> '' "
            "ORDER BY created_at"), {"p": project_id}).mappings():
        n = r["n"] or pr_number(r["u"])
        if n and str(r["sha"]) not in out:
            out[str(r["sha"])] = {"number": int(n), "url": r["u"],
                                  "title": short_title(r["title"], r["brief"])}
    return out


def _duration(start: Any, end: Any) -> Optional[str]:
    if start is None or end is None:
        return None
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _history(connection: Connection, project_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Recent deploys in words (screen 08): "Staging · PR #2 UI refresh", "Wed 16:40 · 3m 10s ·
    smoke ✓", Live / Needs you / Failed, WHO by name (people_names), never a principal id or a
    raw status. A request that never ran because a later deploy overtook it (superseded) is
    not history."""
    from ..people_names import who

    rows = connection.execute(text(
        "SELECT s.id, s.target_id, s.commit_sha, s.status, s.created_by, s.created_at, "
        "s.apply_started_at, s.finished_at, s.failure_reason, s.rollback_of, "
        "t.environment, t.app_name, d.status AS decision_status, d.approved_by "
        "FROM stack_deployments s JOIN deployment_targets t ON t.id = s.target_id "
        "LEFT JOIN decisions d ON d.id = s.decision_id "
        "WHERE s.project_id = :p ORDER BY s.created_at DESC LIMIT :n"),
        {"p": project_id, "n": limit * 3}).mappings().all()
    prs = _pr_by_commit(connection, project_id)
    out = []
    newest: set[str] = set()
    live_after: dict[str, Any] = {}
    firsts = {str(t): at for t, at in connection.execute(text(
        "SELECT target_id, min(created_at) FROM stack_deployments WHERE project_id = :p "
        "GROUP BY target_id"), {"p": project_id}).all()}
    for r in rows:
        if r["status"] == "healthy":
            t = str(r["target_id"])
            live_after[t] = max(live_after.get(t) or r["created_at"], r["created_at"])
    for r in rows:
        word, tone = _state_words(r["status"], r["decision_status"])
        if word == "Superseded":
            continue
        if r["status"] == "healthy":
            if r["target_id"] in newest:
                word = "Done"
            newest.add(r["target_id"])
        pr = prs.get(str(r["commit_sha"] or ""))
        first_ever = r["created_at"] == firsts.get(str(r["target_id"]))
        what = (f"PR #{pr['number']} {pr['title']}".strip() if pr else
                "rollback" if r["rollback_of"] else "first deploy" if first_ever
                else (r["commit_sha"] or "")[:7])
        actor = r["approved_by"] or r["created_by"]
        state = ("done" if r["status"] == "healthy" else "failed" if r["status"] == "failed"
                 else "waiting" if r["status"] == "awaiting_approval" else
                 "skipped" if r["status"] == "rejected" else "now")
        recovered = (r["status"] == "failed" and live_after.get(str(r["target_id"])) is not None
                     and live_after[str(r["target_id"])] > r["created_at"])
        if recovered:
            state = "recovered"
        took = _duration(r["apply_started_at"], r["finished_at"])
        smoke = ("smoke ✓" if r["status"] == "healthy" else
                 "smoke ✗" if r["status"] == "failed" and r["apply_started_at"] else None)
        reason = None if r["status"] in ("healthy", "awaiting_approval") else \
            (" ".join(str(r["failure_reason"] or "").split())[:110].rstrip(".") or None)
        out.append({"id": r["id"], "label": f"{env_label(r['environment'])} · {what}".strip(" ·"),
                    "env": r["environment"], "app": r["app_name"], "status": r["status"],
                    "state": state, "state_word": word, "tone": tone,
                    "pr": pr["number"] if pr else None, "title": pr["title"] if pr else None,
                    "commit": (r["commit_sha"] or "")[:7] or None,
                    "duration": None if recovered else took, "smoke": None if recovered else smoke,
                    "reason": reason, "recovered": recovered,
                    "by": who(actor, connection) if actor else None,
                    "by_agent": str(actor or "") in ("boss", "auto_approval") or
                    str(actor or "").startswith(("agt_", "policy:")),
                    "at": iso(r["finished_at"] or r["created_at"]),
                    "href": href(project_id, area="infra", view="deploys", deploy=f"stack:{r['id']}")})
        if len(out) >= limit:
            break
    return out


def host_label(name: Any, address: Any, environment: Any = None) -> str:
    """A server's NAME for a title: its own name, unless that is only its address (an IP or a
    bare host), then "<Env> server"; the address stays a mono sub-line beside it."""
    import re as _re

    n = str(name or "").strip()
    a = str(address or "").strip()
    if n and n != a and not _re.match(r"^\d{1,3}(\.\d{1,3}){3}$", n):
        return n
    return f"{env_label(environment)} server" if environment else "Server"


def _who_name(connection: Connection, actor: Any) -> Optional[str]:
    from ..people_names import who

    return who(actor, connection) if actor else None


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
    # ONE SOURCE for uptime and p95: the Health tab's environments (the app's own p95 when it
    # reports one, else the probe's), so Overview, Health and Deployments show the same number.
    from .health import environments as health_environments

    same = {e["target_id"]: e for e in health_environments(connection, project_id)}
    last = _last_deploys(connection, [t["id"] for t in targets])
    prs = _pr_by_commit(connection, project_id)
    host_ids = sorted({str(t["host_id"]) for t in targets if t["host_id"]})
    shared = {str(r["host_id"]): int(r["n"]) for r in connection.execute(text(
        "SELECT host_id, count(DISTINCT project_id) AS n FROM deployment_targets "
        "WHERE host_id = ANY(:h) AND status = 'active' GROUP BY host_id"),
        {"h": host_ids}).mappings()} if host_ids else {}
    logins = demo.listing(connection, project_id)
    by_env = {e["key"]: e["logins"] for e in logins["environments"]}
    apps = {t["app_name"] for t in targets}
    envs = []
    for t in targets:
        h = health.get(t["id"], {})
        one = same.get(t["id"], {})
        d = last.get(t["id"])
        domains = t["domains"] if isinstance(t["domains"], list) else []
        url = web_url((d or {}).get("open_url") or t.get("public_url")
                      or (domains[0] if domains else None))
        label = env_label(t["environment"])
        if len(apps) > 1:
            label = f"{t['app_name']} {label}"
        is_prod = str(t["environment"] or "").lower() in PRODUCTION
        envs.append({
            "key": t["environment"], "label": label, "app": t["app_name"], "target_id": t["id"],
            "url": url, "health": health_word(h.get("status")), "status": h.get("status"),
            "uptime_pct": one.get("uptime_pct", h.get("uptime_7d")),
            "p95_ms": one.get("p95_ms", h.get("p95_ms")),
            "p95_source": one.get("p95_source", "probe"),
            "error_rate": one.get("error_rate"),
            # The 24 h strip: each point's share of good answers (1 = all good).
            "series": [round(max(0.0, 1 - float(v) / 100.0), 3)
                       for v in ((one.get("series") or {}).get("errors") or [])],
            "host": t["host_address"] or t["host_name"], "host_status": t["host_status"],
            "host_name": host_label(t["host_name"], t["host_address"], t["environment"]),
            "host_projects": shared.get(str(t["host_id"]), 1) if t["host_id"] else None,
            # The server, in Infra (shared across projects): its drawer, which links back here.
            "host_id": t["host_id"],
            "host_href": href(project_id, area="infra", host=t["host_id"]) if t["host_id"] else None,
            "last_deploy": ({"commit": (d["commit_sha"] or "")[:7] or None, "status": d["status"],
                             "pr": (prs.get(str(d["commit_sha"] or "")) or {}).get("number"),
                             "pr_url": (prs.get(str(d["commit_sha"] or "")) or {}).get("url"),
                             "at": iso(d["finished_at"] or d["created_at"]),
                             "by": _who_name(connection, d["created_by"]),
                             "href": href(project_id, area="infra", view="deploys",
                                          deploy=f"stack:{d['id']}")} if d else None),
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
        # Infra opens its own + Add sheet from `add=` (host, or an app target; with no machine
        # yet it asks for the host first).
        production = ({"state": "needs_host",
                       "action": {"label": "Add a host", "href": href(project_id, area="infra",
                                                                       add="host")}}
                      if not hosts else
                      {"state": "none",
                       "action": {"label": "Set up production",
                                  "href": href(project_id, area="infra", add="target")}})
    preview = next((e for e in logins["environments"] if e["key"] == "preview"), None)
    out = {"environments": envs, "production": production, "ship_path": ship_path(envs),
           "preview": ({"demo_logins": preview["logins"]} if preview else None),
           "demo_logins": logins, "history": _history(connection, project_id)}
    if not envs:
        out["empty"] = {"label": "Not deployed yet",
                        "detail": "The first merged work deploys to staging by the project's "
                                  "policy; production always waits for a person.",
                        "href": href(project_id, area="infra", add="target")}
    return out


def ship_path(envs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """How code ships, as a strip (screen 8): merge → build → the first non-production
    environment (auto or a person, from its target's policy) → its health check → production,
    always a person's approval. States come from what the environments show today."""
    pre = next((e for e in envs if str(e["key"] or "").lower() not in PRODUCTION), None)
    prod = next((e for e in envs if str(e["key"] or "").lower() in PRODUCTION), None)
    shipped = bool(pre and pre.get("last_deploy"))
    nodes = [{"label": "PR merged", "state": "done" if shipped else "todo"},
             {"label": "Image built", "state": "done" if shipped else "todo"}]
    if pre is None:
        nodes.append({"label": "Staging · not set", "state": "todo"})
        nodes.append({"label": "Smoke test", "state": "todo"})
    else:
        who = "auto" if pre["policy"] == "auto" else "you approve"
        nodes.append({"label": f"{env_label(pre['key'])} · {who}",
                      "state": "done" if shipped else ("waiting" if who != "auto" else "todo")})
        nodes.append({"label": "Smoke test", "state": {"healthy": "done", "down": "failed"}.get(
            pre["health"], "todo") if shipped else "todo"})
    nodes.append({"label": "Prod · you approve",
                  "state": "done" if prod and prod.get("last_deploy") else "waiting"})
    return nodes


def live_url(envs: list[dict[str, Any]]) -> Optional[str]:
    """The URL the header chip opens: staging first, then any non-production, then prod."""
    ordered = sorted(envs, key=lambda e: (str(e["key"]).lower() not in ("staging", "stage"),
                                          str(e["key"]).lower() in PRODUCTION))
    return next((e["url"] for e in ordered if e.get("url")), None)


__all__ = ["PRODUCTION", "host_label", "live_url", "ship_path", "view"]
