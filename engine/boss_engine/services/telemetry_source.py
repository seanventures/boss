"""RB-2 — the `TelemetrySource` seam + canonical shapes (spec 12 §2).

Every observability tool (Splunk, Grafana, Datadog, OTel, Loki, CloudWatch …) is read through ONE
uniform surface and normalized into canonical shapes, so the mining pipeline (`telemetry_mining`) is
100% source-agnostic — a new tool is a new adapter, never a pipeline change. Connectors come two ways
(spec 12 §2.2): MCP-backed (wrap an existing MCP server via the registry) or a native skill adapter for
tools without MCP. Auth is always a scoped, read-only SecretRef — raw telemetry never leaves the source
un-distilled, and creds never enter agent context.

This module ships the canonical shapes, the Protocol, and the first two adapters: an in-memory source
(for dogfooding/tests) and a log-export file source (NDJSON / plain lines — the lowest-common-denominator
"export from Splunk/anything" on-ramp).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class TimeWindow:
    start: float          # epoch seconds
    end: float

    def contains(self, ts: float) -> bool:
        return self.start <= ts <= self.end


@dataclass
class LogEvent:
    ts: float
    message: str
    service: str = ""
    level: str = "info"          # debug|info|warn|error|fatal
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricPoint:
    ts: float
    value: float


@dataclass
class MetricSeries:
    name: str
    points: list[MetricPoint]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Span:
    trace_id: str
    name: str
    service: str
    duration_ms: float
    status: str = "ok"           # ok|error


@dataclass
class Alert:
    id: str
    title: str
    service: str
    ts: float
    severity: str = "warning"    # info|warning|critical


class TelemetrySource(Protocol):
    """The one read surface every connector implements. Bounded (window + limit); read-only."""
    kind: str

    def query_logs(self, q: str, window: TimeWindow, *, limit: int = 5000) -> list[LogEvent]: ...
    def query_metrics(self, q: str, window: TimeWindow) -> list[MetricSeries]: ...
    def query_traces(self, q: str, window: TimeWindow, *, limit: int = 2000) -> list[Span]: ...
    def list_alerts(self, window: TimeWindow) -> list[Alert]: ...
    def comment_back(self, ref: str, body: str) -> bool: ...     # close the loop on the source


# --- adapters ---------------------------------------------------------------
class InMemorySource:
    """A source backed by in-memory events — for dogfooding, tests, and replaying a captured batch."""
    kind = "memory"

    def __init__(self, logs: Optional[list[LogEvent]] = None, metrics: Optional[list[MetricSeries]] = None,
                 traces: Optional[list[Span]] = None, alerts: Optional[list[Alert]] = None):
        self._logs = logs or []
        self._metrics = metrics or []
        self._traces = traces or []
        self._alerts = alerts or []

    def query_logs(self, q: str, window: TimeWindow, *, limit: int = 5000) -> list[LogEvent]:
        out = [e for e in self._logs if window.contains(e.ts) and (not q or q.lower() in e.message.lower())]
        return out[:limit]

    def query_metrics(self, q: str, window: TimeWindow) -> list[MetricSeries]:
        return [MetricSeries(s.name, [p for p in s.points if window.contains(p.ts)], s.labels)
                for s in self._metrics if not q or q.lower() in s.name.lower()]

    def query_traces(self, q: str, window: TimeWindow, *, limit: int = 2000) -> list[Span]:
        return [s for s in self._traces if window.contains(0) or True][:limit]

    def list_alerts(self, window: TimeWindow) -> list[Alert]:
        return [a for a in self._alerts if window.contains(a.ts)]

    def comment_back(self, ref: str, body: str) -> bool:
        return False          # in-memory source has nothing to write back to


_LEVELS = ("fatal", "critical", "error", "warn", "warning", "info", "debug", "trace")


class LogExportSource:
    """Reads a log EXPORT file (NDJSON with {ts,message,service,level,...} or plain text lines) into
    canonical LogEvents — the lowest-common-denominator connector: 'export from anything, point BOSS at
    it.' A real Splunk/Datadog connector implements the same surface against the live API."""
    kind = "log_export"

    def __init__(self, path: str):
        self.path = path

    @staticmethod
    def _parse_line(line: str, idx: int) -> Optional[LogEvent]:
        line = line.rstrip("\n")
        if not line.strip():
            return None
        if line.lstrip().startswith("{"):
            try:
                d = json.loads(line)
                return LogEvent(
                    ts=float(d.get("ts") or d.get("timestamp") or idx),
                    message=str(d.get("message") or d.get("msg") or d.get("log") or line),
                    service=str(d.get("service") or d.get("app") or ""),
                    level=str(d.get("level") or d.get("severity") or "info").lower(),
                    fields={k: v for k, v in d.items()
                            if k not in ("ts", "timestamp", "message", "msg", "log", "service", "app", "level", "severity")},
                )
            except json.JSONDecodeError:
                pass
        # plain line: sniff a level token, use line index as a monotonic ts
        low = line.lower()
        level = next((lv for lv in _LEVELS if re.search(rf"\b{lv}\b", low)), "info")
        return LogEvent(ts=float(idx), message=line, level="warn" if level == "warning" else level)

    def query_logs(self, q: str, window: TimeWindow, *, limit: int = 5000) -> list[LogEvent]:
        out: list[LogEvent] = []
        with open(self.path, errors="ignore") as f:
            for i, line in enumerate(f):
                ev = self._parse_line(line, i)
                if ev and (not q or q.lower() in ev.message.lower()):
                    out.append(ev)
                    if len(out) >= limit:
                        break
        return out

    def query_metrics(self, q, window): return []
    def query_traces(self, q, window, *, limit=2000): return []
    def list_alerts(self, window): return []
    def comment_back(self, ref, body): return False
