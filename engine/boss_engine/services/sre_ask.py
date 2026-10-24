"""NL observability question → a query *plan*, written by a MODEL (BOSSAPP-598, spec §8.3, G8).

THE KEYWORD MAP IS A HINT, NEVER THE DECIDER. People do not ask the same question the same way;
the map below matched words ("latency", "5xx") to canned queries and answered with whichever
rule hit first. Now a model reads the question and writes the plan (connector, query, window) as
JSON; the keyword reading is passed to it as "BOSS's guess", which it may ignore. BOSS then
checks what the model wrote: a PromQL query must parse, and is scoped to the project ON THE
PARSED QUERY (promql_lite.inject_scope) — a query that reads another project's series cannot be
written. With no model the answer says so (status `no_model`) and there is no keyword fallback.
Never executes the plan.
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


PLAN_SYSTEM = (
    "You turn an observability question into ONE query plan. Answer with ONLY a JSON object: "
    '{"status": "planned" | "unknown", "connector_kind": "prometheus" | "loki" | "datadog" | '
    '"grafana" | "splunk" | "pagerduty" | "cloudwatch", "capability": "metrics" | "logs" | '
    '"alerts", "query": "<the query in that connector\'s language>", "window": "5m" | "15m" | '
    '"1h" | "6h" | "24h" | "7d", "service": "<service or empty>", "reason": "<one sentence>"}. '
    "Use status unknown, with a reason, when the question is not about the system's "
    "telemetry. Never invent a metric the question does not need. BOSS's keyword guess is a "
    "hint you may ignore.")
_WINDOWS = ("5m", "15m", "1h", "6h", "24h", "7d")
_KINDS = tuple(sorted({k for _w, k in _CONNECTOR_KEYWORDS} | set(_DEFAULT_QUERY)))


def _project_adapter(connection, settings, project_id: str, question: str):
    from .agent_decision import _routed

    adapter, _raw = _routed(connection, settings, project_id, "sre_ask", question)
    return adapter


def plan_query(project_id: str, question: str, *, connection: Any = None, settings: Any = None,
               adapter: Any = None) -> dict[str, Any]:
    """A natural-language question as a connector query plan, written by a model and checked
    by BOSS. Never executes the plan. No model: `no_model`, said plainly."""
    import json as _json

    from . import provider_limits
    from .agent_decision import NO_MODEL_REPLY, _provider_name

    def parse(text_out: str):
        import re as _re

        for cand in [text_out] + _re.findall(r"\{.*\}", text_out or "", _re.S):
            try:
                got = _json.loads(cand)
            except ValueError:
                continue
            if isinstance(got, dict):
                return got
        return None

    pid = str(project_id or "").strip()
    raw_question = str(question or "").strip()
    guess = keyword_guess(pid, raw_question)
    base = {"project_id": pid, "question": raw_question, "executed": False, "public_ga": False,
            "boss_guess": {k: guess.get(k) for k in ("status", "connector_kind",
                                                      "suggested_query", "window")}}
    no_model_reason = NO_MODEL_REPLY
    try:
        if adapter is None:
            adapter = _project_adapter(connection, settings, pid, raw_question)
        prompt = (f"QUESTION:\n<<<\n{raw_question[:2000]}\n>>>\n\nBOSS'S KEYWORD GUESS (a hint): "
                  + _json.dumps(base["boss_guess"]))
        out = adapter.create_text_response(prompt, system_prompt=PLAN_SYSTEM)
        text_out = out[1] if isinstance(out, tuple) and len(out) > 1 else str(out or "")
        answer = parse(str(text_out or ""))
    except Exception as exc:  # noqa: BLE001 — no model is an answer, never a crash
        answer = None
        if provider_limits.credit_exhausted(str(exc)):
            who = _provider_name(adapter) or "its provider"
            no_model_reason = (f"BOSS could not get an answer: credit balance too low on "
                               f"{who}. Add credit there (or connect a different model in "
                               "Repo and models), then ask again.")
    if not isinstance(answer, dict) or not answer.get("status"):
        return {**base, "status": "no_model", "reason": no_model_reason, "connector_kind": None,
                "suggested_query": None, "window": None}
    if answer.get("status") != "planned":
        return {**base, "status": "unknown", "reason": str(answer.get("reason") or
                                                          "not an observability question")[:300],
                "connector_kind": None, "suggested_query": None, "window": None}
    kind = str(answer.get("connector_kind") or "")
    query = str(answer.get("query") or "").strip()[:2000]
    if kind not in _KINDS or not query:
        return {**base, "status": "unknown", "reason": "The model's plan named no connector "
                "BOSS reads, or no query", "connector_kind": None, "suggested_query": None,
                "window": None}
    if kind in ("prometheus", "grafana"):
        from . import promql_lite as pq

        try:
            query = pq.inject_scope(query, {"project": pid}) if pid else pq.format_expr(query)
        except pq.PromQLError as exc:
            return {**base, "status": "unknown", "reason": f"The model wrote PromQL BOSS "
                    f"cannot check ({exc}); nothing is suggested", "connector_kind": kind,
                    "suggested_query": None, "window": None}
    window = str(answer.get("window") or "1h")
    return {**base, "status": "planned", "connector_kind": kind,
            "capability": str(answer.get("capability") or _DEFAULT_QUERY.get(kind, ("metrics",
                                                                                   ""))[0]),
            "suggested_query": query,
            "window": {"duration": window if window in _WINDOWS else "1h", "source": "model"},
            "service": str(answer.get("service") or "") or None,
            "reason": str(answer.get("reason") or "")[:300], "decided_by": "model"}


def keyword_guess(project_id: str, question: str) -> dict[str, Any]:
    """BOSS's keyword reading of the question: a HINT for the model, never the answer."""
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
