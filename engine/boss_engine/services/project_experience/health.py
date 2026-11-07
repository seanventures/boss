"""Health tab (contract §12): one project's environments, alerts, incidents and monitoring.

Built only from what BOSS already records:
  environments   deployment_health.health_view (the probe samples: status, uptime, p95, series)
                 + app_metrics_scrapes (the app's own requests/errors/p95, when it exposes them)
  alerts         app_alerts (the alert engine's rows for this project)
  incidents      the incident squad's work items (ambient board, task classes incident_*), each
                 linked to its Agents-at-work graph; open incidents the squad never picked up too
  pack           app_alert_rules (Beacon's monitoring pack lives in boss.deploy.yaml: source yaml)
  links          observability_stack.endpoint_state (a Grafana link only when it answered)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import as_json, href, iso, project_href, words

#: The availability objective an environment is held to when nobody set one.
DEFAULT_SLO = 99.5
#: Points in each sparkline.
POINTS = 30

_HEALTH = {"up": "healthy", "degraded": "degraded", "down": "down"}
_SEVERITY = {"sev1": "page", "sev2": "page", "sev3": "ticket", "sev4": "info"}
_ENV_LABELS = {"preview": "Preview", "staging": "Staging", "stage": "Staging",
               "production": "Production", "prod": "Production", "dev": "Dev",
               "development": "Dev", "test": "Test", "qa": "QA"}


def env_label(environment: Any) -> str:
    key = str(environment or "").strip().lower()
    return _ENV_LABELS.get(key) or (key[:1].upper() + key[1:] if key else "Environment")


def health_word(status: Any) -> str:
    """deployment_health's verdict → the shared `health` enum."""
    return _HEALTH.get(str(status or ""), "unknown")


def burn(uptime: Optional[float], target: float = DEFAULT_SLO) -> str:
    """ok | warning | breach: the objective met, within half a point of it, or missed."""
    if uptime is None:
        return "ok"
    if uptime >= target:
        return "ok"
    if uptime >= target - 0.5:
        return "warning"
    return "breach"


def _metrics(connection: Connection, target_ids: list[str], since: datetime
             ) -> dict[str, dict[str, Any]]:
    """Per target: the app's own error rate (%) and p95 over 24 h, and the newest points."""
    if not target_ids:
        return {}
    rows = connection.execute(text(
        "SELECT deployment_target_id AS t, scraped_at, requests, errors, p95_ms FROM ("
        " SELECT deployment_target_id, scraped_at, requests, errors, p95_ms, row_number() OVER ("
        "  PARTITION BY deployment_target_id ORDER BY scraped_at DESC) AS n "
        " FROM app_metrics_scrapes WHERE deployment_target_id = ANY(:ids) AND ok "
        " AND scraped_at >= :since) s WHERE n <= 200 ORDER BY scraped_at"),
        {"ids": target_ids, "since": since}).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        m = out.setdefault(str(r["t"]), {"requests": 0.0, "errors": 0.0, "p95": [], "errors_s": []})
        req, err = float(r["requests"] or 0), float(r["errors"] or 0)
        m["requests"] += req
        m["errors"] += err
        if r["p95_ms"] is not None:
            m["p95"].append(float(r["p95_ms"]))
        m["errors_s"].append(round(err * 100.0 / req, 2) if req else 0.0)
    for m in out.values():
        m["error_rate"] = round(m["errors"] * 100.0 / m["requests"], 2) if m["requests"] else None
        m["p95_ms"] = round(sorted(m["p95"])[int(0.95 * (len(m["p95"]) - 1))]) if m["p95"] else None
        m["latency_s"] = [round(v) for v in m["p95"][-POINTS:]]
        m["errors_s"] = m["errors_s"][-POINTS:]
    return out


def environments(connection: Connection, project_id: str, *,
                 now: Optional[datetime] = None) -> list[dict[str, Any]]:
    from ..deployment_health import health_view

    now = now or datetime.now(timezone.utc)
    view = health_view(connection, project_id, now=now, points=POINTS)
    targets = view.get("targets") or []
    metrics = _metrics(connection, [t["target_id"] for t in targets], now - timedelta(hours=24))
    hosts = {str(r["id"]): dict(r) for r in connection.execute(text(
        "SELECT t.id, t.host_id, h.address, h.name FROM deployment_targets t "
        "LEFT JOIN deployment_hosts h ON h.id = t.host_id WHERE t.id = ANY(:ids)"),
        {"ids": [t["target_id"] for t in targets]}).mappings()} if targets else {}
    apps = {t["app_name"] for t in targets}
    out = []
    for t in targets:
        m = metrics.get(t["target_id"], {})
        uptime = t.get("uptime_7d")
        label = env_label(t["environment"])
        if len(apps) > 1:
            label = words(f"{t['app_name']} {label}")
        latency = m.get("latency_s") or [s["ms"] for s in t.get("series") or []
                                         if s.get("ms") is not None][-POINTS:]
        errors = m.get("errors_s") or [0.0 if s.get("ok") else 100.0
                                       for s in t.get("series") or []][-POINTS:]
        out.append({
            "key": str(t["environment"]), "label": label, "app": t["app_name"],
            "target_id": t["target_id"], "url": t.get("url"),
            "health": health_word(t["status"]), "status": t["status"],
            "uptime_pct": uptime, "error_rate": m.get("error_rate"),
            "p95_ms": m.get("p95_ms") if m.get("p95_ms") is not None else t.get("p95_ms"),
            "series": {"errors": errors, "latency": latency},
            "slo": {"target": DEFAULT_SLO, "burn": burn(t.get("uptime_24h"))},
            "last_checked": t.get("last_checked"), "last_error": t.get("last_error"),
            "deployed_commit": (t.get("deployed_commit") or "")[:7] or None,
            "deployed_at": t.get("deployed_at"),
            "host": (hosts.get(str(t["target_id"])) or {}).get("address")
            or (hosts.get(str(t["target_id"])) or {}).get("name"),
            "host_href": (href(project_id, area="infra",
                               host=hosts[str(t["target_id"])]["host_id"])
                          if (hosts.get(str(t["target_id"])) or {}).get("host_id") else None),
            "href": project_href(project_id, tab="health", drawer=f"env:{t['target_id']}"),
        })
    return out


def alerts(connection: Connection, project_id: str, *, now: Optional[datetime] = None,
           limit: int = 20) -> list[dict[str, Any]]:
    from .. import severity as sev

    now = now or datetime.now(timezone.utc)
    rows = connection.execute(text(
        "SELECT a.id, a.alertname, a.labels, a.annotations, a.state, a.active_at, a.fired_at, "
        "a.cleared_at, a.incident_id, t.app_name, t.environment FROM app_alerts a "
        "LEFT JOIN deployment_targets t ON t.id = COALESCE(a.subject_id, a.target_id) "
        "WHERE a.project_id = :p AND (a.state IN ('pending', 'firing') OR a.active_at >= :since "
        "OR a.cleared_at >= :since) ORDER BY (a.state <> 'resolved') DESC, "
        "COALESCE(a.fired_at, a.active_at) DESC LIMIT :n"),
        {"p": project_id, "since": now - timedelta(days=7), "n": limit}).mappings().all()
    out = []
    for r in rows:
        labels = as_json(r["labels"], {}) or {}
        ann = as_json(r["annotations"], {}) or {}
        out.append({
            "id": r["id"], "label": words(ann.get("summary") or r["alertname"], 6),
            "detail": ann.get("description") or ann.get("summary") or r["alertname"],
            "severity": _SEVERITY.get(sev.level(labels), "ticket"),
            "state": "resolved" if r["state"] == "resolved" else "firing",
            "pending": r["state"] == "pending",
            "since": iso(r["fired_at"] or r["active_at"]), "cleared_at": iso(r["cleared_at"]),
            "app": r["app_name"], "env": r["environment"], "incident_id": r["incident_id"],
            "href": href(project_id, area="observability", view="alerts", alert=r["id"]),
        })
    return out


#: The squad's lifecycle as a 4-station strip: Alert → Triage → Fix → Verify.
def _mini(lifecycle: str, outcome: Optional[str]) -> list[dict[str, str]]:
    done_to = {"proposed": 1, "claimed": 1, "in_progress": 2, "needs_you": 2,
               "resolved": 4, "dismissed": 4}.get(lifecycle, 1)
    names = ("Alert", "Triage", "Fix", "Verify")
    out = []
    for i, name in enumerate(names):
        if i < done_to:
            state = "done"
        elif i == done_to:
            state = "waiting" if lifecycle == "needs_you" else "now"
        else:
            state = "todo"
        out.append({"label": name, "state": state})
    return out


def incidents(connection: Connection, project_id: str, *, limit: int = 10
              ) -> list[dict[str, Any]]:
    from ..incident_squad import INCIDENT_CLASSES

    rows = connection.execute(text(
        "SELECT w.id, w.title, w.lifecycle, w.outcome, w.app, w.created_at, w.updated_at, "
        "(SELECT l.ref FROM work_item_links l WHERE l.work_item_id = w.id AND l.kind = 'incident' "
        " LIMIT 1) AS incident_id FROM work_items w WHERE w.project_id = :p AND w.ambient "
        "AND w.task_class = ANY(:classes) ORDER BY (w.lifecycle IN ('resolved', 'dismissed')), "
        "w.updated_at DESC LIMIT :n"),
        {"p": project_id, "classes": list(INCIDENT_CLASSES), "n": limit}).mappings().all()
    out = []
    covered: set[str] = set()
    for r in rows:
        life = str(r["lifecycle"] or "proposed")
        state = ("resolved" if life in ("resolved", "dismissed") else
                 "needs_you" if life == "needs_you" else "running")
        if r["incident_id"]:
            covered.add(str(r["incident_id"]))
        out.append({"id": r["id"], "label": words(r["title"], 6), "detail": r["title"],
                    "state": state, "app": r["app"], "since": iso(r["created_at"]),
                    "incident_id": r["incident_id"],
                    "graph_href": href(project_id, area="agent_ops", item=r["id"]),
                    "mini": _mini(life, r["outcome"])})
    open_rows = connection.execute(text(
        "SELECT id, title, first_seen FROM incidents WHERE project_id = :p "
        "AND status NOT IN ('resolved', 'dismissed') ORDER BY last_seen DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()
    for r in open_rows:
        if str(r["id"]) in covered:
            continue
        out.append({"id": r["id"], "label": words(r["title"], 6), "detail": r["title"],
                    "state": "needs_you", "app": None, "since": iso(r["first_seen"]),
                    "incident_id": r["id"],
                    "graph_href": href(project_id, area="observability", inc=r["id"]),
                    "mini": _mini("needs_you", None)})
    return out[:limit]


def monitoring_pack(connection: Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT count(*) FILTER (WHERE enabled) AS rules, "
        "count(*) FILTER (WHERE enabled AND source = 'yaml') AS repo, "
        "count(*) FILTER (WHERE enabled AND source = 'console') AS console "
        "FROM app_alert_rules WHERE project_id = :p"), {"p": project_id}).mappings().first()
    rules = int(row["rules"] or 0) if row else 0
    repo = int(row["repo"] or 0) if row else 0
    managed = "none" if not rules else "repo" if repo else "boss"
    return {"rules": rules, "managed_in": managed,
            # Observability › Alerts: every app's rules and what fired (no `rules` view exists).
            "href": href(project_id, area="observability", view="alerts")}


def links(connection: Connection, project_id: str, apps: list[str]) -> dict[str, Any]:
    from ..observability_stack import endpoint_state

    try:
        with connection.begin_nested():
            grafana = endpoint_state(connection, project_id, "grafana")
    except Exception:  # noqa: BLE001 — a link is a help, never a failed tab
        grafana = {"state": "unknown", "url": None, "reason": None}
    return {"grafana": grafana.get("url") if grafana.get("state") == "answering" else None,
            "grafana_state": grafana.get("state"), "grafana_reason": grafana.get("reason") or None,
            # The deploy log (Infra › Deploys); Observability has no logs view of its own.
            "logs": href(project_id, area="infra", view="deploys"),
            "observability": href(project_id, area="observability",
                                  app=apps[0] if len(apps) == 1 else None)}


def view(connection: Connection, project_id: str, *, now: Optional[datetime] = None
         ) -> dict[str, Any]:
    envs = environments(connection, project_id, now=now)
    pack = monitoring_pack(connection, project_id)
    out = {"environments": envs, "alerts": alerts(connection, project_id, now=now),
           "incidents": incidents(connection, project_id), "monitoring_pack": pack,
           "links": links(connection, project_id, sorted({e["app"] for e in envs}))}
    if not envs:
        out["empty"] = {"label": "Nothing deployed yet",
                        "detail": "Health appears after the first deploy: Beacon adds a "
                                  "monitoring pack to the app's boss.deploy.yaml when it ships.",
                        "href": project_href(project_id, tab="deployments")}
    elif not pack["rules"]:
        out["empty"] = {"label": "No alert rules yet",
                        "detail": "Beacon writes the monitoring pack on the next deploy; until "
                                  "then BOSS only checks that the app answers.",
                        "href": pack["href"]}
    return out


def rollup(envs: list[dict[str, Any]]) -> str:
    """The worst health across environments (for the header dot and project cards)."""
    order = ("down", "degraded", "healthy")
    for word in order:
        if any(e["health"] == word for e in envs):
            return word
    return "unknown"


__all__ = ["DEFAULT_SLO", "alerts", "burn", "env_label", "environments", "health_word",
           "incidents", "links", "monitoring_pack", "rollup", "view"]
