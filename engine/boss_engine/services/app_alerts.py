"""App alerts: every rule BOSS watches for an app, every alert that fired, and what BOSS did.

THE RULE IS PROMQL, IN THE PROMETHEUS SHAPE (operator decision 2026-10-18)
-------------------------------------------------------------------------
A rule is `alert`, `expr` (PromQL), `for`, `labels.severity` and `annotations` summary /
description / runbook_url — portable as-is to Prometheus, Alertmanager and Grafana alerting
(`export_rules`). BOSS's built-in rules are PromQL over the platform-standard metrics
(docs/platform_standards: `http_requests_total{method,route,status}`,
`http_request_duration_seconds`), the blackbox probe's `probe_success`, and one BOSS series,
`boss_deploy_health_ok`:

    error_rate     100 * sum(increase(http_requests_total{app,env,status=~"5.."}[5m]))
                     / sum(increase(http_requests_total{app,env}[5m])) > 5
                     and sum(increase(http_requests_total{app,env}[5m])) >= 20
    latency_p95    1000 * histogram_quantile(0.95, sum by (le) (increase(
                     http_request_duration_seconds_bucket{app,env}[5m]))) > 800
                     and sum(increase(http_request_duration_seconds_count{app,env}[5m])) >= 20
    health_check   sum(count_over_time(probe_success{app,env}[10m]))
                     - sum(sum_over_time(probe_success{app,env}[10m])) >= 3
                     and max(max_over_time(probe_success{app,env}[10m])) == 0
    no_traffic     sum(increase(http_requests_total{app,env}[15m])) < 1
                     and sum(increase(http_requests_total{app,env}[2h])) > 0      (off by default)
    deploy_health  min(boss_deploy_health_ok{app,env}) == 0

The app's boss.deploy.yaml gives the starting values (`metrics.alert`, `alerts:`); a change made
in the console wins and is recorded (app_alert_rule_changes), with who and when.

ONE EVALUATION SEMANTICS, TWO EVALUATORS
----------------------------------------
The same `expr` is evaluated by the deployed Prometheus when BOSS's recorded check says it
answers, and otherwise by BOSS's own monitor over its own scrapes (`promql_lite`, Prometheus'
semantics; `local_store` presents the scrapes as the series Prometheus would hold). The result is
then judged by ONE state machine (`_apply`): Prometheus' alert lifecycle (pending until `for`
has elapsed, firing, resolved). `test_alert_rules_mean_the_same_to_boss_and_prometheus` runs
`promtool test rules` on the same series and expects BOSS's values and alerts.

THE ALERT RECORD, ALERTMANAGER-STYLE
------------------------------------
  * FINGERPRINT — the app, the alert name and the result's labels. A re-firing alert updates
    its row; one that resolved and fires again within RESOLVE_WINDOW reopens it (a flap is one
    alert, not two). One open alert per fingerprint is a unique index.
  * GROUP — `labels.group_key`, else the runbook: breaches with one resolution become ONE
    incident, every alert listed under it, so only one response runs. The built-in error-rate,
    latency and no-traffic rules share the app's `serving` group (whose incident is the app's
    error-rate incident, `app_metrics.INCIDENT_PREFIX` + target, which reports and responses
    already key on); health check and deploy share `availability`.
  * INHIBIT — while an availability alert (health check, deploy) fires, the app's symptom
    alerts (error rate, latency, no traffic) are suppressed: recorded, not grouped, not triaged.
  * SILENCE — a person silences a rule for a set time with a reason; audited.

AGENTS PROCESS EVERY ALERT (agent_decision: a model decides, never keywords)
---------------------------------------------------------------------------
Each fired alert is triaged with the rule and breach, the metrics around it, recent releases,
open incidents, the alert's group and the app's recent alerts. The agent keeps it in (opens or
joins) its group's incident, moves it to another open incident, splits it into one of its own,
marks it noise with a reason, or asks the person one question. Its words are recorded on the
alert. With no model, the alert says so and is not triaged. `Respond automatically` (per rule,
on the standing grant of the person who switched it on) starts Incident Response when the
triage keeps the alert in an incident.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..ports import sql
from . import promql_lite as pq
from .core import emit_event, make_id

log = logging.getLogger(__name__)

#: A resolved alert that fires again within this window is the same alert again (a flap).
RESOLVE_WINDOW = timedelta(minutes=5)
#: A no-model triage is tried again after this long.
TRIAGE_RETRY = timedelta(minutes=10)
#: At most this many triages per sweep: each is a model call.
TRIAGE_PER_SWEEP = 5
#: The workflow Respond automatically starts (workflow_catalogue "incident_response").
RESPONSE_WORKFLOW = "incident_response"
SERVING, AVAILABILITY = "serving", "availability"
BUILTIN_RUNBOOK = "boss:runbook/"

KINDS = ("error_rate", "latency_p95", "health_check", "no_traffic", "deploy_health")

#: The built-in rules. `params` are the starting values a yaml or a person changes.
BUILTIN: dict[str, dict[str, Any]] = {
    "error_rate": {
        "alert": "AppErrorRate", "label": "Error rate", "severity": "critical",
        "layer": "symptom", "runbook": BUILTIN_RUNBOOK + "app-serving", "enabled": True,
        "unit": "%", "params": {"threshold": 5.0, "window_minutes": 5, "min_requests": 20},
    },
    "latency_p95": {
        "alert": "AppLatencyP95", "label": "p95 latency", "severity": "warning",
        "layer": "symptom", "runbook": BUILTIN_RUNBOOK + "app-serving", "enabled": True,
        "unit": "ms", "params": {"threshold": 800.0, "window_minutes": 5, "min_requests": 20},
    },
    "health_check": {
        "alert": "AppHealthCheckFailing", "label": "Health check", "severity": "critical",
        "layer": "availability", "runbook": BUILTIN_RUNBOOK + "app-down", "enabled": True,
        "unit": "failed probes", "params": {"min_failures": 3, "window_minutes": 10},
    },
    "no_traffic": {
        "alert": "AppNoTraffic", "label": "No traffic", "severity": "warning",
        "layer": "symptom", "runbook": BUILTIN_RUNBOOK + "app-serving", "enabled": False,
        "unit": "requests", "params": {"window_minutes": 15, "expected_hours": 2},
    },
    "deploy_health": {
        "alert": "AppDeployUnhealthy", "label": "Deploy health", "severity": "critical",
        "layer": "availability", "runbook": BUILTIN_RUNBOOK + "app-down", "enabled": True,
        "unit": "", "params": {},
    },
}
#: Which parameters a person may set, with their bounds (the edit sheet and the yaml).
PARAM_BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "error_rate": {"threshold": (0.1, 100.0), "window_minutes": (1, 1440),
                   "min_requests": (1, 1_000_000)},
    "latency_p95": {"threshold": (1, 600_000), "window_minutes": (1, 1440),
                    "min_requests": (1, 1_000_000)},
    "health_check": {"min_failures": (1, 1000), "window_minutes": (1, 1440)},
    "no_traffic": {"window_minutes": (1, 1440), "expected_hours": (1, 168)},
    "deploy_health": {},
}
SEVERITIES = ("warning", "critical")

RUNBOOKS: dict[str, dict[str, str]] = {
    "app-serving": {
        "title": "The app is serving badly",
        "markdown": (
            "# The app is serving badly\n\n"
            "Errors, slow requests or a sudden stop in traffic, seen in the app's own metrics.\n\n"
            "1. **Which release is serving?** Compare its deploy time with the alert's.\n"
            "2. **Which route fails?** The alert's incident lists the failing routes.\n"
            "3. **What does the app log?** Open Logs for the app's containers.\n"
            "4. **New release and new errors:** roll back, or let Incident Response fix it with "
            "a regression test.\n"
            "5. **No release:** check dependencies (database, upstream APIs) and the host.\n"),
    },
    "app-down": {
        "title": "The app is down or its deploy failed",
        "markdown": (
            "# The app is down or its deploy failed\n\n"
            "Its health check fails from outside, or the newest deploy failed its health "
            "check. Errors and latency alerts are held back while this fires.\n\n"
            "1. **Is the host up?** Open the host in Infra.\n"
            "2. **Is the container running?** Check the app's containers and their restarts.\n"
            "3. **Did a deploy just fail?** Open the deploy and read its failed step.\n"
            "4. **Roll back** to the last healthy release if the new one cannot start.\n"),
    },
}


# --------------------------------------------------------------------------- #
# Rendering: the built-in rules as PromQL
# --------------------------------------------------------------------------- #
def quote(value: Any) -> str:
    """A PromQL string literal (boss_promql_quote in schema.sql writes the same)."""
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def num(value: Any) -> str:
    """A number as PromQL text: 5.0 -> '5', 0.5 -> '0.5' (boss_promql_num)."""
    v = float(value)
    return str(int(v)) if v == int(v) else repr(v)


def window(minutes: Any) -> str:
    """5 -> '5m', 1.5 -> '90s' (boss_promql_window)."""
    m = float(minutes)
    return f"{int(m)}m" if m == int(m) else f"{int(round(m * 60))}s"


def selector(app: str, env: str) -> str:
    return f"app={quote(app)},environment={quote(env)}"


def render(kind: str, params: dict[str, Any], app: str, env: str) -> dict[str, Any]:
    """{expr, value_expr, guard_expr, threshold, op} for a built-in rule."""
    p = {**BUILTIN[kind]["params"], **(params or {})}
    sel = selector(app, env)
    if kind == "error_rate":
        w = window(p["window_minutes"])
        reqs = f"sum(increase(http_requests_total{{{sel}}}[{w}]))"
        value = (f"100 * sum(increase(http_requests_total{{{sel},status=~\"5..\"}}[{w}])) / "
                 f"{reqs}")
        guard = f"{reqs} >= {num(p['min_requests'])}"
        return {"expr": f"{value} > {num(p['threshold'])} and {guard}", "value_expr": value,
                "guard_expr": guard, "threshold": float(p["threshold"]), "op": ">"}
    if kind == "latency_p95":
        w = window(p["window_minutes"])
        value = ("1000 * histogram_quantile(0.95, sum by (le) (increase("
                 f"http_request_duration_seconds_bucket{{{sel}}}[{w}])))")
        guard = (f"sum(increase(http_request_duration_seconds_count{{{sel}}}[{w}])) >= "
                 f"{num(p['min_requests'])}")
        return {"expr": f"{value} > {num(p['threshold'])} and {guard}", "value_expr": value,
                "guard_expr": guard, "threshold": float(p["threshold"]), "op": ">"}
    if kind == "health_check":
        w = window(p["window_minutes"])
        value = (f"sum(count_over_time(probe_success{{{sel}}}[{w}])) - "
                 f"sum(sum_over_time(probe_success{{{sel}}}[{w}]))")
        guard = f"max(max_over_time(probe_success{{{sel}}}[{w}])) == 0"
        return {"expr": f"{value} >= {num(p['min_failures'])} and {guard}", "value_expr": value,
                "guard_expr": guard, "threshold": float(p["min_failures"]), "op": ">="}
    if kind == "no_traffic":
        w = window(p["window_minutes"])
        value = f"sum(increase(http_requests_total{{{sel}}}[{w}]))"
        guard = (f"sum(increase(http_requests_total{{{sel}}}"
                 f"[{num(p['expected_hours'])}h])) > 0")
        return {"expr": f"{value} < 1 and {guard}", "value_expr": value, "guard_expr": guard,
                "threshold": 1.0, "op": "<"}
    if kind == "deploy_health":
        value = f"min(boss_deploy_health_ok{{{sel}}})"
        return {"expr": f"{value} == 0", "value_expr": value, "guard_expr": None,
                "threshold": 0.0, "op": "=="}
    raise ValueError(f"unknown rule kind {kind}")


def condition_words(kind: str, params: dict[str, Any], for_seconds: int = 0) -> str:
    """The rule in words, for the Rules list: '> 5% for 5 min (≥ 20 req)'."""
    p = {**BUILTIN.get(kind, {}).get("params", {}), **(params or {})}
    hold = f", held {int(for_seconds // 60)} min" if for_seconds >= 60 else ""
    if kind == "error_rate":
        return (f"> {num(p['threshold'])}% for {num(p['window_minutes'])} min "
                f"(≥ {num(p['min_requests'])} req){hold}")
    if kind == "latency_p95":
        return f"> {num(p['threshold'])} ms for {num(p['window_minutes'])} min{hold}"
    if kind == "health_check":
        return f"{num(p['min_failures'])} failed probes in {num(p['window_minutes'])} min{hold}"
    if kind == "no_traffic":
        return f"0 req for {num(p['window_minutes'])} min{hold}"
    if kind == "deploy_health":
        return "newest deploy failed its health check"
    return "Custom PromQL" + hold


def fingerprint(target_id: str, alertname: str, labels: dict[str, str]) -> str:
    """The alert's identity: app, alert name, the result's labels. A built-in rule aggregates
    away every label, so its fingerprint is the app and the name (schema.sql's backfill)."""
    sig = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "__name__")
    return hashlib.sha256(f"{target_id}|{alertname}|{sig}".encode()).hexdigest()[:16]


def group_of(rule: dict[str, Any]) -> str:
    """labels.group_key, else the runbook (its name for a built-in runbook), else the alert."""
    labels = rule.get("labels") or {}
    if labels.get("group_key"):
        return str(labels["group_key"])
    runbook = str((rule.get("annotations") or {}).get("runbook_url") or "")
    if runbook == BUILTIN_RUNBOOK + "app-serving":
        return SERVING
    if runbook == BUILTIN_RUNBOOK + "app-down":
        return AVAILABILITY
    return runbook or str(rule.get("alert") or "")


def incident_key(target_id: str, group_key: str) -> str:
    """The incident a group's alerts share. `serving` is the app's error-rate incident, the key
    reports and responses keyed on before groups existed."""
    from .app_metrics import INCIDENT_PREFIX

    if group_key == SERVING:
        return INCIDENT_PREFIX + target_id
    slug = "".join(c if c.isalnum() else "-" for c in group_key.lower())[:40].strip("-")
    return f"app_alert:{target_id}:{slug or hashlib.sha1(group_key.encode()).hexdigest()[:8]}"


# --------------------------------------------------------------------------- #
# Rules: the yaml's starting values, the console's edits
# --------------------------------------------------------------------------- #
def _json(v: Any, default: Any = None) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (v or None)


def _manifest(connection, target_id: str) -> dict[str, Any]:
    row = connection.execute(sql(
        "SELECT manifest FROM stack_deployments WHERE target_id = :t AND status = 'healthy' "
        "ORDER BY created_at DESC LIMIT 1"), {"t": target_id}).scalar()
    return _json(row, {}) or {}


def yaml_values(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """kind -> what boss.deploy.yaml says about it ({params, enabled, severity, runbook_url,
    for_seconds}); `custom` -> [Prometheus-shaped rules]. `metrics.alert` is the error-rate
    rule's older spelling and still counts."""
    out: dict[str, dict[str, Any]] = {}
    alert = ((manifest.get("metrics") or {}).get("alert")) or {}
    if alert:
        er: dict[str, Any] = {"params": {}}
        if "error_rate_percent" in alert:
            er["params"]["threshold"] = float(alert["error_rate_percent"])
        for k in ("window_minutes", "min_requests"):
            if k in alert:
                er["params"][k] = alert[k]
        if "enabled" in alert:
            er["enabled"] = bool(alert["enabled"])
        out["error_rate"] = er
    alerts = manifest.get("alerts") or {}
    for kind in KINDS:
        spec = alerts.get(kind)
        if not isinstance(spec, dict):
            continue
        got = out.setdefault(kind, {"params": {}})
        for k in PARAM_BOUNDS[kind]:
            if k in spec:
                got["params"][k] = spec[k]
        for k in ("enabled", "severity", "runbook_url", "for_seconds"):
            if k in spec:
                got[k] = spec[k]
        if "for" in spec and "for_seconds" not in spec:
            try:
                got["for_seconds"] = _for_seconds(spec["for"])
            except pq.PromQLError:
                pass
    if isinstance(alerts.get("rules"), list):
        out["custom"] = {"rules": [r for r in alerts["rules"] if isinstance(r, dict)]}
    return out


def _builtin_row(kind: str, target: dict, yaml: dict[str, Any]) -> dict[str, Any]:
    b = BUILTIN[kind]
    params = {**b["params"], **(yaml.get("params") or {})}
    rendered = render(kind, params, target["app_name"], target["environment"])
    return {
        "kind": kind, "alert": b["alert"], "expr": rendered["expr"],
        "for_seconds": int(yaml.get("for_seconds") or 0), "params": params,
        "labels": {"severity": yaml.get("severity") or b["severity"], "layer": b["layer"]},
        "annotations": {"summary": f"{b['label']} on {target['app_name']}",
                        "description": condition_words(kind, params),
                        "runbook_url": yaml.get("runbook_url") or b["runbook"]},
        "enabled": bool(yaml.get("enabled", b["enabled"])),
        "source": "yaml" if yaml else "builtin",
        "yaml_params": yaml or None,
    }


def _rule_dict(row: Any) -> dict[str, Any]:
    r = dict(row)
    for k in ("params", "labels", "annotations"):
        r[k] = _json(r.get(k), {}) or {}
    r["respond_link"] = _json(r.get("respond_link"), None)
    r["yaml_params"] = _json(r.get("yaml_params"), None)
    return r


def ensure_rules(connection, target: dict) -> list[dict[str, Any]]:
    """The app's rules, created from boss.deploy.yaml (or BOSS's defaults) the first time, and
    kept in step with the yaml until a person edits them in the console (then theirs win).
    A built-in rule's expression is re-rendered when the app is renamed or its params change."""
    tid, pid = target["id"], target["project_id"]
    yaml = yaml_values(_manifest(connection, tid))
    rows = {r["kind"]: _rule_dict(r) for r in connection.execute(sql(
        "SELECT * FROM app_alert_rules WHERE target_id = :t AND kind <> 'custom'"),
        {"t": tid}).mappings().all()}
    for kind in KINDS:
        want = _builtin_row(kind, target, yaml.get(kind) or {})
        have = rows.get(kind)
        if have is None:
            connection.execute(sql(
                "INSERT INTO app_alert_rules (id, project_id, target_id, kind, alert, expr, "
                " for_seconds, params, labels, annotations, enabled, source, yaml_params) VALUES "
                " (:id, :p, :t, :k, :a, :e, :f, CAST(:params AS JSONB), CAST(:l AS JSONB), "
                " CAST(:an AS JSONB), :en, :src, CAST(:y AS JSONB)) "
                "ON CONFLICT (target_id, kind) WHERE kind <> 'custom' DO NOTHING"),
                {"id": make_id("arule"), "p": pid, "t": tid, "k": kind, "a": want["alert"],
                 "e": want["expr"], "f": want["for_seconds"], "params": json.dumps(want["params"]),
                 "l": json.dumps(want["labels"]), "an": json.dumps(want["annotations"]),
                 "en": want["enabled"], "src": want["source"],
                 "y": json.dumps(want["yaml_params"]) if want["yaml_params"] else None})
            continue
        if have["source"] != "console" and (have.get("yaml_params") or None) != (
                want["yaml_params"] or None):
            # THE YAML CHANGED AND NOBODY EDITED THIS RULE: the new deploy's values apply.
            connection.execute(sql(
                "UPDATE app_alert_rules SET expr = :e, for_seconds = :f, "
                " params = CAST(:params AS JSONB), labels = CAST(:l AS JSONB), "
                " annotations = CAST(:an AS JSONB), enabled = :en, source = :src, "
                " yaml_params = CAST(:y AS JSONB) WHERE id = :id"),
                {"id": have["id"], "e": want["expr"], "f": want["for_seconds"],
                 "params": json.dumps(want["params"]), "l": json.dumps(want["labels"]),
                 "an": json.dumps(want["annotations"]), "en": want["enabled"],
                 "src": want["source"],
                 "y": json.dumps(want["yaml_params"]) if want["yaml_params"] else None})
            continue
        expr = render(kind, have["params"], target["app_name"], target["environment"])["expr"]
        if expr != have["expr"]:
            connection.execute(sql("UPDATE app_alert_rules SET expr = :e WHERE id = :id"),
                               {"e": expr, "id": have["id"]})
    for spec in (yaml.get("custom") or {}).get("rules") or []:
        _upsert_yaml_custom(connection, target, spec)
    return [_rule_dict(r) for r in connection.execute(sql(
        "SELECT * FROM app_alert_rules WHERE target_id = :t ORDER BY "
        " array_position(ARRAY['error_rate','latency_p95','health_check','no_traffic',"
        " 'deploy_health','custom'], kind), created_at"), {"t": tid}).mappings().all()]


def _upsert_yaml_custom(connection, target: dict, spec: dict) -> None:
    try:
        rule = validate_custom(spec)
    except ValueError as exc:
        log.info("app_alerts: yaml rule refused: %s", exc)
        return
    have = connection.execute(sql(
        "SELECT id, source FROM app_alert_rules WHERE target_id = :t AND kind = 'custom' "
        "AND alert = :a"), {"t": target["id"], "a": rule["alert"]}).mappings().first()
    if have and have["source"] == "console":
        return
    if have:
        connection.execute(sql(
            "UPDATE app_alert_rules SET expr = :e, for_seconds = :f, labels = CAST(:l AS JSONB), "
            " annotations = CAST(:an AS JSONB) WHERE id = :id"),
            {"id": have["id"], "e": rule["expr"], "f": rule["for_seconds"],
             "l": json.dumps(rule["labels"]), "an": json.dumps(rule["annotations"])})
        return
    connection.execute(sql(
        "INSERT INTO app_alert_rules (id, project_id, target_id, kind, alert, expr, for_seconds, "
        " labels, annotations, enabled, source) VALUES (:id, :p, :t, 'custom', :a, :e, :f, "
        " CAST(:l AS JSONB), CAST(:an AS JSONB), true, 'yaml')"),
        {"id": make_id("arule"), "p": target["project_id"], "t": target["id"],
         "a": rule["alert"], "e": rule["expr"], "f": rule["for_seconds"],
         "l": json.dumps(rule["labels"]), "an": json.dumps(rule["annotations"])})


def _for_seconds(raw: Any) -> int:
    if raw in (None, "", 0):
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    return pq.parse_duration(str(raw)) // 1000


def validate_custom(spec: dict[str, Any]) -> dict[str, Any]:
    """A Prometheus-shaped rule from a person or a yaml, checked. Raises ValueError in words."""
    import re

    alert = str(spec.get("alert") or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", alert):
        raise ValueError("alert must be a name like CheckoutSlow (letters, digits, _)")
    expr = str(spec.get("expr") or "").strip()
    if len(expr) > 4000:
        raise ValueError("expr is longer than 4000 characters")
    try:
        pq.parse(expr)
    except pq.PromQLUnsupported:
        pass                     # valid PromQL that only Prometheus evaluates: kept, and said
    except pq.PromQLError as exc:
        raise ValueError(f"expr is not valid PromQL: {exc}") from None
    try:
        for_seconds = _for_seconds(spec.get("for"))
    except pq.PromQLError as exc:
        raise ValueError(f"for: {exc}") from None
    labels = {str(k): str(v) for k, v in (spec.get("labels") or {}).items()}
    labels.setdefault("severity", "warning")
    if labels["severity"] not in SEVERITIES:
        raise ValueError(f"labels.severity must be one of {list(SEVERITIES)}")
    ann = {str(k): str(v)[:2000] for k, v in (spec.get("annotations") or {}).items()
           if k in ("summary", "description", "runbook_url")}
    return {"alert": alert, "expr": expr, "for_seconds": for_seconds, "labels": labels,
            "annotations": ann}


def rules(connection, target_id: str) -> list[dict[str, Any]]:
    return [_rule_dict(r) for r in connection.execute(sql(
        "SELECT * FROM app_alert_rules WHERE target_id = :t ORDER BY "
        " array_position(ARRAY['error_rate','latency_p95','health_check','no_traffic',"
        " 'deploy_health','custom'], kind), created_at"), {"t": target_id}).mappings().all()]


# --------------------------------------------------------------------------- #
# The series BOSS holds, as Prometheus would hold them
# --------------------------------------------------------------------------- #
def _max_range_ms(expr: str) -> int:
    best = 0

    def walk(n):
        nonlocal best
        if isinstance(n, pq.Selector):
            best = max(best, n.range_ms or 0)
        for child in ("expr", "lhs", "rhs"):
            if hasattr(n, child):
                walk(getattr(n, child))
        for a in getattr(n, "args", []) or []:
            walk(a)
    try:
        walk(pq.parse(expr))
    except pq.PromQLError:
        pass
    return best


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _split_route(key: str) -> tuple[str, str]:
    head, _, rest = key.partition(" ")
    if rest and head.isupper():
        return head, rest
    return "", key


def local_store(connection, target: dict, now: datetime, range_ms: int) -> pq.Store:
    """The app's series from BOSS's own records, labelled as the stack's collector labels them
    (app, environment, project, target_id):

      http_requests_total{method,route,status}   app_metrics scrapes (cumulative counters as
                                                  scraped; status "5xx" / "non_5xx")
      http_request_duration_seconds_bucket{le}, _count, _sum
      probe_success, probe_duration_seconds       deployment_health samples (1 / 0)
      boss_deploy_health_ok                       1 when the newest finished deploy is healthy,
                                                  0 when it failed (a gauge read now)
    """
    base = {"app": str(target.get("app_name") or ""),
            "environment": str(target.get("environment") or ""),
            "project": str(target.get("project_id") or ""), "target_id": str(target["id"])}
    start = now - timedelta(milliseconds=range_ms + pq.LOOKBACK_MS)
    store = pq.Store()

    def add(name: str, extra: dict[str, str], t: int, v: Any) -> None:
        if v is None:
            return
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        store.add({"__name__": name, **base, **extra}, [(t, f)])

    for r in connection.execute(sql(
            "SELECT scraped_at, snapshot FROM app_metrics_scrapes WHERE deployment_target_id = :t "
            "AND ok AND snapshot IS NOT NULL AND scraped_at > :a AND scraped_at <= :z "
            "ORDER BY scraped_at"), {"t": target["id"], "a": start, "z": now}).mappings().all():
        snap = _json(r["snapshot"], {}) or {}
        t = _ms(r["scraped_at"])
        routes = snap.get("routes") or {}
        errors_known = snap.get("errors") is not None
        if routes:
            for key, rv in routes.items():
                method, route = _split_route(key)
                lab = {"method": method, "route": route} if method else {"route": route}
                req, err = float(rv.get("requests") or 0), float(rv.get("errors") or 0)
                if errors_known:
                    add("http_requests_total", {**lab, "status": "5xx"}, t, err)
                    add("http_requests_total", {**lab, "status": "non_5xx"}, t, req - err)
                else:
                    add("http_requests_total", lab, t, req)
        elif snap.get("requests") is not None:
            req = float(snap["requests"])
            if errors_known:
                err = float(snap["errors"])
                add("http_requests_total", {"status": "5xx"}, t, err)
                add("http_requests_total", {"status": "non_5xx"}, t, req - err)
            else:
                add("http_requests_total", {}, t, req)
        for le, c in (snap.get("buckets") or {}).items():
            add("http_request_duration_seconds_bucket", {"le": le}, t, c)
        add("http_request_duration_seconds_count", {}, t, snap.get("hist_count"))
        add("http_request_duration_seconds_sum", {}, t, snap.get("hist_sum"))
    for r in connection.execute(sql(
            "SELECT checked_at, ok, latency_ms FROM deployment_health_samples "
            "WHERE deployment_target_id = :t AND checked_at > :a AND checked_at <= :z "
            "ORDER BY checked_at"), {"t": target["id"], "a": start, "z": now}).mappings().all():
        t = _ms(r["checked_at"])
        add("probe_success", {}, t, 1.0 if r["ok"] else 0.0)
        if r["latency_ms"] is not None:
            add("probe_duration_seconds", {}, t, float(r["latency_ms"]) / 1000.0)
    newest = connection.execute(sql(
        "SELECT status FROM stack_deployments WHERE target_id = :t "
        "AND status IN ('healthy', 'failed') "
        "ORDER BY COALESCE(finished_at, updated_at, created_at) DESC LIMIT 1"),
        {"t": target["id"]}).scalar()
    if newest is None:
        newest = connection.execute(sql(
            "SELECT CASE WHEN status IN ('succeeded', 'deployed') THEN 'healthy' ELSE 'failed' "
            "END FROM deployment_runs WHERE deployment_target_id = :t "
            "AND status IN ('succeeded', 'deployed', 'failed') "
            "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1"),
            {"t": target["id"]}).scalar()
    if newest is not None:
        add("boss_deploy_health_ok", {}, _ms(now), 1.0 if newest == "healthy" else 0.0)
    return store


BOSS_ONLY_PREFIX = "boss_"


def boss_only(expr: str) -> bool:
    """Does the expression read a series only BOSS holds (Prometheus does not scrape BOSS)?"""
    try:
        return any(n.startswith(BOSS_ONLY_PREFIX) for n in pq.selected_metrics(expr))
    except pq.PromQLError:
        return False


# --------------------------------------------------------------------------- #
# Prometheus, when it answers
# --------------------------------------------------------------------------- #
class Prometheus:
    """The deployed stack's Prometheus: an instant query, as /api/v1/query answers it."""

    def __init__(self, base_url: str, timeout: float = 4.0):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    def query(self, expr: str, at: datetime) -> list[pq.Sample]:
        import urllib.parse
        import urllib.request

        url = (f"{self.base_url}/api/v1/query?" + urllib.parse.urlencode(
            {"query": expr, "time": f"{at.timestamp():.3f}"}))
        with urllib.request.urlopen(url, timeout=self.timeout) as r:   # noqa: S310
            body = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
        if body.get("status") != "success":
            raise pq.PromQLError(str(body.get("error") or "Prometheus refused the query"))
        data = body.get("data") or {}
        if data.get("resultType") == "scalar":
            raise pq.PromQLError("an alert expression must give a vector, not a single number")
        out = []
        for row in data.get("result") or []:
            v = row.get("value") or [0, "NaN"]
            out.append(pq.Sample(dict(row.get("metric") or {}), float(v[1])))
        return out


def prometheus_for(connection, project_id: str) -> Optional[Prometheus]:
    """The project's Prometheus, only when BOSS's recorded check says it answers."""
    from .observability_stack import endpoint_state

    state = endpoint_state(connection, project_id, "prometheus")
    return Prometheus(state["base_url"]) if state["state"] == "answering" else None


# --------------------------------------------------------------------------- #
# Evaluation: one rule -> its result, its measure and why it is quiet
# --------------------------------------------------------------------------- #
def _measure(rule: dict, target: dict) -> dict[str, Optional[str]]:
    if rule["kind"] == "custom":
        return {"expr": rule["expr"], "value_expr": None, "guard_expr": None}
    r = render(rule["kind"], rule["params"], target["app_name"], target["environment"])
    return {"expr": rule["expr"], "value_expr": r["value_expr"], "guard_expr": r["guard_expr"]}


def evaluate_rule(rule: dict, target: dict, now: datetime, *, store: Optional[pq.Store] = None,
                  prometheus: Optional[Prometheus] = None) -> dict[str, Any]:
    """{results: [Sample], value, quiet: no_data|too_little|ok, source, error}. Never raises.

    Prometheus when given and the rule reads nothing only BOSS holds; else BOSS's own store."""
    m = _measure(rule, target)
    use_prom = prometheus is not None and not boss_only(m["expr"])
    source = "prometheus" if use_prom else "boss"

    def run(expr: str) -> list[pq.Sample]:
        if use_prom:
            return prometheus.query(expr, now)
        return pq.instant_vector(expr, store or pq.Store(), _ms(now))

    try:
        results = run(m["expr"])
    except pq.PromQLUnsupported as exc:
        return {"results": [], "value": None, "quiet": "error", "source": source,
                "error": f"Only Prometheus evaluates {exc}"}
    except Exception as exc:  # noqa: BLE001 — a rule that cannot be evaluated says why
        return {"results": [], "value": None, "quiet": "error", "source": source,
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    value = results[0].value if results else None
    quiet = "ok"
    try:
        if m["value_expr"]:
            got = run(m["value_expr"])
            value = got[0].value if got else None
            if not got or math.isnan(got[0].value):
                quiet = "no_data"
            elif m["guard_expr"] and not run(m["guard_expr"]) and not results:
                quiet = "too_little"
        elif not results:
            quiet = "ok"
    except Exception:  # noqa: BLE001 — the measure is for words; the result is what counts
        pass
    if value is not None and (math.isnan(value) or math.isinf(value)):
        value = None
    return {"results": results, "value": value, "quiet": quiet, "source": source, "error": None}


# --------------------------------------------------------------------------- #
# The state machine (Prometheus' alert lifecycle) and the record
# --------------------------------------------------------------------------- #
def lifecycle(state: Optional[str], active_at_ms: Optional[int], present: bool, now_ms: int,
              hold_ms: int) -> Optional[str]:
    """Prometheus' alert lifecycle, one evaluation: the next state of one alert instance.

    present (the expression returned it): new -> pending, or firing when `for` is 0; pending ->
    firing once it has been active for `for`; firing stays firing. Absent: pending is dropped
    (None: it never fired), firing -> resolved. This one function is the semantics both
    evaluators share; `_apply` writes it, the promtool agreement test replays it."""
    if present:
        if state in (None, "resolved"):
            return "firing" if hold_ms <= 0 else "pending"
        start = now_ms if active_at_ms is None else int(active_at_ms)
        if state == "pending" and now_ms - start >= hold_ms:
            return "firing"
        return state
    if state == "pending":
        return None
    if state == "firing":
        return "resolved"
    return state


def _open_alerts(connection, target_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(sql(
        "SELECT * FROM app_alerts WHERE target_id = :t AND state IN ('pending', 'firing') "
        "AND source_kind = 'rule'"), {"t": target_id}).mappings().all()
    return {r["fingerprint"]: dict(r) for r in rows}


def _release(connection, target_id: str) -> Optional[dict[str, Any]]:
    from .incident_response import deploy_context

    d = deploy_context(connection, target_id)
    if not d:
        return None
    return {"commit_sha": d.get("commit_sha"), "ref": d.get("ref"),
            "deployed_at": d.get("deployed_at")}


_QUIET_REASON = {"ok": "recovered", "no_data": "no_data", "too_little": "too_little",
                 "error": "error"}


def _apply(connection, target: dict, rule: dict, ev: dict, now: datetime,
           opened: dict[str, dict]) -> list[str]:
    """Write one evaluation into the record. Returns the ids of alerts that FIRED now."""
    fired: list[str] = []
    active = {fp: a for fp, a in opened.items()
              if a.get("rule_id") == rule["id"] or (a.get("rule_id") is None
                                                    and a.get("rule_kind") == rule["kind"])}
    seen: set[str] = set()
    hold = int(rule.get("for_seconds") or 0)
    threshold = None
    if rule["kind"] != "custom":
        threshold = render(rule["kind"], rule["params"], target["app_name"],
                           target["environment"])["threshold"]
    for sample in ev["results"]:
        labels = {k: v for k, v in sample.labels.items() if k != "__name__"}
        fp = fingerprint(target["id"], rule["alert"], labels)
        seen.add(fp)
        value = None if math.isnan(sample.value) else sample.value
        row = active.get(fp)
        if row is None:
            # A FLAP IS ONE ALERT: the same fingerprint cleared moments ago is reopened.
            row = connection.execute(sql(
                "SELECT * FROM app_alerts WHERE fingerprint = :f AND state = 'resolved' "
                "AND cleared_at >= :since ORDER BY cleared_at DESC LIMIT 1"),
                {"f": fp, "since": now - RESOLVE_WINDOW}).mappings().first()
            if row is not None:
                # Reopened through the same lifecycle: with a `for`, it is pending again (from
                # now) before it fires again; without one it fires again at once.
                again = lifecycle(None, None, True, _ms(now), hold * 1000)
                connection.execute(sql(
                    "UPDATE app_alerts SET state = :s, cleared_at = NULL, cleared_reason = NULL, "
                    " fire_count = fire_count + CASE WHEN :s = 'firing' THEN 1 ELSE 0 END, "
                    " active_at = CASE WHEN :s = 'pending' THEN :now ELSE active_at END, "
                    " last_value = :v, last_eval_at = :now, rule_id = :r, eval_source = :src, "
                    " updated_at = :now WHERE id = :id"),
                    {"id": row["id"], "v": value, "now": now, "r": rule["id"],
                     "src": ev["source"], "s": again})
                opened[fp] = {**dict(row), "state": again,
                              "active_at": now if again == "pending" else row["active_at"]}
                continue
            state = lifecycle(None, None, True, _ms(now), hold * 1000)
            alert_id = make_id("alrt")
            ann = dict(rule["annotations"])
            connection.execute(sql(
                "INSERT INTO app_alerts (id, project_id, target_id, rule_id, rule_kind, "
                " fingerprint, alertname, labels, annotations, expr, value, last_value, "
                " threshold, unit, state, active_at, fired_at, last_eval_at, eval_source, "
                " release, group_key, triage_state, created_at, updated_at) VALUES "
                " (:id, :p, :t, :r, :k, :f, :a, CAST(:l AS JSONB), CAST(:an AS JSONB), :e, :v, "
                " :v, :th, :u, :s, :now, :fired, :now, :src, CAST(:rel AS JSONB), :g, :ts, :now, "
                " :now)"),
                {"id": alert_id, "p": target["project_id"], "t": target["id"], "r": rule["id"],
                 "k": rule["kind"], "f": fp, "a": rule["alert"],
                 "l": json.dumps({**rule["labels"], **labels}), "an": json.dumps(ann),
                 "e": rule["expr"], "v": value, "th": threshold,
                 "u": BUILTIN.get(rule["kind"], {}).get("unit", ""), "s": state, "now": now,
                 "fired": now if state == "firing" else None, "src": ev["source"],
                 "rel": json.dumps(_release(connection, target["id"])), "g": group_of(rule),
                 "ts": "pending" if rule.get("triage", True) else "off"})
            opened[fp] = {"id": alert_id, "state": state, "rule_id": rule["id"],
                          "rule_kind": rule["kind"], "fingerprint": fp}
            if state == "firing":
                fired.append(alert_id)
            continue
        promote = row["state"] == "pending" and lifecycle(
            "pending", _ms(row["active_at"]), True, _ms(now), hold * 1000) == "firing"
        connection.execute(sql(
            "UPDATE app_alerts SET last_value = :v, last_eval_at = :now, rule_id = :r, "
            " eval_source = :src, updated_at = :now, "
            " state = CASE WHEN :promote THEN 'firing' ELSE state END, "
            " fired_at = CASE WHEN :promote AND fired_at IS NULL THEN :now ELSE fired_at END, "
            " fire_count = fire_count + CASE WHEN :promote AND fired_at IS NOT NULL THEN 1 "
            "                                ELSE 0 END, "
            " value = CASE WHEN :promote THEN :v ELSE value END "
            "WHERE id = :id"),
            {"id": row["id"], "v": value, "now": now, "r": rule["id"], "src": ev["source"],
             "promote": promote})
        if promote:
            fired.append(row["id"])
            opened[fp] = {**row, "state": "firing"}
    reason = _QUIET_REASON.get(ev["quiet"], "recovered")
    for fp, row in active.items():
        if fp in seen:
            continue
        if row["state"] == "pending":
            # Prometheus drops a pending alert that clears before `for`: it never fired.
            connection.execute(sql("DELETE FROM app_alerts WHERE id = :id"), {"id": row["id"]})
        else:
            _resolve(connection, row["id"], now, reason)
        opened.pop(fp, None)
    return fired


def _resolve(connection, alert_id: str, now: datetime, reason: str) -> None:
    connection.execute(sql(
        "UPDATE app_alerts SET state = 'resolved', cleared_at = :now, cleared_reason = :r, "
        " updated_at = :now WHERE id = :id AND state IN ('pending', 'firing')"),
        {"id": alert_id, "now": now, "r": reason})


def _record_rule_state(connection, rule: dict, ev: dict, now: datetime, state: str) -> None:
    word = {"firing": "Firing", "pending": "Pending", "ok": "OK", "no_data": "No data",
            "too_little": "Too little traffic", "off": "Off", "silenced": "Silenced",
            "inhibited": "Held back", "error": "Can't evaluate"}.get(state, state)
    connection.execute(sql(
        "UPDATE app_alert_rules SET last_eval_at = :now, last_state = :s, last_value = :v, "
        " last_word = :w, last_source = :src WHERE id = :id"),
        {"id": rule["id"], "now": now, "s": state, "v": ev.get("value"),
         "w": (ev.get("error") or word)[:300], "src": ev.get("source")})


# --------------------------------------------------------------------------- #
# Silences, inhibition, grouping
# --------------------------------------------------------------------------- #
def active_silences(connection, target_id: str, now: datetime) -> dict[str, dict[str, Any]]:
    rows = connection.execute(sql(
        "SELECT * FROM app_alert_silences WHERE target_id = :t AND expired_at IS NULL "
        "AND starts_at <= :now AND ends_at > :now ORDER BY ends_at DESC"),
        {"t": target_id, "now": now}).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out.setdefault(r["rule_id"], dict(r))
    return out


def _suppress(connection, target: dict, now: datetime,
              silences: dict[str, dict[str, Any]]) -> None:
    """Inhibition and silences, recomputed for every open alert of the app."""
    rows = [dict(r) for r in connection.execute(sql(
        "SELECT id, rule_id, rule_kind, state, labels, alertname FROM app_alerts "
        "WHERE target_id = :t AND state IN ('pending', 'firing')"),
        {"t": target["id"]}).mappings().all()]
    cause = next((r for r in rows if r["state"] == "firing"
                  and (_json(r["labels"], {}) or {}).get("layer") == "availability"), None)
    for r in rows:
        layer = (_json(r["labels"], {}) or {}).get("layer")
        inhibited = cause["id"] if cause and layer == "symptom" else None
        silence = silences.get(r["rule_id"] or "")
        connection.execute(sql(
            "UPDATE app_alerts SET inhibited_by = :i, silenced_by = :s WHERE id = :id"),
            {"i": inhibited, "s": silence["id"] if silence else None, "id": r["id"]})


def _incident_row(connection, project_id: str, key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql(
        "SELECT id, status, title, finalize_ctx FROM incidents WHERE project_id = :p "
        "AND dedupe_key = :k"), {"p": project_id, "k": key}).mappings().first()
    if not row:
        return None
    out = dict(row)
    out["finalize_ctx"] = _json(out.get("finalize_ctx"), {}) or {}
    return out


_CLOSED = ("resolved", "dismissed")


def _alert_payload(alert: dict, rule: Optional[dict], window: dict) -> dict[str, Any]:
    """The legacy rule shape incident_response reads (error_rate_percent ...) plus the alert."""
    params = (rule or {}).get("params") or {}
    legacy = {}
    if (rule or {}).get("kind") == "error_rate":
        legacy = {"enabled": True, "error_rate_percent": float(params.get("threshold", 5.0)),
                  "window_minutes": params.get("window_minutes", 5),
                  "min_requests": params.get("min_requests", 20)}
    return {**legacy, "alert": alert.get("alertname"), "expr": alert.get("expr"),
            "kind": alert.get("rule_kind"), "alert_id": alert["id"]}


def _window_for(connection, target_id: str, rule: Optional[dict], now: datetime) -> dict:
    from . import app_metrics

    minutes = float(((rule or {}).get("params") or {}).get("window_minutes") or 5)
    return app_metrics.aggregate(app_metrics._window_rows(
        connection, target_id, now - timedelta(minutes=minutes), now))


def _group(connection, target: dict, now: datetime, rules_by_id: dict[str, dict]
           ) -> list[dict[str, Any]]:
    """Every firing alert that is not held back joins its group's incident: opened (or
    reopened) by the first, joined by the rest. Returns what happened to each incident."""
    from . import incident_response

    pid = target["project_id"]
    rows = [dict(r) for r in connection.execute(sql(
        "SELECT * FROM app_alerts a WHERE a.target_id = :t AND a.state = 'firing' "
        "AND a.inhibited_by IS NULL AND a.silenced_by IS NULL AND a.source_kind = 'rule' "
        "AND COALESCE(a.triage->>'outcome', '') <> 'noise' "
        # no incident yet, or its incident was closed while it kept firing (or flapped back)
        "AND (a.incident_id IS NULL OR EXISTS (SELECT 1 FROM incidents i WHERE "
        "     i.id = a.incident_id AND i.status IN ('resolved', 'dismissed'))) "
        "ORDER BY a.fired_at"), {"t": target["id"]}).mappings().all()]
    actions: list[dict[str, Any]] = []
    for a in _still_firing(connection, target, now):
        # STILL FIRING: the incident hears it on every evaluation (its signal count and last
        # seen), once per evaluation however many sweeps run it.
        connection.execute(sql(
            "UPDATE incidents SET last_seen = :now, signal_count = signal_count + 1 "
            "WHERE id = :i"), {"i": a["incident_id"], "now": now})
        _signal(connection, target, a, a["incident_id"],
                _window_for(connection, target["id"], rules_by_id.get(a.get("rule_id") or ""),
                            now), now)
    for a in rows:
        rule = rules_by_id.get(a.get("rule_id") or "")
        key = incident_key(target["id"], a["group_key"] or SERVING)
        inc = _incident_row(connection, pid, key)
        alerted = bool(inc and (inc["finalize_ctx"].get("alert") not in (None, {})))
        window = _window_for(connection, target["id"], rule, now)
        title = _incident_title(target, a, window, rule)
        if inc is None:
            inc_id = make_id("inc")
            connection.execute(sql(
                "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, "
                " status, signal_count, first_seen, last_seen) VALUES (:id, :p, :k, :title, "
                " :svc, :sev, 'open', 1, :now, :now)"),
                {"id": inc_id, "p": pid, "k": key, "title": title,
                 "svc": target.get("app_name") or "",
                 "sev": "critical" if (_json(a["labels"], {}) or {}).get("severity")
                 == "critical" else "warning", "now": now})
            action = "opened"
        else:
            inc_id = inc["id"]
            joined_open = inc["status"] not in _CLOSED and alerted
            connection.execute(sql(
                "UPDATE incidents SET last_seen = :now, signal_count = signal_count + 1, "
                " title = CASE WHEN status IN ('resolved', 'dismissed') OR NOT :alerted "
                "              THEN :title ELSE title END, "
                " status = CASE WHEN status IN ('resolved', 'dismissed') THEN 'open' "
                "               ELSE status END, "
                " first_seen = CASE WHEN status IN ('resolved', 'dismissed') THEN :now "
                "                   ELSE first_seen END WHERE id = :id"),
                {"id": inc_id, "now": now, "title": title, "alerted": alerted})
            action = ("reopened" if inc["status"] in _CLOSED else
                      "joined" if joined_open else "opened")
        connection.execute(sql("UPDATE app_alerts SET incident_id = :i WHERE id = :id"),
                           {"i": inc_id, "id": a["id"]})
        _signal(connection, target, a, inc_id, window, now)
        event_id = None
        if action == "joined":
            incident_response._evidence(connection, inc_id, "alert", ref=a["alertname"],
                                        payload={"fired_at": _iso(now), "joined": True,
                                                 **_alert_payload(a, rule, window),
                                                 "value": a.get("value")}, at=now)
        else:
            point = connection.begin_nested()
            try:
                event_id = incident_response.on_alert(
                    connection, target, inc_id, action=action, window=window,
                    rule=_alert_payload(a, rule, window), now=now)
                point.commit()
            except Exception:  # noqa: BLE001 — evidence never costs the alert
                log.exception("app_alerts: incident evidence failed")
                point.rollback()
        actions.append({"alert_id": a["id"], "incident_id": inc_id, "action": action,
                        "event_id": event_id, "kind": a["rule_kind"]})
    return actions


def _still_firing(connection, target: dict, now: datetime) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(sql(
        "SELECT a.* FROM app_alerts a JOIN incidents i ON i.id = a.incident_id "
        "WHERE a.target_id = :t AND a.state = 'firing' AND a.source_kind = 'rule' "
        "AND a.inhibited_by IS NULL AND a.silenced_by IS NULL "
        "AND i.status NOT IN ('resolved', 'dismissed') "
        "AND NOT EXISTS (SELECT 1 FROM anomaly_signals s WHERE s.incident_id = a.incident_id "
        "                AND s.ts = :now AND s.sample->>'alert_id' = a.id)"),
        {"t": target["id"], "now": now}).mappings().all()]


def _signal(connection, target: dict, a: dict, incident_id: str, window: dict,
            now: datetime) -> None:
    """One evaluation's evidence on the incident (anomaly_signals), as the monitor always kept."""
    connection.execute(sql(
        "INSERT INTO anomaly_signals (id, project_id, incident_id, kind, signature, service, "
        " score, count, sample, ts) VALUES (:id, :p, :inc, :kind, :sig, :svc, :score, "
        " :count, CAST(:sample AS JSONB), :now)"),
        {"id": make_id("sig"), "p": target["project_id"], "inc": incident_id,
         "kind": "error_surge" if a["rule_kind"] == "error_rate" else "metric_spike",
         "sig": "http_error_rate" if a["rule_kind"] == "error_rate" else a["alertname"],
         "svc": target.get("app_name") or "", "score": a.get("last_value", a.get("value")),
         "count": int(window.get("errors") or 0),
         "sample": json.dumps({"target_id": target["id"], "alert_id": a["id"],
                               "expr": a["expr"], "value": a.get("value"),
                               "window": window}, default=str), "now": now})


def _incident_title(target: dict, a: dict, window: Optional[dict] = None,
                    rule: Optional[dict] = None) -> str:
    head = f"{target.get('app_name')} ({target.get('environment')}): "
    if a.get("rule_kind") == "error_rate" and window and window.get("error_rate") is not None:
        # The words reports, cards and the incident response have always used for this alert.
        p = (rule or {}).get("params") or BUILTIN["error_rate"]["params"]
        pct = round(float(window["error_rate"]) * 100.0, 1)
        return (head + f"{pct}% of requests failing over {num(p['window_minutes'])} min "
                f"(alert above {float(p['threshold'])}%)")[:200]
    ann = _json(a.get("annotations"), {}) or {}
    v = a.get("value")
    unit = a.get("unit") or ""
    val = "" if v is None else f" ({num(round(v, 1))}{'' if unit in ('', '%') else ' '}{unit})"
    return (f"{target.get('app_name')} ({target.get('environment')}): "
            f"{ann.get('summary') or a['alertname']}{val}")[:200]


def _maybe_resolve_incidents(connection, target: dict, now: datetime) -> list[dict[str, Any]]:
    """An incident the monitor raised resolves when none of its alerts is open any more and
    every rule behind them now reads OK on enough traffic. Quiet is not recovery: a rule that
    went silent (too little traffic, no data) HOLDS the incident until it reads OK. And a
    running response closes its incident by its own verification, never the monitor."""
    from . import incident_response

    out = []
    rows = connection.execute(sql(
        "SELECT DISTINCT a.incident_id FROM app_alerts a JOIN incidents i ON i.id = a.incident_id "
        "WHERE a.target_id = :t AND a.source_kind = 'rule' "
        "AND i.status NOT IN ('resolved', 'dismissed')"), {"t": target["id"]}).scalars().all()
    for inc_id in rows:
        still = connection.execute(sql(
            "SELECT count(*) FROM app_alerts WHERE incident_id = :i AND source_kind = 'rule' "
            "AND state IN ('pending', 'firing') AND inhibited_by IS NULL"), {"i": inc_id}).scalar()
        if still:
            continue
        behind = connection.execute(sql(
            "SELECT DISTINCT r.id, r.target_id, r.last_state, r.last_eval_at, r.enabled "
            "FROM app_alerts a JOIN app_alert_rules r ON r.id = a.rule_id "
            "WHERE a.incident_id = :i AND a.source_kind = 'rule'"), {"i": inc_id}).mappings().all()
        if not behind or not all(
                (not r["enabled"]) or (r["last_state"] == "ok" and (
                    r["target_id"] != target["id"] or r["last_eval_at"] == now))
                for r in behind):
            continue
        inc = connection.execute(sql("SELECT id, finalize_ctx FROM incidents WHERE id = :i"),
                                 {"i": inc_id}).mappings().first()
        ctx = _json(inc["finalize_ctx"], {}) or {}
        if ctx.get("alert") in (None, {}):
            continue        # reported only: the monitor never closes what it did not raise
        if incident_response._response_in_flight(connection, target["project_id"], inc_id):
            continue
        connection.execute(sql(
            "UPDATE incidents SET status = 'resolved', last_seen = :now WHERE id = :id"),
            {"id": inc_id, "now": now})
        rule = connection.execute(sql(
            "SELECT r.* FROM app_alerts a JOIN app_alert_rules r ON r.id = a.rule_id "
            "WHERE a.incident_id = :i ORDER BY a.cleared_at DESC NULLS LAST LIMIT 1"),
            {"i": inc_id}).mappings().first()
        rule = _rule_dict(rule) if rule else None
        window = _window_for(connection, target["id"], rule, now)
        point = connection.begin_nested()
        event_id = None
        try:
            event_id = incident_response.on_alert(
                connection, target, inc_id, action="resolved", window=window,
                rule=_alert_payload({"id": "", "alertname": (rule or {}).get("alert"),
                                     "expr": (rule or {}).get("expr"),
                                     "rule_kind": (rule or {}).get("kind")}, rule, window),
                now=now)
            point.commit()
        except Exception:  # noqa: BLE001
            point.rollback()
        out.append({"incident_id": inc_id, "action": "resolved", "event_id": event_id})
    return out


def _close_signal_alerts(connection, target_id: str, now: datetime) -> None:
    connection.execute(sql(
        "UPDATE app_alerts a SET state = 'resolved', cleared_at = :now, "
        " cleared_reason = 'incident_closed', updated_at = :now FROM incidents i "
        "WHERE i.id = a.incident_id AND a.target_id = :t AND a.source_kind = 'signal' "
        "AND a.state = 'firing' AND i.status IN ('resolved', 'dismissed')"),
        {"t": target_id, "now": now})


# --------------------------------------------------------------------------- #
# One app, one evaluation
# --------------------------------------------------------------------------- #
def evaluate_target(connection, target: dict, now: Optional[datetime] = None, *,
                    prefetched: Optional[dict[str, dict[str, Any]]] = None,
                    local_only: bool = False) -> dict[str, Any]:
    """Evaluate every rule of one app and write the record: alerts, inhibition, silences,
    incidents. `prefetched` holds Prometheus' answers (rule id -> evaluation), fetched by the
    sweep outside any transaction; every other rule is evaluated over BOSS's own scrapes —
    except, with `local_only` (a scrape's own call) while Prometheus answers, the rules
    Prometheus evaluates: one evaluator per rule, so two never take turns.

    Returns {fired: [alert ids], incidents: [...], rules: {rule id: state}}."""
    now = now or _now()
    rule_rows = ensure_rules(connection, target)
    silences = active_silences(connection, target["id"], now)
    opened = _open_alerts(connection, target["id"])
    prom_answers = local_only and prometheus_for(connection, target["project_id"]) is not None
    enabled = [r for r in rule_rows if r["enabled"]]
    span = max([_max_range_ms(r["expr"]) for r in enabled] or [0])
    store = local_store(connection, target, now, span) if enabled else pq.Store()
    fired: list[str] = []
    states: dict[str, str] = {}
    for rule in rule_rows:
        if not rule["enabled"]:
            for fp, row in list(opened.items()):
                if row.get("rule_id") == rule["id"]:
                    _resolve(connection, row["id"], now, "rule_off")
                    opened.pop(fp)
            _record_rule_state(connection, rule, {"value": None, "source": None}, now, "off")
            states[rule["id"]] = "off"
            continue
        if prom_answers and not boss_only(rule["expr"]) and (prefetched or {}).get(
                rule["id"]) is None:
            continue
        ev = (prefetched or {}).get(rule["id"]) or evaluate_rule(rule, target, now, store=store)
        fired += _apply(connection, target, rule, ev, now, opened)
        mine = [a for a in opened.values() if a.get("rule_id") == rule["id"]]
        state = ("error" if ev.get("error") else
                 "firing" if any(a["state"] == "firing" for a in mine) else
                 "pending" if mine else ev["quiet"])
        if rule["id"] in silences and state in ("firing", "pending"):
            state = "silenced"
        _record_rule_state(connection, rule, ev, now, state)
        states[rule["id"]] = state
    _suppress(connection, target, now, silences)
    for rid, st in list(states.items()):
        if st == "firing" and connection.execute(sql(
                "SELECT bool_and(inhibited_by IS NOT NULL) FROM app_alerts WHERE rule_id = :r "
                "AND state = 'firing'"), {"r": rid}).scalar():
            states[rid] = "inhibited"
            connection.execute(sql("UPDATE app_alert_rules SET last_state = 'inhibited', "
                                   "last_word = 'Held back' WHERE id = :r"), {"r": rid})
    by_id = {r["id"]: r for r in rule_rows}
    incidents = _group(connection, target, now, by_id)
    incidents += _maybe_resolve_incidents(connection, target, now)
    _close_signal_alerts(connection, target["id"], now)
    return {"fired": fired, "incidents": incidents, "rules": states}


def _lock(connection, target_id: str) -> None:
    """One writer per app (the same lock app_metrics' scrape takes)."""
    connection.execute(sql("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": "app_metrics:" + target_id})


def sweep(engine, settings: Any, targets: list[dict], *, now: Optional[datetime] = None,
          skip: Optional[set[str]] = None) -> dict[str, Any]:
    """Every deployed app's rules, on the monitor's cadence; then triage what fired. Never
    raises. Prometheus is asked (outside any transaction) when its recorded check, refreshed
    here when stale, says it answers."""
    from ..db import begin
    from .observability_stack import check_endpoint, prometheus_base

    now = now or _now()
    evaluated = failed = 0
    try:
        for pid in sorted({t["project_id"] for t in targets}):
            # Only a stack BOSS deployed is asked (its registered source); a host that never ran
            # one is not probed on every sweep.
            with begin(engine) as c:
                registered = prometheus_base(c, pid) is not None
            for kind in ("prometheus", "grafana") if registered else ():
                try:
                    check_endpoint(engine, pid, kind, only_if_stale=True)
                except Exception:  # noqa: BLE001
                    log.info("app_alerts: %s check failed", kind, exc_info=True)
        for t in targets:
            if skip and t["id"] in skip:
                continue
            try:
                with begin(engine) as c:
                    prom = prometheus_for(c, t["project_id"])
                    rule_rows = ensure_rules(c, t) if prom else []
                prefetched = {}
                for r in rule_rows:
                    if r["enabled"] and not boss_only(r["expr"]):
                        prefetched[r["id"]] = evaluate_rule(r, t, now, prometheus=prom)
                with begin(engine) as c:
                    _lock(c, t["id"])
                    evaluate_target(c, t, now, prefetched=prefetched)
                evaluated += 1
            except Exception:  # noqa: BLE001 — one app's rules never cost another's
                log.exception("app_alerts: evaluation failed for %s", t.get("id"))
                failed += 1
        triaged = triage_pending(engine, settings,
                                 project_ids={t["project_id"] for t in targets})
    except Exception as exc:  # noqa: BLE001
        return {"alerts_evaluated": evaluated, "alerts_failed": failed,
                "alerts_error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"alerts_evaluated": evaluated, "alerts_failed": failed, "alerts_triaged": triaged}


# --------------------------------------------------------------------------- #
# Signals an app reports are alerts too
# --------------------------------------------------------------------------- #
def record_signal(connection, *, reporter: dict, affected: dict, signal: dict,
                  incident_id: str, now: datetime) -> str:
    """An app's report (incident_signals.ingest) as an alert from a signal source."""
    fp = hashlib.sha256(f"{reporter['id']}|AppSignal|{signal['id']}".encode()).hexdigest()[:16]
    alert_id = make_id("alrt")
    connection.execute(sql(
        "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, source_kind, "
        " reporter_target_id, fingerprint, alertname, labels, annotations, value, unit, state, "
        " active_at, fired_at, last_eval_at, group_key, incident_id, release, triage_state, "
        " created_at, updated_at) VALUES (:id, :p, :t, 'signal', 'signal', :rep, :f, "
        " 'AppSignal', CAST(:l AS JSONB), CAST(:an AS JSONB), :v, 'reports', 'firing', :now, "
        " :now, :now, :g, :inc, CAST(:rel AS JSONB), 'pending', :now, :now) "
        "ON CONFLICT DO NOTHING"),
        {"id": alert_id, "p": reporter["project_id"], "t": affected["id"],
         "rep": reporter["id"], "f": fp,
         "l": json.dumps({"severity": signal.get("severity") or "warning", "layer": "symptom"}),
         "an": json.dumps({"summary": signal.get("title") or "Reported by the app",
                           "description": signal.get("summary") or "",
                           "runbook_url": BUILTIN_RUNBOOK + "app-serving"}),
         "v": float(signal.get("count") or 1), "now": now, "g": SERVING, "inc": incident_id,
         "rel": json.dumps(_release(connection, affected["id"]))})
    return alert_id


# --------------------------------------------------------------------------- #
# Triage: a model decides (agent_decision)
# --------------------------------------------------------------------------- #
NO_MODEL_WORD = "Not triaged: no model is answering"


def _claim(engine, project_ids: Optional[set[str]], limit: int) -> list[str]:
    from ..db import begin

    with begin(engine) as c:
        rows = c.execute(sql(
            "UPDATE app_alerts SET triage_attempt_at = now() WHERE id IN ("
            " SELECT id FROM app_alerts WHERE state = 'firing' AND inhibited_by IS NULL "
            " AND silenced_by IS NULL AND ("
            "  (triage_state = 'pending' AND (triage_attempt_at IS NULL "
            "    OR triage_attempt_at < now() - interval '2 minutes')) "
            "  OR (triage_state = 'no_model' AND (triage_attempt_at IS NULL "
            "    OR triage_attempt_at < now() - :retry)))"
            + (" AND project_id = ANY(:pids)" if project_ids else "")
            + " ORDER BY fired_at LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING id"),
            {"retry": TRIAGE_RETRY, "n": limit,
             **({"pids": sorted(project_ids)} if project_ids else {})}).scalars().all()
    return list(rows)


def triage_pending(engine, settings: Any, *, project_ids: Optional[set[str]] = None,
                   limit: int = TRIAGE_PER_SWEEP, adapter: Any = None) -> int:
    done = 0
    for alert_id in _claim(engine, project_ids, limit):
        try:
            triage_alert(engine, settings, alert_id, adapter=adapter)
            done += 1
        except Exception:  # noqa: BLE001
            log.exception("app_alerts: triage failed for %s", alert_id)
    return done


def _alert(connection, alert_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql("SELECT * FROM app_alerts WHERE id = :i"),
                             {"i": alert_id}).mappings().first()
    if not row:
        return None
    out = dict(row)
    for k in ("labels", "annotations", "release", "triage"):
        out[k] = _json(out.get(k), None)
    return out


def _target(connection, target_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql("SELECT * FROM deployment_targets WHERE id = :t"),
                             {"t": target_id}).mappings().first()
    return dict(row) if row else None


def triage_context(connection, alert: dict) -> dict[str, Any]:
    """What the agent reads: the rule and breach, the metrics, recent releases (and the serving
    one's diff summary when the investigation has one), open incidents, the alert's group and
    the app's recent alerts."""
    tid, pid = alert["target_id"], alert["project_id"]
    rule = None
    if alert.get("rule_id"):
        r = connection.execute(sql("SELECT * FROM app_alert_rules WHERE id = :i"),
                               {"i": alert["rule_id"]}).mappings().first()
        rule = _rule_dict(r) if r else None
    releases = [{"commit": (r["commit_sha"] or "")[:7], "ref": r["ref"], "status": r["status"],
                 "at": _iso(r["finished_at"] or r["created_at"])}
                for r in connection.execute(sql(
                    "SELECT commit_sha, ref, status, finished_at, created_at FROM "
                    "stack_deployments WHERE target_id = :t ORDER BY created_at DESC LIMIT 4"),
                    {"t": tid}).mappings().all()]
    opened = [{"id": r["id"], "title": r["title"], "status": r["status"],
               "since": _iso(r["first_seen"])}
              for r in connection.execute(sql(
                  "SELECT id, title, status, first_seen FROM incidents WHERE project_id = :p "
                  "AND status NOT IN ('resolved', 'dismissed') ORDER BY last_seen DESC LIMIT 6"),
                  {"p": pid}).mappings().all()]
    group = [{"alert": r["alertname"], "value": r["value"], "state": r["state"],
              "fired_at": _iso(r["fired_at"])}
             for r in connection.execute(sql(
                 "SELECT alertname, value, state, fired_at FROM app_alerts WHERE incident_id = :i "
                 "AND id <> :a ORDER BY fired_at DESC LIMIT 8"),
                 {"i": alert.get("incident_id") or "", "a": alert["id"]}).mappings().all()]
    recent = [{"alert": r["alertname"], "value": r["value"], "state": r["state"],
               "fired_at": _iso(r["fired_at"]),
               "triage": ((_json(r["triage"], {}) or {}).get("outcome"))}
              for r in connection.execute(sql(
                  "SELECT alertname, value, state, fired_at, triage FROM app_alerts "
                  "WHERE target_id = :t AND id <> :a ORDER BY active_at DESC LIMIT 8"),
                  {"t": tid, "a": alert["id"]}).mappings().all()]
    diff = connection.execute(sql(
        "SELECT payload FROM incident_evidence WHERE incident_id = :i AND kind = 'release_diff' "
        "ORDER BY created_at DESC LIMIT 1"), {"i": alert.get("incident_id") or ""}).scalar()
    window = _window_for(connection, tid, rule, alert.get("fired_at") or _now())
    return {
        "alert": {"name": alert["alertname"], "summary": (alert.get("annotations") or {}).get(
                      "summary"), "expr": alert.get("expr"), "value_at_breach": alert.get("value"),
                  "threshold": alert.get("threshold"), "unit": alert.get("unit"),
                  "severity": (alert.get("labels") or {}).get("severity"),
                  "fired_at": _iso(alert.get("fired_at")), "evaluated_by": alert.get(
                      "eval_source"), "source": alert.get("source_kind")},
        "rule": {"kind": (rule or {}).get("kind"), "params": (rule or {}).get("params"),
                 "for_seconds": (rule or {}).get("for_seconds")} if rule else None,
        "metrics_window": {k: window.get(k) for k in ("requests", "errors", "error_rate",
                                                      "rate_per_min", "p50_ms", "p95_ms")},
        "failing_routes": (window.get("routes") or [])[:5],
        "serving_release": alert.get("release"),
        "recent_releases": releases,
        "release_diff": ({k: (_json(diff, {}) or {}).get(k) for k in ("word", "files")}
                         if diff else None),
        "its_incident": alert.get("incident_id"),
        "alerts_in_its_group": group,
        "open_incidents": opened,
        "recent_alerts_of_this_app": recent,
    }


def _candidates(alert: dict, ctx: dict) -> list:
    from .agent_decision import Candidate

    own = alert.get("incident_id")
    out = []
    if own:
        first = not ctx["alerts_in_its_group"]
        out.append(Candidate("open_incident" if first else "join_incident",
                             ("It is a real problem: keep it in the incident BOSS opened for it"
                              if first else "It is the same problem as the other alerts in its "
                              "incident: keep it there")))
    for inc in ctx["open_incidents"]:
        if inc["id"] == own:
            continue
        out.append(Candidate(f"join:{inc['id']}", f"It is the same problem as the open incident "
                                                  f"'{inc['title']}': move it there"))
    if own and ctx["alerts_in_its_group"]:
        out.append(Candidate("split", "It is a different problem from the other alerts in its "
                                      "incident: open a new incident for it alone"))
    if not own:
        out.append(Candidate("open_incident", "It is a real problem: open an incident for it"))
    out.append(Candidate("noise", "It is noise (a blip, a restart, a test): mark it noise with "
                                  "the reason and open nothing"))
    out.append(Candidate("ask", "Ask the person one question before deciding", acts=False))
    return out


TRIAGE_MESSAGE = (
    "Triage this alert. Decide which candidate action fits. In `reason`, say in at most three "
    "sentences: what it is, how bad it is, whether it is BOSS-side (the platform, the deploy) or "
    "app-side (the app's own code), and which recent release it links to, if any.")


def triage_alert(engine, settings: Any, alert_id: str, *, adapter: Any = None,
                 decider: Any = None) -> dict[str, Any]:
    """One alert's triage by a model, recorded word for word. Never closes an alert silently."""
    from ..db import begin
    from . import agent_decision as ad

    with begin(engine) as c:
        alert = _alert(c, alert_id)
        if not alert or alert["state"] != "firing":
            return {"status": "skipped"}
        ctx = triage_context(c, alert)
        cands = _candidates(alert, ctx)
        target = _target(c, alert["target_id"]) or {}
        decision = ad.decide(
            c, settings, alert["project_id"],
            situation=(f"An alert fired on {target.get('app_name')} "
                       f"({target.get('environment')}), an app BOSS deployed. You are the "
                       "on-call agent deciding what it is and what BOSS does with it."),
            message=TRIAGE_MESSAGE, candidates=cands, context=ctx, agent="BOSS's on-call agent",
            ref=alert_id, adapter=adapter, decider=decider)
    at = _now()
    record: dict[str, Any] = {**decision.to_dict(), "at": at.isoformat()}
    with begin(engine) as c:
        _lock(c, alert["target_id"])
        alert = _alert(c, alert_id) or alert
        if decision.status == ad.NO_MODEL:
            record.update(outcome=None, word=NO_MODEL_WORD)
            _write_triage(c, alert_id, record, "no_model")
            return record
        if decision.status == ad.CLARIFY or decision.action == "ask":
            record.update(outcome="asked", word="Asks you")
            _write_triage(c, alert_id, record, "done")
            _note(c, alert.get("incident_id"), f"Agent asks: {decision.question}", alert_id, at)
            return record
        action = decision.action or ""
        inc_id = alert.get("incident_id")
        if action in ("open_incident", "join_incident"):
            if not inc_id:
                inc_id = _new_incident(c, target, alert, at)
            record.update(outcome="opened" if action == "open_incident" else "joined",
                          incident_id=inc_id)
            c.execute(sql("UPDATE incidents SET status = 'triaged' WHERE id = :i "
                          "AND status = 'open'"), {"i": inc_id})
        elif action.startswith("join:"):
            other = action.split(":", 1)[1]
            c.execute(sql("UPDATE app_alerts SET incident_id = :i WHERE id = :a"),
                      {"i": other, "a": alert_id})
            _retire_if_empty(c, inc_id, f"Merged into {other}: {decision.reason}", at)
            record.update(outcome="joined", incident_id=other, moved_from=inc_id)
            inc_id = other
        elif action == "split":
            new_id = _new_incident(c, target, alert, at, split=True)
            record.update(outcome="split", incident_id=new_id, moved_from=inc_id)
            inc_id = new_id
        elif action == "noise":
            record.update(outcome="noise", incident_id=inc_id)
            _write_triage(c, alert_id, record, "done")
            _retire_if_noise(c, inc_id, decision.reason, at)
            return record
        record["word"] = {"opened": "Opened an incident", "joined": "Joined the incident",
                          "split": "Opened its own incident"}.get(record.get("outcome"), "")
        _write_triage(c, alert_id, record, "done")
        _note(c, inc_id, f"Agent triage ({alert['alertname']}): {decision.reason}", alert_id, at)
        response = _respond(c, alert, inc_id, at)
        if response:
            c.execute(sql("UPDATE app_alerts SET response_run_id = :r, response_word = :w "
                          "WHERE id = :a"),
                      {"r": response.get("run_id"), "w": response["word"], "a": alert_id})
            record["response"] = response
    return record


def _write_triage(connection, alert_id: str, record: dict, state: str) -> None:
    connection.execute(sql(
        "UPDATE app_alerts SET triage = CAST(:t AS JSONB), triage_state = :s, updated_at = now() "
        "WHERE id = :a"), {"t": json.dumps(record, default=str), "s": state, "a": alert_id})


def _note(connection, incident_id: Optional[str], text: str, alert_id: str,
          at: datetime) -> None:
    if not incident_id:
        return
    from .incident_response import _evidence

    _evidence(connection, incident_id, "note", ref=alert_id,
              payload={"text": text[:1500], "alert_id": alert_id}, at=at)


def _new_incident(connection, target: dict, alert: dict, at: datetime, *,
                  split: bool = False) -> str:
    inc_id = make_id("inc")
    key = (f"app_alert:{alert['target_id']}:split:{alert['id']}" if split
           else incident_key(alert["target_id"], alert.get("group_key") or SERVING))
    connection.execute(sql(
        "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, status, "
        " signal_count, first_seen, last_seen) VALUES (:id, :p, :k, :title, :svc, :sev, "
        " 'triaged', 1, :at, :at) ON CONFLICT (project_id, dedupe_key) DO UPDATE SET "
        " status = 'triaged', last_seen = EXCLUDED.last_seen RETURNING id"),
        {"id": inc_id, "p": alert["project_id"], "k": key,
         "svc": target.get("app_name") or "",
         "sev": "critical" if (alert.get("labels") or {}).get("severity") == "critical"
         else "warning", "at": at, "title": _incident_title(target, alert)})
    got = connection.execute(sql("SELECT id FROM incidents WHERE project_id = :p "
                                 "AND dedupe_key = :k"),
                             {"p": alert["project_id"], "k": key}).scalar()
    connection.execute(sql("UPDATE app_alerts SET incident_id = :i WHERE id = :a"),
                       {"i": got, "a": alert["id"]})
    return str(got)


def _closable(connection, incident_id: str) -> bool:
    from .incident_response import _response_in_flight

    row = connection.execute(sql("SELECT project_id, status FROM incidents WHERE id = :i"),
                             {"i": incident_id}).mappings().first()
    if not row or row["status"] in _CLOSED:
        return False
    if _response_in_flight(connection, row["project_id"], incident_id):
        return False
    reported = connection.execute(sql(
        "SELECT 1 FROM incident_evidence WHERE incident_id = :i AND kind = 'customer_reports' "
        "LIMIT 1"), {"i": incident_id}).first()
    return not reported


def _retire_if_empty(connection, incident_id: Optional[str], why: str, at: datetime) -> None:
    if not incident_id or not _closable(connection, incident_id):
        return
    left = connection.execute(sql(
        "SELECT count(*) FROM app_alerts WHERE incident_id = :i AND state = 'firing'"),
        {"i": incident_id}).scalar()
    if left:
        return
    connection.execute(sql("UPDATE incidents SET status = 'dismissed', last_seen = :at "
                           "WHERE id = :i"), {"i": incident_id, "at": at})
    _note(connection, incident_id, why, "", at)


def _retire_if_noise(connection, incident_id: Optional[str], why: str, at: datetime) -> None:
    """Every firing alert in the incident is noise, by the agent's word: dismissed, with it."""
    if not incident_id or not _closable(connection, incident_id):
        return
    real = connection.execute(sql(
        "SELECT count(*) FROM app_alerts WHERE incident_id = :i AND state = 'firing' "
        "AND COALESCE(triage->>'outcome', '') <> 'noise'"), {"i": incident_id}).scalar()
    if real:
        return
    connection.execute(sql("UPDATE incidents SET status = 'dismissed', last_seen = :at "
                           "WHERE id = :i"), {"i": incident_id, "at": at})
    _note(connection, incident_id, f"Agent marked it noise: {why}", "", at)


# --------------------------------------------------------------------------- #
# Respond automatically, on the grant of whoever switched it on
# --------------------------------------------------------------------------- #
def _respond(connection, alert: dict, incident_id: Optional[str],
             at: datetime) -> Optional[dict[str, Any]]:
    if not incident_id or not alert.get("rule_id"):
        return None
    rule = connection.execute(sql("SELECT * FROM app_alert_rules WHERE id = :r"),
                              {"r": alert["rule_id"]}).mappings().first()
    if not rule or not rule["auto_respond"]:
        return None
    rule = _rule_dict(rule)
    from .event_dispatch import start_workflow_on_grant
    from .incident_response import _response_in_flight

    running = _response_in_flight(connection, alert["project_id"], incident_id)
    if running:
        return {"run_id": running, "word": "A response is already running"}
    inc = connection.execute(sql("SELECT title, finalize_ctx FROM incidents WHERE id = :i"),
                             {"i": incident_id}).mappings().first()
    ctx = _json((inc or {}).get("finalize_ctx"), {}) or {}
    deploy = ctx.get("deploy") or {}
    title = (inc or {}).get("title") or alert["alertname"]
    got = start_workflow_on_grant(
        connection, project_id=alert["project_id"], workflow_key=RESPONSE_WORKFLOW,
        link=rule.get("respond_link"), actor="monitor",
        input_data={"incident_id": incident_id, "target_id": alert["target_id"],
                    "alert_id": alert["id"], "extends_run_id": deploy.get("extends_run_id"),
                    "commit_sha": deploy.get("commit_sha"),
                    "previous_commit_sha": deploy.get("previous_commit_sha"),
                    "repository": deploy.get("repository"), "title": title,
                    "directive": f"Respond to the incident: {title}."},
        dedupe_key=f"alert:{alert['id']}:respond", door="alert_rule", door_id=rule["id"],
        door_name=f"Respond automatically on {rule['alert']}")
    if got.get("run_id"):
        return {"run_id": got["run_id"], "word": "Responding"}
    return {"run_id": None, "word": got.get("word") or "Could not start the response"}


def respond_link(connection, project_id: str, rule_name: str) -> dict[str, Any]:
    """Switching Respond automatically on: the person's standing grant covering the response
    workflow, or ConsentRequired (409, the console asks once). {} outside a person's session."""
    from ..identity import consent as _consent

    spec = connection.execute(sql(
        "SELECT v.spec FROM workflow_definitions d JOIN workflow_versions v "
        "ON v.id = d.active_version_id WHERE d.project_id = :p AND d.workflow_key = :k"),
        {"p": project_id, "k": RESPONSE_WORKFLOW}).scalar()
    if spec is None:
        from . import workflow_catalogue as wc

        entry = next((e for e in wc.CATALOGUE if e["key"] == RESPONSE_WORKFLOW), None)
        spec = wc.entry_spec(entry) if entry else {}
    return _consent.require_for_schedule(
        connection, project_id=project_id, spec=spec, input_data={},
        name=f"Respond automatically when {rule_name} fires") or {}


# --------------------------------------------------------------------------- #
# Retiring the app-wide Respond automatically (per rule, never per app)
# --------------------------------------------------------------------------- #
#: The audit's reason for a rule the retirement switched on.
RETIRED_REASON = ("Moved from the app-wide Respond automatically ({sub}), which {who} switched "
                  "on; it runs on the same standing grant. Incident Response now starts per "
                  "rule.")


def _fed_targets(connection, project_id: str) -> list[dict[str, Any]]:
    """The apps whose incidents an app-wide subscription answered: every app with an incident
    the monitor or an alert raised. None on record: every app in the project (what the
    subscription covered)."""
    ids = connection.execute(sql(
        "SELECT DISTINCT t FROM ("
        " SELECT finalize_ctx->'app'->>'target_id' AS t FROM incidents WHERE project_id = :p"
        " UNION SELECT a.target_id FROM app_alerts a WHERE a.project_id = :p"
        "   AND a.incident_id IS NOT NULL) x WHERE t IS NOT NULL"),
        {"p": project_id}).scalars().all()
    rows = [dict(r) for r in connection.execute(sql(
        "SELECT * FROM deployment_targets WHERE project_id = :p "
        "AND (CAST(:none AS BOOLEAN) OR id = ANY(:ids)) ORDER BY id"),
        {"p": project_id, "ids": list(ids), "none": not ids}).mappings().all()]
    return [r for r in rows if r.get("app_name") and r.get("environment")]


def _fed_rules(connection, target: dict, rules_now: list[dict[str, Any]]) -> list[dict]:
    """The app's rules that fed its incidents, and (the subscription followed every
    `incident.opened`, so it covered them all) every serving rule: error rate, latency, no
    traffic and any rule in their group. Always the error-rate rule."""
    fed = set(connection.execute(sql(
        "SELECT DISTINCT rule_id FROM app_alerts WHERE target_id = :t AND incident_id IS NOT "
        "NULL AND rule_id IS NOT NULL"), {"t": target["id"]}).scalars().all())
    return [r for r in rules_now
            if r["id"] in fed or r["kind"] == "error_rate" or group_of(r) == SERVING]


def retire_app_wide_response(connection, *, now: Optional[datetime] = None
                             ) -> list[dict[str, Any]]:
    """THE INCIDENT CARD'S APP-WIDE "Respond automatically" BECOMES PER-RULE SWITCHES, without
    losing what the person chose. Each enabled workflow subscription that starts Incident
    Response (event_dispatch.PER_RULE_ONLY) is migrated once:

      * the rules that fed its incidents (and every serving rule; at least error rate) of each
        app it answered get Respond automatically on — triage too, which a response follows —
        on the SAME person's standing grant the subscription ran on, each change in the rule's
        audit (app_alert_rule_changes) by `boss`, naming the subscription and the person;
      * then the subscription is disabled, its row kept for history (`config.retired`).

    Idempotent: a retired subscription is disabled, so it is never migrated twice; a rule
    already on is left as its owner set it. Returns what moved, per subscription."""
    from .event_dispatch import PER_RULE_ONLY, invalidate_subscription_cache
    from .event_subscription_service import event_type_matches
    from .incident_response import _person

    now = now or _now()
    subs = [dict(r) for r in connection.execute(sql(
        "SELECT id, project_id, event_type, on_behalf_of, grant_id FROM event_subscriptions "
        "WHERE sink_kind = 'workflow' AND sink_ref = ANY(:k) AND enabled ORDER BY created_at"),
        {"k": sorted(PER_RULE_ONLY)}).mappings().all()]
    out = []
    for sub in subs:
        link = ({"on_behalf_of": sub["on_behalf_of"], "grant_id": sub["grant_id"]}
                if sub.get("on_behalf_of") else None)
        who = _person(connection, sub["on_behalf_of"]) if sub.get("on_behalf_of") else "BOSS"
        moved: list[dict[str, Any]] = []
        if event_type_matches(sub["event_type"], "incident.opened"):
            for target in _fed_targets(connection, sub["project_id"]):
                for rule in _fed_rules(connection, target, ensure_rules(connection, target)):
                    if rule.get("auto_respond"):
                        continue
                    before = _snapshot(rule)
                    after = {**before, "auto_respond": True, "triage": True}
                    connection.execute(sql(
                        "UPDATE app_alert_rules SET auto_respond = true, triage = true, "
                        " respond_link = CAST(:link AS JSONB), changed_by = 'boss', "
                        " changed_at = :now WHERE id = :id"),
                        {"id": rule["id"], "now": now,
                         "link": json.dumps(link) if link else None})
                    _audit(connection, rule, "edited", before, after, "boss",
                           RETIRED_REASON.format(sub=sub["id"], who=who), now)
                    moved.append({"rule_id": rule["id"], "target_id": target["id"],
                                  "kind": rule["kind"], "alert": rule["alert"]})
        retired = {"at": _iso(now), "by": "boss", "rules": [m["rule_id"] for m in moved],
                   "reason": "Incident Response starts per alert rule now (the app's Alerts "
                             "tab); this app-wide switch was moved onto its rules."}
        connection.execute(sql(
            "UPDATE event_subscriptions SET enabled = false, updated_at = :now, "
            "config = config || CAST(:c AS JSONB) WHERE id = :i"),
            {"i": sub["id"], "now": now, "c": json.dumps({"retired": retired})})
        emit_event(connection, sub["project_id"], "event_subscription.retired", "boss",
                   "event_subscription", sub["id"],
                   {"rules": moved, "on_behalf_of": sub.get("on_behalf_of"),
                    "reason": retired["reason"]})
        invalidate_subscription_cache(sub["project_id"])
        out.append({"subscription_id": sub["id"], "project_id": sub["project_id"],
                    "on_behalf_of": sub.get("on_behalf_of"), "rules": moved})
    return out


def auto_responding(connection, target_id: Optional[str]) -> list[dict[str, Any]]:
    """The app's rules that start Incident Response by themselves (the incident card says which,
    and links to the Alerts tab where each is switched)."""
    if not target_id:
        return []
    rows = connection.execute(sql(
        "SELECT * FROM app_alert_rules WHERE target_id = :t AND enabled AND auto_respond "
        "ORDER BY array_position(ARRAY['error_rate','latency_p95','health_check','no_traffic',"
        " 'deploy_health','custom'], kind), created_at"), {"t": target_id}).mappings().all()
    out = []
    for r in map(_rule_dict, rows):
        out.append({"rule_id": r["id"], "kind": r["kind"], "alert": r["alert"],
                    "label": BUILTIN.get(r["kind"], {}).get("label") or r["alert"],
                    "on_behalf_of": (r.get("respond_link") or {}).get("on_behalf_of")})
    return out


# --------------------------------------------------------------------------- #
# Changing rules and silences (audited)
# --------------------------------------------------------------------------- #
class RuleRefused(ValueError):
    """A change BOSS will not make, said in words (422)."""


_AUDITED = ("alert", "expr", "for_seconds", "params", "labels", "annotations", "enabled",
            "triage", "auto_respond", "notify")


def _refusing(fn, *args):
    try:
        return fn(*args)
    except RuleRefused:
        raise
    except ValueError as exc:
        raise RuleRefused(str(exc)) from None


def _snapshot(rule: dict) -> dict[str, Any]:
    return {k: rule.get(k) for k in _AUDITED}


def _audit(connection, rule: dict, change: str, before: Optional[dict], after: Optional[dict],
           actor: str, reason: str, at: datetime) -> None:
    connection.execute(sql(
        "INSERT INTO app_alert_rule_changes (id, project_id, rule_id, target_id, change, before, "
        " after, reason, changed_by, changed_at) VALUES (:id, :p, :r, :t, :c, CAST(:b AS JSONB), "
        " CAST(:a AS JSONB), :why, :by, :at)"),
        {"id": make_id("arch"), "p": rule["project_id"], "r": rule["id"], "t": rule["target_id"],
         "c": change, "b": json.dumps(before, default=str) if before is not None else None,
         "a": json.dumps(after, default=str) if after is not None else None,
         "why": (reason or "")[:500], "by": actor, "at": at})
    emit_event(connection, rule["project_id"], f"alert_rule.{change}", actor, "app_alert_rule",
               rule["id"], {"target_id": rule["target_id"], "alert": rule.get("alert"),
                            "reason": (reason or "")[:500]})


def _bounded(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in (params or {}).items():
        if k not in PARAM_BOUNDS[kind]:
            raise RuleRefused(f"{BUILTIN[kind]['label']} has no setting '{k}'")
        lo, hi = PARAM_BOUNDS[kind][k]
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise RuleRefused(f"{k} must be a number") from None
        if not lo <= f <= hi:
            raise RuleRefused(f"{k} must be between {num(lo)} and {num(hi)}")
        out[k] = int(f) if k in ("min_requests", "min_failures") else f
    return out


def update_rule(connection, target: dict, rule_id: Optional[str], change: dict[str, Any], *,
                actor: str, reason: str = "", now: Optional[datetime] = None) -> dict[str, Any]:
    """Apply one person's edit (or create a custom rule when rule_id is None). Console wins:
    the rule's source becomes `console`. Respond automatically needs their consent grant."""
    now = now or _now()
    if rule_id is None:
        try:
            spec = validate_custom(change)
        except RuleRefused:
            raise
        except ValueError as exc:
            raise RuleRefused(str(exc)) from None
        if connection.execute(sql("SELECT 1 FROM app_alert_rules WHERE target_id = :t "
                                  "AND alert = :a"),
                              {"t": target["id"], "a": spec["alert"]}).first():
            raise RuleRefused(f"This app already has a rule named {spec['alert']}")
        rid = make_id("arule")
        connection.execute(sql(
            "INSERT INTO app_alert_rules (id, project_id, target_id, kind, alert, expr, "
            " for_seconds, labels, annotations, enabled, source, changed_by, changed_at) VALUES "
            " (:id, :p, :t, 'custom', :a, :e, :f, CAST(:l AS JSONB), CAST(:an AS JSONB), true, "
            " 'console', :by, :now)"),
            {"id": rid, "p": target["project_id"], "t": target["id"], "a": spec["alert"],
             "e": spec["expr"], "f": spec["for_seconds"], "l": json.dumps(spec["labels"]),
             "an": json.dumps(spec["annotations"]), "by": actor, "now": now})
        rule = _rule_dict(connection.execute(sql("SELECT * FROM app_alert_rules WHERE id = :i"),
                                             {"i": rid}).mappings().first())
        _audit(connection, rule, "created", None, _snapshot(rule), actor, reason, now)
        rule_id, change = rid, {k: v for k, v in change.items()
                                if k in ("auto_respond", "triage")}
        if not change:
            return rule
    row = connection.execute(sql("SELECT * FROM app_alert_rules WHERE id = :i AND target_id = :t"),
                             {"i": rule_id, "t": target["id"]}).mappings().first()
    if not row:
        raise LookupError("No such rule for this app")
    rule = _rule_dict(row)
    before = _snapshot(rule)
    new = dict(rule)
    if rule["kind"] == "custom":
        if any(k in change for k in ("alert", "expr", "for", "labels", "annotations")):
            spec = _refusing(validate_custom, {"alert": change.get("alert", rule["alert"]),
                                    "expr": change.get("expr", rule["expr"]),
                                    "for": change.get("for", rule["for_seconds"]),
                                    "labels": change.get("labels", rule["labels"]),
                                    "annotations": change.get("annotations",
                                                              rule["annotations"])})
            new.update(alert=spec["alert"], expr=spec["expr"], for_seconds=spec["for_seconds"],
                       labels=spec["labels"], annotations=spec["annotations"])
    else:
        if "expr" in change and change["expr"] != rule["expr"]:
            raise RuleRefused("A built-in rule's PromQL follows its settings; add a custom rule "
                              "to write your own")
        if "params" in change:
            new["params"] = {**rule["params"], **_bounded(rule["kind"], change["params"])}
            new["expr"] = render(rule["kind"], new["params"], target["app_name"],
                                 target["environment"])["expr"]
            new["annotations"] = {**rule["annotations"],
                                  "description": condition_words(rule["kind"], new["params"])}
        if "for" in change:
            try:
                new["for_seconds"] = _for_seconds(change["for"])
            except pq.PromQLError as exc:
                raise RuleRefused(f"for: {exc}") from None
        if "severity" in change:
            if change["severity"] not in SEVERITIES:
                raise RuleRefused(f"severity must be one of {list(SEVERITIES)}")
            new["labels"] = {**rule["labels"], "severity": change["severity"]}
        if "runbook_url" in change:
            new["annotations"] = {**new["annotations"],
                                  "runbook_url": str(change["runbook_url"] or "")[:500]
                                  or BUILTIN[rule["kind"]]["runbook"]}
    for k in ("enabled", "triage", "auto_respond"):
        if k in change:
            new[k] = bool(change[k])
    if new.get("auto_respond") and not new.get("triage"):
        raise RuleRefused("Respond automatically needs the agent's triage on")
    link = rule.get("respond_link")
    if new.get("auto_respond") and not rule.get("auto_respond"):
        link = respond_link(connection, target["project_id"], new["alert"]) or None
    if not new.get("auto_respond"):
        link = None
    after = _snapshot(new)
    if after == before and link == rule.get("respond_link"):
        return rule
    connection.execute(sql(
        "UPDATE app_alert_rules SET alert = :a, expr = :e, for_seconds = :f, "
        " params = CAST(:params AS JSONB), labels = CAST(:l AS JSONB), "
        " annotations = CAST(:an AS JSONB), enabled = :en, triage = :tr, auto_respond = :ar, "
        " respond_link = CAST(:link AS JSONB), source = 'console', changed_by = :by, "
        " changed_at = :now WHERE id = :id"),
        {"id": rule_id, "a": new["alert"], "e": new["expr"], "f": new["for_seconds"],
         "params": json.dumps(new["params"]), "l": json.dumps(new["labels"]),
         "an": json.dumps(new["annotations"]), "en": new["enabled"], "tr": new["triage"],
         "ar": new["auto_respond"], "link": json.dumps(link) if link else None, "by": actor,
         "now": now})
    _audit(connection, rule, "edited", before, after, actor, reason, now)
    return _rule_dict(connection.execute(sql("SELECT * FROM app_alert_rules WHERE id = :i"),
                                         {"i": rule_id}).mappings().first())


def silence(connection, target: dict, rule_id: str, *, minutes: float, reason: str,
            actor: str, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or _now()
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise RuleRefused("Say why you are silencing it")
    if not 1 <= float(minutes) <= 60 * 24 * 30:
        raise RuleRefused("A silence lasts between 1 minute and 30 days")
    row = connection.execute(sql("SELECT * FROM app_alert_rules WHERE id = :i AND target_id = :t"),
                             {"i": rule_id, "t": target["id"]}).mappings().first()
    if not row:
        raise LookupError("No such rule for this app")
    rule = _rule_dict(row)
    sid = make_id("asil")
    ends = now + timedelta(minutes=float(minutes))
    connection.execute(sql(
        "INSERT INTO app_alert_silences (id, project_id, rule_id, target_id, starts_at, ends_at, "
        " reason, created_by, created_at) VALUES (:id, :p, :r, :t, :now, :end, :why, :by, :now)"),
        {"id": sid, "p": rule["project_id"], "r": rule_id, "t": target["id"], "now": now,
         "end": ends, "why": reason[:500], "by": actor})
    _audit(connection, rule, "silenced", None, {"until": ends.isoformat()}, actor, reason, now)
    _suppress(connection, target, now, active_silences(connection, target["id"], now))
    return {"id": sid, "rule_id": rule_id, "ends_at": ends.isoformat(), "reason": reason}


def end_silence(connection, target: dict, silence_id: str, *, actor: str,
                now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or _now()
    row = connection.execute(sql(
        "UPDATE app_alert_silences SET expired_at = :now, expired_by = :by WHERE id = :i "
        "AND target_id = :t AND expired_at IS NULL RETURNING rule_id"),
        {"i": silence_id, "t": target["id"], "now": now, "by": actor}).first()
    if not row:
        raise LookupError("No such silence")
    rule = _rule_dict(connection.execute(sql("SELECT * FROM app_alert_rules WHERE id = :i"),
                                         {"i": row[0]}).mappings().first())
    _audit(connection, rule, "unsilenced", None, None, actor, "", now)
    _suppress(connection, target, now, active_silences(connection, target["id"], now))
    return {"id": silence_id, "ended": True}


def export_rules(rule_rows: list[dict[str, Any]], group: str = "boss") -> dict[str, Any]:
    """The rules as a Prometheus rule file (`groups:`), for Prometheus or Grafana alerting."""
    from .promql_lite import format_duration

    return {"groups": [{"name": group, "rules": [
        {"alert": r["alert"], "expr": r["expr"],
         "for": format_duration(int(r.get("for_seconds") or 0) * 1000),
         "labels": {k: v for k, v in (r.get("labels") or {}).items()},
         "annotations": {k: v for k, v in (r.get("annotations") or {}).items() if v}}
        for r in rule_rows if r.get("enabled", True)]}]}


__all__ = ["BUILTIN", "KINDS", "NO_MODEL_WORD", "RESOLVE_WINDOW", "RUNBOOKS", "RuleRefused",
           "auto_responding", "end_silence", "ensure_rules", "evaluate_rule", "evaluate_target",
           "export_rules", "fingerprint", "group_of", "incident_key", "local_store",
           "record_signal", "render", "retire_app_wide_response", "silence", "sweep",
           "triage_alert", "triage_pending", "update_rule"]
