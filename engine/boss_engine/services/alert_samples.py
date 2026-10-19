"""SAMPLE alert rules, and the metric names the New alert rule builder offers.

Samples are ordinary custom PromQL rules, created through the same `update_rule` path a person
uses (validated, audited), and marked twice so nobody mistakes them for real ones:
`labels.sample = "true"` (the console shows a Sample chip; routing can match it) and a summary
that starts with "SAMPLE:". `remove` deletes every one of an app's samples in one call; their
alert history stays, marked by the same label. Nothing here writes an alert: alerts come only
from the evaluator reading real metrics.

The set is chosen per app from what it actually exports (its scraped metric families): an
error-budget burn, p95 latency, health check, no traffic, a dead-man's-switch heartbeat, and —
when the app records customer tickets — a customer-signal rule.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from ..ports import sql
from . import app_alerts as aa
from . import monitoring as mon

SAMPLE_PREFIX = "SAMPLE: "


def _sel(target: dict) -> str:
    return aa.selector(target["app_name"], target["environment"])


def families(connection, target: dict) -> list[dict[str, Any]]:
    """The metric families BOSS last saw from this app: what its scrape recorded on its
    monitored service, plus any series it pushed to the floor."""
    sid = mon.service_for_target(connection, target["id"]) if target.get("id") else None
    sid = sid or target.get("service_id")
    out: dict[str, dict[str, Any]] = {}
    if sid:
        svc = mon.service(connection, target["project_id"], sid) or {}
        for f in (svc.get("onboarding") or {}).get("metric_families") or []:
            if isinstance(f, dict) and f.get("name"):
                out[str(f["name"])] = {"name": str(f["name"]),
                                       "type": str(f.get("type") or "unknown"),
                                       "labels": sorted(str(x) for x in f.get("labels") or []),
                                       "help": f.get("help") or None}
        for name, labels in connection.execute(sql(
                "SELECT name, array_agg(DISTINCT k) FROM metric_series s, "
                " jsonb_object_keys(s.labels) k WHERE s.service_id = :s GROUP BY name"),
                {"s": sid}).all():
            out.setdefault(str(name), {"name": str(name), "type": "unknown", "help": None,
                                       "labels": sorted(set(labels or []) - set(_STAMPED))})
    return sorted(out.values(), key=lambda f: f["name"])


#: Labels BOSS stamps on every series; not offered as filters (they are the selector).
_STAMPED = ("__name__", "project", "service_id", "service", "app", "environment", "target_id",
            "job", "instance")


def metric_names(connection, target: dict) -> dict[str, Any]:
    fams = families(connection, target)
    if not any(f["name"] == "probe_success" for f in fams):
        fams.append({"name": "probe_success", "type": "gauge", "labels": [],
                     "help": "BOSS's health probe of the app: 1 answered, 0 failed"})
    rules = aa.rules(connection, target["id"])
    by_prom = any(r.get("last_source") == "prometheus" for r in rules if r["enabled"])
    scraped = [f for f in fams if f["name"] != "probe_success"]
    return {"selector": _sel(target), "metrics": sorted(fams, key=lambda f: f["name"]),
            "evaluator": "Prometheus" if by_prom else "BOSS's own records",
            "word": (f"{len(scraped)} metric families from the app's last scrape"
                     if scraped else "No metrics scraped from this app yet: only its health "
                                     "probe")}


def specs(target: dict, names: set[str]) -> list[dict[str, Any]]:
    """The sample rules for this app, from the metrics it actually exports."""
    s = _sel(target)
    reqs = "http_requests_total"
    out = [
        {"alert": "SampleErrorBudgetBurn", "for": "2m",
         "expr": (f'(sum(rate({reqs}{{{s},status=~"5.."}}[10m])) / sum(rate({reqs}{{{s}}}[10m])))'
                  f" / 0.005 > 1 and sum(increase({reqs}{{{s}}}[10m])) >= 100"),
         "labels": {"severity": "sev2"},
         "summary": "Error budget burning faster than 1x (99.5% availability SLO, 10 min)",
         "description": "5xx share over 10 minutes, divided by the 0.5% budget a 99.5% SLO "
                        "allows. Above 1 the month's budget runs out early."},
        {"alert": "SampleLatencyP95", "for": "1m", "keep_firing_for": "1m",
         "expr": ("1000 * histogram_quantile(0.95, sum by (le) (rate("
                  f"http_request_duration_seconds_bucket{{{s}}}[2m]))) > 750 and "
                  f"sum(increase(http_request_duration_seconds_count{{{s}}}[2m])) >= 20"),
         "labels": {"severity": "sev3"}, "summary": "p95 latency above 750 ms (2 min window)",
         "description": "95 of 100 requests should finish within 750 ms."},
        {"alert": "SampleHealthCheck", "for": "2m",
         "expr": (f"sum(count_over_time(probe_success{{{s}}}[10m])) - "
                  f"sum(sum_over_time(probe_success{{{s}}}[10m])) >= 2"),
         "labels": {"severity": "sev1"}, "summary": "Health check failing from BOSS",
         "description": "Two or more of BOSS's health probes failed in 10 minutes: the app may "
                        "be down for customers."},
        {"alert": "SampleHeartbeat", "for": "0m",
         "expr": f"absent_over_time({reqs}{{{s}}}[5m])",
         "labels": {"severity": "sev2"}, "summary": "Heartbeat lost: no metrics for 5 min",
         "description": "A dead-man's switch: fires when the app's request counter stops "
                        "arriving at all (the app or its /metrics went away)."},
    ]
    if target.get("app_name", "").lower().startswith("order"):
        out.append({
            "alert": "SampleNoDeletes", "for": "0m",
            "expr": (f'sum(increase({reqs}{{{s},method="DELETE"}}[5m])) < 1 and '
                     f"sum(increase({reqs}{{{s}}}[5m])) > 0"),
            "labels": {"severity": "sev4"},
            "summary": "No DELETE /orders traffic for 5 min while the app serves",
            "description": "A no-traffic rule on one route: informational."})
    else:
        out.append({
            "alert": "SampleNoTraffic", "for": "0m",
            "expr": (f"sum(increase({reqs}{{{s}}}[30m])) < 1 and "
                     f"sum(increase({reqs}{{{s}}}[6h])) > 0"),
            "labels": {"severity": "sev4"}, "summary": "No requests for 30 min",
            "description": "The app usually has traffic and had none for 30 minutes."})
    if "tickets_created_total" in names:
        out.append({
            "alert": "SampleCustomerTicketSurge", "for": "0m",
            "expr": f"sum(increase(tickets_created_total{{{s}}}[1h])) > 10",
            "labels": {"severity": "sev3"},
            "summary": "Customers are opening tickets faster than usual (over 10 an hour)",
            "description": "A customer signal: new support tickets are what customers do when "
                           "something is wrong."})
    return out


def seed(connection, target: dict, *, actor: str,
         now: Optional[datetime] = None) -> dict[str, Any]:
    """Create this app's sample rules that do not exist yet. Idempotent."""
    now = now or datetime.now(timezone.utc)
    names = {f["name"] for f in families(connection, target)}
    have = {r["alert"] for r in aa.rules(connection, target["id"])}
    created = []
    for sp in specs(target, names):
        if sp["alert"] in have:
            continue
        change = {"alert": sp["alert"], "expr": sp["expr"], "for": sp["for"],
                  "labels": {**sp["labels"], "sample": "true"},
                  "annotations": {"summary": SAMPLE_PREFIX + sp["summary"],
                                  "description": sp["description"] + " This is a SAMPLE rule: "
                                  "remove it with Remove samples."},
                  "triage": False}
        if sp.get("keep_firing_for"):
            change["keep_firing_for"] = sp["keep_firing_for"]
        aa.update_rule(connection, target, None, change, actor=actor,
                       reason="Sample rule (BOSSAPP-598)", now=now)
        created.append(sp["alert"])
    return {"created": created,
            "word": (f"Added {len(created)} sample rule{'s' if len(created) != 1 else ''}"
                     if created else "The sample rules are already here")}


def remove(connection, target: dict, *, actor: str,
           now: Optional[datetime] = None) -> dict[str, Any]:
    """Delete every sample rule of this app; their open alerts resolve as "Rule switched off"."""
    now = now or datetime.now(timezone.utc)
    rows = [r for r in aa.rules(connection, target["id"])
            if str((r.get("labels") or {}).get("sample") or "").lower() == "true"]
    for r in rows:
        for (aid,) in connection.execute(sql(
                "SELECT id FROM app_alerts WHERE rule_id = :r AND state IN ('pending', 'firing')"),
                {"r": r["id"]}).all():
            aa._resolve(connection, aid, now, "rule_off", r["id"])
        connection.execute(sql("DELETE FROM notification_policies WHERE rule_id = :r"),
                           {"r": r["id"]})
        aa._audit(connection, r, "deleted", aa._snapshot(r), None, actor,
                  "Removed sample rules", now)
        connection.execute(sql("DELETE FROM app_alert_rules WHERE id = :r"), {"r": r["id"]})
    return {"removed": len(rows),
            "word": (f"Removed {len(rows)} sample rule{'s' if len(rows) != 1 else ''}"
                     if rows else "No sample rules to remove")}


def record_families(connection, target_id: str, body: str) -> None:
    """At a scrape: the metric families the app exports (names, types, label keys, help), kept
    on its monitored service for the builder. Written only when the set changed."""
    from .app_metrics import parse_exposition

    fams = exposition_families(body, parse_exposition(body))
    sid = mon.service_for_target(connection, target_id)
    if not sid:
        return
    blob = json.dumps(fams, sort_keys=True)
    connection.execute(sql(
        "UPDATE monitored_services SET onboarding = onboarding || jsonb_build_object("
        " 'metric_families', CAST(:f AS JSONB)), updated_at = now() WHERE id = :s "
        "AND COALESCE(onboarding->'metric_families', 'null'::jsonb) <> CAST(:f AS JSONB)"),
        {"s": sid, "f": blob})


def exposition_families(body: str, samples: list) -> list[dict[str, Any]]:
    """Families from an exposition: a histogram or summary under its base name; `_created`
    series (client bookkeeping) left out. At most 300."""
    types: dict[str, str] = {}
    helps: dict[str, str] = {}
    for line in body.splitlines():
        if line.startswith("# TYPE "):
            parts = line[7:].split()
            if len(parts) >= 2:
                types[parts[0]] = parts[1]
        elif line.startswith("# HELP "):
            parts = line[7:].split(" ", 1)
            helps[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    fams: dict[str, dict[str, Any]] = {}
    for smp in samples:
        name = smp.name
        base = name
        for suffix in ("_bucket", "_sum", "_count"):
            if name.endswith(suffix) and types.get(name[: -len(suffix)]) in ("histogram",
                                                                             "summary"):
                base = name[: -len(suffix)]
        if base.endswith("_created"):
            continue
        f = fams.setdefault(base, {"name": base, "type": types.get(base) or (
            "counter" if base.endswith("_total") else "unknown"), "labels": set(),
            "help": helps.get(base) or None})
        f["labels"].update(k for k in smp.labels if k not in ("le", "quantile"))
    return [{**f, "labels": sorted(f["labels"])} for f in
            sorted(fams.values(), key=lambda x: x["name"])][:300]


__all__ = ["SAMPLE_PREFIX", "exposition_families", "families", "metric_names",
           "record_families", "remove", "seed", "specs"]
