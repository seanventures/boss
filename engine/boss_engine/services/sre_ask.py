"""NL observability question → query *plan* only.

Deterministic keyword map. No LLM. Never executes PromQL, Datadog, or any
live connector. Unmapped questions return status=unknown with a reason —
never a fabricated query.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# First matching rule wins. Queries are canned templates, not invented RCA.
_CONNECTOR_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("datadog", "ddog"), "datadog"),
    (("loki", "logql"), "loki"),
    (("splunk", "spl"), "splunk"),
    (("pagerduty",), "pagerduty"),
    (("cloudwatch",), "cloudwatch"),
    (("grafana",), "grafana"),
    (("prometheus", "promql"), "prometheus"),
)

_CLASS_RULES: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (
        ("error rate", "5xx", "http errors", "http error"),
        "prometheus",
        "metrics",
        'sum(rate(http_requests_total{code=~"5.."}[5m]))',
    ),
    (
        ("latency", "p99", "p95"),
        "prometheus",
        "metrics",
        "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
    ),
    (("cpu",), "prometheus", "metrics", "rate(process_cpu_seconds_total[5m])"),
    (("memory",), "prometheus", "metrics", "process_resident_memory_bytes"),
    (("logs", "log lines"), "loki", "logs", '{job=~".+"}'),
    (("alerts", "firing", "pages"), "pagerduty", "alerts", "list incidents"),
)

_DEFAULT_QUERY: dict[str, tuple[str, str]] = {
    "prometheus": ("metrics", "up"),
    "loki": ("logs", '{job=~".+"}'),
    "datadog": ("metrics", "avg:trace.http.request{*}"),
    "grafana": ("metrics", "up"),
    "splunk": ("logs", "index=* error"),
    "pagerduty": ("alerts", "list incidents"),
    "cloudwatch": ("logs", "fields @timestamp, @message"),
}

_WINDOW_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:last\s+)?5\s*(?:m|min|minutes?)\b", re.I), "5m"),
    (re.compile(r"\b(?:last\s+)?15\s*(?:m|min|minutes?)\b", re.I), "15m"),
    (re.compile(r"\b(?:last\s+)?6\s*(?:h|hours?)\b", re.I), "6h"),
    (re.compile(r"\b(?:last\s+)?(?:24\s*(?:h|hours?)|day|yesterday)\b", re.I), "24h"),
    (re.compile(r"\b(?:last\s+)?(?:7\s*(?:d|days?)|week)\b", re.I), "7d"),
    (re.compile(r"\b(?:last(?:\s+1)?\s+hour|1\s*h|last hour)\b", re.I), "1h"),
)

_SERVICE_RE = re.compile(
    r"\b(?:for(?:\s+the)?|service)\s+([A-Za-z0-9_.:-]+?)(?:\s+service)?\b",
    re.I,
)
_SERVICE_STOP = frozenset({"the", "a", "an", "last", "my", "our", "this"})


def _haystack(question: str) -> str:
    return " ".join((question or "").lower().split())


def _match_connector(hay: str) -> Optional[str]:
    for words, kind in _CONNECTOR_KEYWORDS:
        if any(word in hay for word in words):
            return kind
    return None


def _match_class(hay: str) -> Optional[tuple[str, str, str, str]]:
    for words, kind, capability, query in _CLASS_RULES:
        if any(word in hay for word in words):
            return words[0], kind, capability, query
    return None


def _match_window(hay: str) -> Optional[str]:
    for pattern, duration in _WINDOW_RES:
        if pattern.search(hay):
            return duration
    return None


def _service(question: str) -> Optional[str]:
    match = _SERVICE_RE.search(question or "")
    if not match:
        return None
    value = match.group(1).strip(".:-")
    if not value or value.lower() in _SERVICE_STOP:
        return None
    return value


def _specialize(kind: str, query: str, service: Optional[str]) -> str:
    if not service:
        return query
    if kind == "loki" and query.startswith("{") and "service=" not in query:
        return f'{{service="{service}"}}'
    if kind == "datadog" and "{*}" in query:
        return query.replace("{*}", f"{{service:{service}}}")
    if kind == "prometheus" and "{" in query and "service=" not in query:
        return query.replace("{", f'{{service="{service}",', 1)
    return query


def plan_query(project_id: str, question: str) -> dict[str, Any]:
    """Translate a natural-language question into a connector query plan.

    Never executes the plan. Unknown questions stay unknown.
    """
    pid = str(project_id or "").strip()
    raw_question = str(question or "").strip()
    hay = _haystack(raw_question)
    connector = _match_connector(hay)
    classified = _match_class(hay)
    window = _match_window(hay)
    service = _service(raw_question)

    if connector is None and classified is None:
        return {
            "status": "unknown",
            "reason": "no observability connector or metric class matched",
            "project_id": pid,
            "question": raw_question,
            "executed": False,
            "public_ga": False,
            "connector_kind": None,
            "suggested_query": None,
            "window": None,
        }

    if connector is None and classified is not None:
        connector = classified[1]
        capability = classified[2]
        query = classified[3]
        matched = classified[0]
    elif classified is not None and classified[1] == connector:
        capability = classified[2]
        query = classified[3]
        matched = classified[0]
    else:
        capability, query = _DEFAULT_QUERY[connector]
        matched = connector

    query = _specialize(connector, query, service)
    duration = window or "1h"
    return {
        "status": "planned",
        "project_id": pid,
        "question": raw_question,
        "connector_kind": connector,
        "capability": capability,
        "suggested_query": query,
        "window": {
            "duration": duration,
            "source": "question" if window else "default",
        },
        "matched": matched,
        "service": service,
        "executed": False,
        "public_ga": False,
    }
