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
