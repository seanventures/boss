"""RB-9 — native connectors: Prometheus, Loki, and Grafana (spec 12 §2.2/§2.4).

Thin, read-only HTTP adapters that normalize each tool into the canonical shapes behind the one
`TelemetrySource` surface — the mining pipeline never knows which tool it's reading.

* `PrometheusSource` — PromQL over `/api/v1/query_range` (+ active alerts over `/api/v1/alerts`).
* `LokiSource` — LogQL over `/loki/api/v1/query_range`.
* `GrafanaSource` — the same two protocols through Grafana's **datasource proxy**
  (`/api/datasources/proxy/uid/<uid>/…`) with a Grafana API token, so one Grafana connection covers
  both metrics and logs — the "connect Grafana, it works" preset.

Auth is indirect by construction: config carries `bearer_env` — the NAME of the env var holding the
token (populated from the ops secret store on deploy) — never the credential itself. Every query is
bounded (window + limit). HTTP goes through module-level `_http_json` so tests stub the wire, not the
adapters.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Optional

from .telemetry_source import Alert, LogEvent, MetricPoint, MetricSeries, Span, TimeWindow

_TIMEOUT = 20


def _http_json(url: str, *, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """One tiny wire function (tests monkeypatch this)."""
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 — operator-configured URL
        return json.loads(r.read().decode())


def _auth_headers(bearer_env: str = "") -> dict[str, str]:
    """Resolve the token by env-var NAME at call time — creds never live in config/agent context."""
    if not bearer_env:
        return {}
    token = os.environ.get(bearer_env, "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _q(params: dict[str, Any]) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


# --- Prometheus ---------------------------------------------------------------
class PrometheusSource:
    """PromQL metrics + active alerts from a Prometheus-compatible API (incl. Thanos/Mimir/VictoriaMetrics)."""
    kind = "prometheus"

    def __init__(self, base_url: str, *, bearer_env: str = "", step_seconds: int = 60,
                 proxy_prefix: str = ""):
        self.base = base_url.rstrip("/") + proxy_prefix
        self.bearer_env = bearer_env
        self.step = step_seconds

    def query_metrics(self, q: str, window: TimeWindow) -> list[MetricSeries]:
        if not q:
            return []
        url = (f"{self.base}/api/v1/query_range?"
               f"{_q({'query': q, 'start': window.start, 'end': window.end, 'step': self.step})}")
        data = _http_json(url, headers=_auth_headers(self.bearer_env))
        out: list[MetricSeries] = []
        for series in (data.get("data", {}) or {}).get("result", []):
            labels = {k: str(v) for k, v in (series.get("metric") or {}).items()}
            name = labels.pop("__name__", q)
            points = [MetricPoint(ts=float(ts), value=float(v))
                      for ts, v in series.get("values", [])]
            out.append(MetricSeries(name=name, points=points, labels=labels))
        return out

    def list_alerts(self, window: TimeWindow) -> list[Alert]:
        data = _http_json(f"{self.base}/api/v1/alerts", headers=_auth_headers(self.bearer_env))
        out: list[Alert] = []
        for i, a in enumerate((data.get("data", {}) or {}).get("alerts", [])):
            labels = a.get("labels") or {}
            out.append(Alert(
                id=str(i), title=labels.get("alertname", "alert"),
                service=labels.get("service", labels.get("job", "")),
                ts=window.end, severity=labels.get("severity", "warning")))
        return out

    def query_logs(self, q, window, *, limit=5000):
        return []
    def query_traces(self, q, window, *, limit=2000):
        return []
    def comment_back(self, ref, body):
        return False


# --- Loki ---------------------------------------------------------------------
class LokiSource:
    """LogQL log lines from Loki's query_range API, normalized to LogEvents."""
    kind = "loki"

    def __init__(self, base_url: str, *, bearer_env: str = "", proxy_prefix: str = ""):
        self.base = base_url.rstrip("/") + proxy_prefix
        self.bearer_env = bearer_env

    def query_logs(self, q: str, window: TimeWindow, *, limit: int = 5000) -> list[LogEvent]:
        query = q or '{level=~".+"}'
        url = (f"{self.base}/loki/api/v1/query_range?"
               f"{_q({'query': query, 'start': int(window.start * 1e9), 'end': int(window.end * 1e9), 'limit': limit})}")
        data = _http_json(url, headers=_auth_headers(self.bearer_env))
        out: list[LogEvent] = []
        for stream in (data.get("data", {}) or {}).get("result", []):
            labels = stream.get("stream") or {}
            service = labels.get("service", labels.get("app", labels.get("job", "")))
            level = str(labels.get("level", "info")).lower()
            for ts_ns, line in stream.get("values", []):
                out.append(LogEvent(ts=float(ts_ns) / 1e9, message=line, service=service,
                                    level="warn" if level == "warning" else level))
                if len(out) >= limit:
                    return out
        return out

    def query_metrics(self, q, window):
        return []
    def query_traces(self, q, window, *, limit=2000):
        return []
    def list_alerts(self, window):
        return []
    def comment_back(self, ref, body):
        return False


# --- Grafana ------------------------------------------------------------------
class GrafanaSource:
    """One Grafana connection covering metrics AND logs via the datasource proxy: the same
    Prometheus/Loki request shapes, routed through `/api/datasources/proxy/uid/<uid>` with a
    Grafana API token. Config: base_url + prometheus_uid and/or loki_uid + bearer_env."""
    kind = "grafana"

    def __init__(self, base_url: str, *, prometheus_uid: str = "", loki_uid: str = "",
                 bearer_env: str = "", step_seconds: int = 60):
        base = base_url.rstrip("/")
        self._prom = PrometheusSource(
            base, bearer_env=bearer_env, step_seconds=step_seconds,
            proxy_prefix=f"/api/datasources/proxy/uid/{prometheus_uid}") if prometheus_uid else None
        self._loki = LokiSource(
            base, bearer_env=bearer_env,
            proxy_prefix=f"/api/datasources/proxy/uid/{loki_uid}") if loki_uid else None

    def query_metrics(self, q: str, window: TimeWindow) -> list[MetricSeries]:
        return self._prom.query_metrics(q, window) if self._prom else []

    def list_alerts(self, window: TimeWindow) -> list[Alert]:
        return self._prom.list_alerts(window) if self._prom else []

    def query_logs(self, q: str, window: TimeWindow, *, limit: int = 5000) -> list[LogEvent]:
        return self._loki.query_logs(q, window, limit=limit) if self._loki else []

    def query_traces(self, q, window, *, limit=2000) -> list[Span]:
        return []
    def comment_back(self, ref, body):
        return False


# --- shared wire for POST/NDJSON APIs (tests stub these too) ------------------
def _http_json_post(url: str, body: dict[str, Any], *,
                    headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310
        return json.loads(r.read().decode())


def _http_ndjson_post(url: str, form: dict[str, Any], *,
                      headers: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=headers or {})
    out: list[dict[str, Any]] = []
    with urllib.request.urlopen(req, timeout=_TIMEOUT * 3) as r:  # noqa: S310
        for line in r.read().decode().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


# --- Splunk -------------------------------------------------------------------
class SplunkSource:
    """SPL search via /services/search/jobs/export (blocking export, NDJSON), normalized to
    LogEvents. Auth: Splunk token via bearer_env. Config: base_url, optional index."""
    kind = "splunk"

    def __init__(self, base_url: str, *, bearer_env: str = "", index: str = ""):
        self.base = base_url.rstrip("/")
        self.bearer_env = bearer_env
        self.index = index

    def query_logs(self, q: str, window: TimeWindow, *, limit: int = 5000) -> list[LogEvent]:
        spl = q or (f"search index={self.index}" if self.index else "search index=main")
        if not spl.lstrip().startswith(("search", "|")):
            spl = "search " + spl
        rows = _http_ndjson_post(
            f"{self.base}/services/search/jobs/export?output_mode=json",
            {"search": spl, "earliest_time": window.start, "latest_time": window.end,
             "count": limit},
            headers=_auth_headers(self.bearer_env))
        out: list[LogEvent] = []
        for row in rows[:limit]:
            res = row.get("result") or row
            if not isinstance(res, dict) or "_raw" not in res:
                continue
            try:
                ts = float(res.get("_time", window.start))
            except (TypeError, ValueError):
                ts = window.start
            out.append(LogEvent(ts=ts, message=str(res["_raw"]),
                                service=str(res.get("source", res.get("host", ""))),
                                level=str(res.get("log_level", "info")).lower()))
        return out

    def query_metrics(self, q, window):
        return []
    def query_traces(self, q, window, *, limit=2000):
        return []
    def list_alerts(self, window):
        return []
    def comment_back(self, ref, body):
        return False


# --- Datadog ------------------------------------------------------------------
class DatadogSource:
    """Datadog logs (v2 events search) + metrics (v1 query). Auth: DD-API-KEY/DD-APPLICATION-KEY
    via api_key_env/app_key_env (env-var names, resolved at call time)."""
    kind = "datadog"

    def __init__(self, base_url: str = "https://api.datadoghq.com", *,
                 api_key_env: str = "DD_API_KEY", app_key_env: str = "DD_APP_KEY"):
        self.base = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.app_key_env = app_key_env

    def _headers(self) -> dict[str, str]:
        return {"DD-API-KEY": os.environ.get(self.api_key_env, ""),
                "DD-APPLICATION-KEY": os.environ.get(self.app_key_env, "")}

    def query_logs(self, q: str, window: TimeWindow, *, limit: int = 5000) -> list[LogEvent]:
        data = _http_json_post(
            f"{self.base}/api/v2/logs/events/search",
            {"filter": {"query": q or "*", "from": int(window.start * 1000),
                        "to": int(window.end * 1000)},
             "page": {"limit": min(limit, 1000)}},
            headers=self._headers())
        out: list[LogEvent] = []
        for item in data.get("data", []):
            attrs = item.get("attributes") or {}
            out.append(LogEvent(
                ts=float(attrs.get("timestamp_epoch", window.start)),
                message=str(attrs.get("message", "")),
                service=str(attrs.get("service", "")),
                level=str(attrs.get("status", "info")).lower()))
        return out

    def query_metrics(self, q: str, window: TimeWindow) -> list[MetricSeries]:
        if not q:
            return []
        data = _http_json(
            f"{self.base}/api/v1/query?{_q({'from': int(window.start), 'to': int(window.end), 'query': q})}",
            headers=self._headers())
        out: list[MetricSeries] = []
        for s in data.get("series", []):
            points = [MetricPoint(ts=p[0] / 1000.0, value=float(p[1]))
                      for p in (s.get("pointlist") or []) if p[1] is not None]
            out.append(MetricSeries(name=s.get("metric", q), points=points,
                                    labels={"scope": s.get("scope", "")}))
        return out

    def query_traces(self, q, window, *, limit=2000):
        return []
    def list_alerts(self, window):
        return []
    def comment_back(self, ref, body):
        return False


# --- CloudWatch (boto3) -------------------------------------------------------
class CloudWatchSource:
    """AWS CloudWatch Logs (filter_log_events) + Metrics (get_metric_data) via boto3 — auth by the
    standard AWS credential chain (env/instance role), never inline. Config: log_group, region,
    optional metric namespace queries."""
    kind = "cloudwatch"

    def __init__(self, log_group: str, *, region: str = "us-east-1", client_factory=None):
        self.log_group = log_group
        self.region = region
        # injectable for tests; defaults to boto3 at call time so importing this module never
        # requires AWS creds
        self._factory = client_factory

    def _client(self, service: str):
        if self._factory:
            return self._factory(service)
        import boto3
        return boto3.client(service, region_name=self.region)

    def query_logs(self, q: str, window: TimeWindow, *, limit: int = 5000) -> list[LogEvent]:
        kwargs = {"logGroupName": self.log_group, "startTime": int(window.start * 1000),
                  "endTime": int(window.end * 1000), "limit": min(limit, 10000)}
        if q:
            kwargs["filterPattern"] = q
        resp = self._client("logs").filter_log_events(**kwargs)
        return [LogEvent(ts=e["timestamp"] / 1000.0, message=e.get("message", ""),
                         service=e.get("logStreamName", ""))
                for e in resp.get("events", [])][:limit]

    def query_metrics(self, q, window):
        return []
    def query_traces(self, q, window, *, limit=2000):
        return []
    def list_alerts(self, window):
        return []
    def comment_back(self, ref, body):
        return False


# --- PagerDuty ----------------------------------------------------------------
class PagerDutySource:
    """PagerDuty incidents as canonical Alerts. Auth: token via token_env
    (Authorization: Token token=<...>)."""
    kind = "pagerduty"

    def __init__(self, base_url: str = "https://api.pagerduty.com", *,
                 token_env: str = "PAGERDUTY_TOKEN"):
        self.base = base_url.rstrip("/")
        self.token_env = token_env

    def list_alerts(self, window: TimeWindow) -> list[Alert]:
        from datetime import datetime, timezone
        since = datetime.fromtimestamp(window.start, tz=timezone.utc).isoformat()
        until = datetime.fromtimestamp(window.end, tz=timezone.utc).isoformat()
        token = os.environ.get(self.token_env, "")
        data = _http_json(
            f"{self.base}/incidents?{_q({'since': since, 'until': until, 'limit': 100})}",
            headers={"Authorization": f"Token token={token}"} if token else {})
        out: list[Alert] = []
        for inc in data.get("incidents", []):
            sev = {"P1": "critical", "P2": "critical"}.get(
                ((inc.get("priority") or {}).get("summary") or ""), "warning")
            out.append(Alert(id=str(inc.get("id", "")), title=inc.get("title", "incident"),
                             service=((inc.get("service") or {}).get("summary", "")),
                             ts=window.end, severity=sev))
        return out

    def query_logs(self, q, window, *, limit=5000):
        return []
    def query_metrics(self, q, window):
        return []
    def query_traces(self, q, window, *, limit=2000):
        return []
    def comment_back(self, ref, body):
        return False


# --- catalog + readiness (HTTP surface helpers) -------------------------------
# Static inventory of connector kinds the control plane can construct. Distinct from
# project-registered sources at GET /v1/reliability/sources.
CONNECTOR_CATALOG: list[dict[str, Any]] = [
    {
        "kind": "prometheus",
        "title": "Prometheus",
        "capabilities": ["metrics", "alerts"],
        "auth": "bearer_env",
        "required_config": ["base_url"],
        "optional_config": ["bearer_env", "step_seconds"],
    },
    {
        "kind": "loki",
        "title": "Loki",
        "capabilities": ["logs"],
        "auth": "bearer_env",
        "required_config": ["base_url"],
        "optional_config": ["bearer_env"],
    },
    {
        "kind": "grafana",
        "title": "Grafana (datasource proxy)",
        "capabilities": ["metrics", "logs", "alerts"],
        "auth": "bearer_env",
        "required_config": ["base_url"],
        "optional_config": ["prometheus_uid", "loki_uid", "bearer_env", "step_seconds"],
    },
    {
        "kind": "splunk",
        "title": "Splunk",
        "capabilities": ["logs"],
        "auth": "bearer_env",
        "required_config": ["base_url"],
        "optional_config": ["bearer_env", "index"],
    },
    {
        "kind": "datadog",
        "title": "Datadog",
        "capabilities": ["logs", "metrics"],
        "auth": "api_key_env+app_key_env",
        "required_config": [],
        "optional_config": ["base_url", "api_key_env", "app_key_env"],
        "required_env_present": True,
    },
    {
        "kind": "cloudwatch",
        "title": "AWS CloudWatch",
        "capabilities": ["logs"],
        "auth": "aws_credential_chain",
        "required_config": ["log_group"],
        "optional_config": ["region"],
    },
    {
        "kind": "pagerduty",
        "title": "PagerDuty",
        "capabilities": ["alerts"],
        "auth": "token_env",
        "required_config": [],
        "optional_config": ["base_url", "token_env"],
        "required_env_present": True,
    },
    {
        "kind": "memory",
        "title": "In-memory (tests / dogfood)",
        "capabilities": ["logs", "metrics", "traces", "alerts"],
        "auth": "none",
        "required_config": [],
        "optional_config": ["logs", "metrics", "traces", "alerts"],
    },
    {
        "kind": "log_export",
        "title": "Log export file",
        "capabilities": ["logs"],
        "auth": "none",
        "required_config": ["path"],
        "optional_config": [],
    },
]

_QUERY_KINDS = frozenset({"metrics", "logs", "traces", "alerts"})


class ConnectorNotReady(Exception):
    """Fail-closed: connector config or required credentials are missing.

    `status_code` is a hint for the HTTP layer (422 misconfig, 503 unavailable).
    """

    def __init__(self, message: str, *, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def list_connector_catalog() -> list[dict[str, Any]]:
    """Return the static catalog of connector kinds BOSS can construct."""
    return [dict(item) for item in CONNECTOR_CATALOG]


def ensure_connector_ready(spec: dict[str, Any]) -> None:
    """Fail closed when a connector cannot be used safely — no silent empty/fake data.

    Rules:
    * unknown kind → 422
    * missing required config fields (base_url, log_group, path, …) → 422
    * bearer_env / token_env / api_key_env named but empty in the process env → 422
    * connectors that always require credentials (Datadog, PagerDuty) with empty keys → 422
    """
    if not isinstance(spec, dict):
        raise ConnectorNotReady("source spec must be an object", status_code=422)
    kind = (spec.get("kind") or "").strip()
    if not kind:
        raise ConnectorNotReady("source.kind is required", status_code=422)

    catalog = {c["kind"]: c for c in CONNECTOR_CATALOG}
    if kind not in catalog:
        raise ConnectorNotReady(f"unsupported connector kind {kind!r}", status_code=422)
    meta = catalog[kind]

    for field in meta.get("required_config") or []:
        if not str(spec.get(field) or "").strip():
            raise ConnectorNotReady(
                f"connector {kind!r} requires config field {field!r}",
                status_code=422,
            )

    # Optional named env-var auth: if the operator declared the name, the value must resolve.
    for env_key in ("bearer_env", "token_env"):
        env_name = (spec.get(env_key) or "").strip()
        if env_name and not os.environ.get(env_name, "").strip():
            raise ConnectorNotReady(
                f"connector {kind!r}: env var {env_name!r} ({env_key}) is not set",
                status_code=422,
            )

    if kind == "datadog":
        api_env = (spec.get("api_key_env") or "DD_API_KEY").strip()
        app_env = (spec.get("app_key_env") or "DD_APP_KEY").strip()
        missing = [n for n in (api_env, app_env) if not os.environ.get(n, "").strip()]
        if missing:
            raise ConnectorNotReady(
                f"connector 'datadog' requires credentials in env: {', '.join(missing)}",
                status_code=422,
            )

    if kind == "pagerduty":
        token_env = (spec.get("token_env") or "PAGERDUTY_TOKEN").strip()
        if not os.environ.get(token_env, "").strip():
            raise ConnectorNotReady(
                f"connector 'pagerduty' requires credentials in env: {token_env}",
                status_code=422,
            )

    if kind == "grafana":
        if not (spec.get("prometheus_uid") or spec.get("loki_uid")):
            raise ConnectorNotReady(
                "connector 'grafana' requires prometheus_uid and/or loki_uid",
                status_code=422,
            )


def serialize_query_result(kind: str, rows: list[Any]) -> list[dict[str, Any]]:
    """Canonical shapes → JSON-friendly dicts for the HTTP response."""
    out: list[dict[str, Any]] = []
    if kind == "metrics":
        for series in rows:
            out.append({
                "name": series.name,
                "labels": dict(series.labels or {}),
                "points": [{"ts": p.ts, "value": p.value} for p in (series.points or [])],
            })
    elif kind == "logs":
        for event in rows:
            out.append({
                "ts": event.ts,
                "message": event.message,
                "service": event.service,
                "level": event.level,
                "fields": dict(getattr(event, "fields", None) or {}),
            })
    elif kind == "traces":
        for span in rows:
            out.append({
                "trace_id": span.trace_id,
                "name": span.name,
                "service": span.service,
                "duration_ms": span.duration_ms,
                "status": span.status,
            })
    elif kind == "alerts":
        for alert in rows:
            out.append({
                "id": alert.id,
                "title": alert.title,
                "service": alert.service,
                "ts": alert.ts,
                "severity": alert.severity,
            })
    else:
        raise ConnectorNotReady(
            f"query kind must be one of {sorted(_QUERY_KINDS)}",
            status_code=422,
        )
    return out


def run_source_query(source: Any, kind: str, query: str, window: TimeWindow, *,
                     limit: int = 5000) -> list[dict[str, Any]]:
    """Dispatch a single kind query against a constructed TelemetrySource."""
    kind = (kind or "").strip().lower()
    if kind not in _QUERY_KINDS:
        raise ConnectorNotReady(
            f"query kind must be one of {sorted(_QUERY_KINDS)}",
            status_code=422,
        )
    try:
        if kind == "metrics":
            rows = source.query_metrics(query or "", window)
        elif kind == "logs":
            rows = source.query_logs(query or "", window, limit=limit)
        elif kind == "traces":
            rows = source.query_traces(query or "", window, limit=limit)
        else:
            rows = source.list_alerts(window)
    except ConnectorNotReady:
        raise
    except Exception as error:  # noqa: BLE001 — map wire failures to 503 at the edge
        raise ConnectorNotReady(
            f"telemetry source {getattr(source, 'kind', '?')!r} query failed: {error}",
            status_code=503,
        ) from error
    return serialize_query_result(kind, rows or [])
