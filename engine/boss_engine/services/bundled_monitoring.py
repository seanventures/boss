"""The Prometheus and Grafana bundled with BOSS on the one-box (spec D1, topology T2).

    app /metrics ──scrape (app_metrics)──► Postgres floor (T0, BOSS's own records)
                                       └──remote write──► bundled Prometheus ──► bundled Grafana
    health probe ─────────────────────────remote write──┘        ▲
    open alerts ── ALERTS{alertname,alertstate,sev,…} ───────────┘ (dashboard annotations)

BOSS already scrapes every deployed app; the bundled Prometheus does not scrape anything
itself (it could not reach every app, and two scrapers would disagree). Instead each scrape's
RAW samples — the app's own series names, stamped with `app`, `environment`, `project` and
`target_id` — are forwarded through Prometheus' remote-write receiver, so Prometheus holds the
same series BOSS read. Health probes go the same way (`probe_success`), and every evaluation's
open alerts as Prometheus' own `ALERTS` series, which the dashboards draw as annotations.

Everything here is best effort and never fails its caller: the floor already holds what BOSS
needs to evaluate, and a Prometheus that does not answer only means the link is not shown.

Per-app dashboards are files in the Grafana container's provisioning directory (a volume BOSS
writes), regenerated when an app appears or changes; Grafana re-reads them every 30 s.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime
from typing import Any, Optional

from ..ports import sql

log = logging.getLogger(__name__)

#: How often open alerts are re-sent as ALERTS samples (Grafana draws a region per run).
ALERTS_EVERY_S = 30
#: How often dashboards and the bundled endpoints' recorded checks are refreshed.
DASHBOARDS_EVERY_S = 300
_LAST: dict[str, float] = {}


def prometheus_url(settings: Any) -> str:
    return str(getattr(settings, "monitoring_prometheus_url", "") or "").rstrip("/")


def _due(key: str, every: float) -> bool:
    now = time.monotonic()
    if now - _LAST.get(key, 0.0) < every:
        return False
    _LAST[key] = now
    return True


def stamp(target: dict) -> dict[str, str]:
    """BOSS's labels on every forwarded series; they win over the app's own."""
    return {"app": str(target.get("app_name") or ""),
            "environment": str(target.get("environment") or ""),
            "project": str(target.get("project_id") or ""),
            "target_id": str(target.get("id") or ""), "job": "boss",
            "instance": str(target.get("id") or "")}


def _send(settings: Any, series: list[tuple[dict[str, str], list[tuple[int, float]]]]) -> None:
    if not series or not prometheus_url(settings):
        return
    from .metric_ingest import _forward

    _forward(settings, series)


def scrape_series(target: dict, samples: list, at: datetime
                  ) -> list[tuple[dict[str, str], list[tuple[int, float]]]]:
    base = stamp(target)
    ms = int(at.timestamp() * 1000)
    out = []
    for s in samples:
        try:
            v = float(s.value)
        except (TypeError, ValueError):
            continue
        if math.isnan(v):
            continue
        labels = {**{str(k): str(x) for k, x in (s.labels or {}).items()}, **base,
                  "__name__": s.name}
        out.append((labels, [(ms, v)]))
    return out


def forward_scrape(settings: Any, target: dict, samples: list, at: datetime) -> None:
    """One scrape's raw samples to the bundled Prometheus. Never raises."""
    if not prometheus_url(settings) or not samples:
        return
    try:
        _send(settings, scrape_series(target, samples, at))
    except Exception:  # noqa: BLE001
        log.info("bundled: scrape forward failed", exc_info=True)


def forward_probe(settings: Any, target: dict, ok: bool, latency_ms: Optional[float],
                  at: datetime) -> None:
    """One health probe as probe_success (and probe_duration_seconds). Never raises."""
    if not prometheus_url(settings):
        return
    try:
        base, ms = stamp(target), int(at.timestamp() * 1000)
        series = [({**base, "__name__": "probe_success"}, [(ms, 1.0 if ok else 0.0)])]
        if latency_ms is not None:
            series.append(({**base, "__name__": "probe_duration_seconds"},
                           [(ms, float(latency_ms) / 1000.0)]))
        _send(settings, series)
    except Exception:  # noqa: BLE001
        log.info("bundled: probe forward failed", exc_info=True)


def alert_series(rows: list[dict], at: datetime
                 ) -> list[tuple[dict[str, str], list[tuple[int, float]]]]:
    """Open alerts as Prometheus' own ALERTS series (alertstate pending | firing)."""
    from . import severity as sev

    ms = int(at.timestamp() * 1000)
    out = []
    for r in rows:
        labels = r.get("labels")
        if isinstance(labels, str):
            labels = json.loads(labels or "{}")
        labels = labels or {}
        out.append(({"__name__": "ALERTS", "alertname": str(r["alertname"]),
                     "alertstate": str(r["state"]), "severity": str(labels.get("severity") or ""),
                     "sev": sev.level(labels), "app": str(r.get("app") or ""),
                     "environment": str(r.get("environment") or ""),
                     "project": str(r["project_id"]),
                     "sample": str(labels.get("sample") or "false")}, [(ms, 1.0)]))
    return out


def forward_alerts(engine, settings: Any, at: datetime) -> int:
    if not prometheus_url(settings) or not _due("alerts", ALERTS_EVERY_S):
        return 0
    from ..db import begin

    with begin(engine) as c:
        rows = [dict(r) for r in c.execute(sql(
            "SELECT a.alertname, a.state, a.labels, a.project_id, "
            " COALESCE(t.app_name, s.name) AS app, COALESCE(t.environment, s.environment) "
            " AS environment FROM app_alerts a "
            "LEFT JOIN deployment_targets t ON t.id = COALESCE(a.subject_id, a.target_id) "
            "LEFT JOIN monitored_services s ON s.id = COALESCE(a.subject_id, a.service_id) "
            "WHERE a.state IN ('pending', 'firing') LIMIT 2000")).mappings().all()]
    _send(settings, alert_series(rows, at))
    return len(rows)


# --------------------------------------------------------------------------- #
# Dashboards
# --------------------------------------------------------------------------- #
DS = {"type": "prometheus", "uid": "boss-prometheus"}


def _panel(pid: int, title: str, kind: str, grid: dict, targets: list[tuple[str, str]],
           unit: Optional[str] = None, extra: Optional[dict] = None) -> dict[str, Any]:
    p = {"id": pid, "title": title, "type": kind, "gridPos": grid, "datasource": DS,
         "targets": [{"expr": e, "legendFormat": legend, "refId": chr(65 + i), "datasource": DS}
                     for i, (e, legend) in enumerate(targets)],
         "fieldConfig": {"defaults": {**({"unit": unit} if unit else {})}, "overrides": []},
         "options": {"legend": {"showLegend": True}}}
    if extra:
        p.update(extra)
    return p


def render_dashboard(app: str, env: str, extra: Optional[list[dict[str, Any]]] = None) -> str:
    """One dashboard per app: requests, error rate, p50/p95 latency, health, alerts — with the
    app's alerts drawn as annotations. Panels say "No data" honestly before any arrives.
    `extra`: the app's own panels from its boss.deploy.yaml `dashboard:` (Set up monitoring
    writes them), drawn below, two to a row."""
    from .app_alerts import quote
    from .observability_stack import dashboard_uid

    sel = f"app={quote(app)},environment={quote(env)}"
    reqs = f"sum(rate(http_requests_total{{{sel}}}[5m]))"
    errs = f'sum(rate(http_requests_total{{{sel},status=~"5.."}}[5m]))'
    def lat(quantile: str) -> str:
        return (f"1000 * histogram_quantile({quantile}, sum by (le) "
                f"(rate(http_request_duration_seconds_bucket{{{sel}}}[5m])))")
    stat = {"options": {"colorMode": "value", "graphMode": "area",
                        "reduceOptions": {"calcs": ["lastNotNull"]}}}
    health_map = {"fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {
        "0": {"text": "NOT ANSWERING", "color": "red", "index": 0},
        "1": {"text": "answering", "color": "green", "index": 1}}}]}, "overrides": []},
        "options": {"colorMode": "background", "graphMode": "none",
                    "reduceOptions": {"calcs": ["lastNotNull"]}}}
    panels = [
        _panel(1, "Health (BOSS probe)", "stat", {"h": 5, "w": 6, "x": 0, "y": 0},
               [(f"max(probe_success{{{sel}}})", "health")], extra=health_map),
        _panel(2, "Requests / s", "stat", {"h": 5, "w": 6, "x": 6, "y": 0},
               [(reqs, "req/s")], "reqps", stat),
        _panel(3, "Error rate", "stat", {"h": 5, "w": 6, "x": 12, "y": 0},
               [(f"100 * {errs} / {reqs}", "5xx %")], "percent", stat),
        _panel(4, "p95 latency", "stat", {"h": 5, "w": 6, "x": 18, "y": 0},
               [(lat("0.95"), "p95")], "ms", stat),
        _panel(5, "Requests / s by route", "timeseries", {"h": 8, "w": 12, "x": 0, "y": 5},
               [(f"sum by (method, route) (rate(http_requests_total{{{sel}}}[5m]))",
                 "{{method}} {{route}}")], "reqps"),
        _panel(6, "Error rate (5xx %)", "timeseries", {"h": 8, "w": 12, "x": 12, "y": 5},
               [(f"100 * {errs} / {reqs}", "5xx %")], "percent"),
        _panel(7, "Latency p50 / p95", "timeseries", {"h": 8, "w": 12, "x": 0, "y": 13},
               [(lat("0.5"), "p50"), (lat("0.95"), "p95")], "ms"),
        _panel(8, "Health over time", "timeseries", {"h": 8, "w": 12, "x": 12, "y": 13},
               [(f"max(probe_success{{{sel}}})", "answering (1) / not (0)")], None,
               {"fieldConfig": {"defaults": {"min": 0, "max": 1}, "overrides": []}}),
        _panel(9, "Open alerts by severity", "timeseries", {"h": 8, "w": 24, "x": 0, "y": 21},
               [(f"count by (sev, alertname) (ALERTS{{{sel},alertstate=\"firing\"}})",
                 "{{sev}} {{alertname}}")], None,
               {"fieldConfig": {"defaults": {"custom": {"drawStyle": "bars"}}, "overrides": []}}),
    ]
    for i, p in enumerate(extra or []):
        panels.append(_panel(10 + i, str(p.get("title") or "Panel"),
                             "stat" if p.get("kind") == "stat" else "timeseries",
                             {"h": 8, "w": 12, "x": 12 * (i % 2), "y": 29 + 8 * (i // 2)},
                             [(str(p.get("expr") or ""), str(p.get("title") or ""))],
                             str(p.get("unit") or "") or None))
    annotations = {"list": [
        {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
         "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
         "name": "Annotations & Alerts", "type": "dashboard"},
        {"datasource": DS, "enable": True, "iconColor": "red", "name": "BOSS alerts (firing)",
         "expr": f'ALERTS{{{sel},alertstate="firing"}}', "step": "60s",
         "titleFormat": "{{sev}} {{alertname}}", "textFormat": "Firing in BOSS",
         "tagKeys": "sev,alertname", "useValueForTime": False},
        {"datasource": DS, "enable": False, "iconColor": "orange", "name": "BOSS alerts (pending)",
         "expr": f'ALERTS{{{sel},alertstate="pending"}}', "step": "60s",
         "titleFormat": "{{sev}} {{alertname}} pending", "tagKeys": "sev,alertname"}]}
    return json.dumps({
        "uid": dashboard_uid(app), "title": f"{app} · {env}", "tags": ["boss", env],
        "timezone": "browser", "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
        "schemaVersion": 39, "editable": False, "annotations": annotations, "panels": panels,
        "description": f"Generated by BOSS for {app} ({env}) from the series BOSS scrapes."},
        indent=1, sort_keys=True)


def write_dashboards(connection, settings: Any) -> list[str]:
    """Write (or refresh) one dashboard file per deployed app. Returns the uids written."""
    from .observability_stack import dashboard_uid

    folder = str(getattr(settings, "monitoring_dashboards_dir", "") or "")
    if not folder or not os.path.isdir(folder):
        return []
    from .app_alerts import _manifest

    rows = connection.execute(sql(
        "SELECT DISTINCT ON (app_name, environment) id, app_name, environment "
        "FROM deployment_targets WHERE status = 'active' "
        "AND app_name IS NOT NULL ORDER BY app_name, environment, created_at")).all()
    wrote: list[str] = []
    seen: set[str] = set()
    for tid, app, env in rows:
        uid = dashboard_uid(str(app))
        if uid in seen:          # one dashboard per app name (the uid the links use)
            continue
        seen.add(uid)
        extra = ((_manifest(connection, str(tid)).get("dashboard") or {}).get("panels")) or []
        body = render_dashboard(str(app), str(env or ""), extra)
        path = os.path.join(folder, f"{uid}.json")
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == body:
                    continue
        except OSError:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
        wrote.append(uid)
    return wrote


def tick(engine, settings: Any, now: datetime) -> dict[str, Any]:
    """The monitor tick's share: ALERTS samples, dashboards, and the recorded checks the
    console's Grafana / Dashboard links are built from (asked, never assumed)."""
    from ..db import begin
    from .observability_stack import check_endpoint

    out: dict[str, Any] = {}
    if not prometheus_url(settings) and not getattr(settings, "monitoring_grafana_url", ""):
        return out
    out["alerts"] = forward_alerts(engine, settings, now)
    if _due("dashboards", DASHBOARDS_EVERY_S):
        with begin(engine) as c:
            out["dashboards"] = write_dashboards(c, settings)
            pids = c.execute(sql("SELECT DISTINCT project_id FROM deployment_targets "
                                 "WHERE status = 'active'")).scalars().all()
        if out["dashboards"]:
            # Grafana re-reads its files every 30 s: ask it about them on a later tick, so the
            # recorded check (what every link is built from) sees the new dashboards.
            _LAST["dashboards"] = time.monotonic() - DASHBOARDS_EVERY_S + 45
            pids = []
        for pid in pids:
            for kind in ("grafana", "prometheus"):
                try:
                    check_endpoint(engine, pid, kind, only_if_stale=True)
                except Exception:  # noqa: BLE001
                    log.info("bundled: %s check failed", kind, exc_info=True)
    return out


__all__ = ["alert_series", "forward_alerts", "forward_probe", "forward_scrape",
           "render_dashboard", "scrape_series", "stamp", "tick", "write_dashboards"]
