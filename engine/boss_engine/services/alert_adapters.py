"""Alerts other tools send BOSS, in each tool's own shape, as ONE normalized event.

Each adapter is a PURE function ``(headers, raw body, source) -> [NormalizedEvent]`` that raises
``AdapterRefused`` with a sentence for anything it will not take. None of them calls the network
(the SNS subscription confirmation and signature check live in ``alert_ingest``). The state they
describe is MIRRORED by the source's own key (``external_ref``): BOSS owns the alert record, the
source owns whether it fires (spec D2).

  Alertmanager webhook v4   alerts[].fingerprint      status per alert     labels.service|job|app
  Grafana alerting          alerts[].fingerprint      status               labels; generator/silence/
                                                                          dashboard/panel URLs kept
  PagerDuty Events v2 /     dedup_key (made from the  event_action         labels.service |
    generic (BOSS's own)    summary when absent)                          payload.source
  Sentry                    issue id / metric alert   action               the source's mapping
  Datadog (BOSS's template) alert_id + aggreg_key     transition           tag service:
  CloudWatch via SNS        AlarmArn + dimensions     NewStateValue        dimension/alarm name
  Heartbeat                 the source                a ping               the source's service

The generic event IS PagerDuty Events v2 (plus Prometheus-style `labels` and `links`), so
anything that pages PagerDuty can send to BOSS unchanged.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

MAX_EVENTS = 200
SEVERITIES = ("critical", "warning", "info")


class AdapterRefused(ValueError):
    """A body BOSS will not take, with the sentence the sender gets back."""

    def __init__(self, word: str, status: int = 400):
        super().__init__(word)
        self.word, self.status = word, status


@dataclass
class NormalizedEvent:
    action: str                       # trigger | acknowledge | resolve | ping
    external_ref: str
    alertname: str
    summary: str = ""
    description: str = ""
    severity: str = "warning"
    service: Optional[str] = None
    environment: Optional[str] = None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    links: list[dict[str, str]] = field(default_factory=list)
    value: Optional[float] = None
    timestamp: Optional[float] = None  # epoch seconds, as the source said
    tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _json_body(raw: bytes) -> Any:
    try:
        return json.loads((raw or b"").decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        raise AdapterRefused("The body is not JSON") from None


def _s(value: Any, limit: int = 500) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _labels(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    return {_s(k, 100): _s(v, 300) for k, v in list(raw.items())[:60] if _s(k, 100)}


def severity(raw: Any) -> str:
    """Every tool's words for how bad it is, as BOSS's three."""
    word = _s(raw, 40).lower()
    if word in ("critical", "crit", "error", "high", "p1", "p2", "page", "fatal", "alarm",
                "disaster", "emergency"):
        return "critical"
    if word in ("info", "informational", "low", "p5", "ok", "debug"):
        return "info"
    return "warning"


def _ts(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) / (1000.0 if raw > 10_000_000_000 else 1.0)
    text = _s(raw, 60)
    try:
        if text.replace(".", "", 1).isdigit():
            return _ts(float(text))
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def _ref(*parts: Any) -> str:
    return hashlib.sha256("|".join(_s(p, 2000) for p in parts).encode()).hexdigest()[:24]


def _link(href: Any, text: str) -> Optional[dict[str, str]]:
    url = _s(href, 2000)
    return {"href": url, "text": text} if url.startswith(("http://", "https://")) else None


# --------------------------------------------------------------------------- #
# Alertmanager and Grafana
# --------------------------------------------------------------------------- #
def alertmanager(headers: Mapping[str, str], raw: bytes, source: Mapping[str, Any],
                 *, tool: str = "alertmanager") -> list[NormalizedEvent]:
    body = _json_body(raw)
    if not isinstance(body, Mapping) or not isinstance(body.get("alerts"), list):
        raise AdapterRefused("Not an Alertmanager webhook: no `alerts` list")
    out = []
    for a in body["alerts"][:MAX_EVENTS]:
        if not isinstance(a, Mapping):
            continue
        labels = _labels(a.get("labels"))
        ann = _labels(a.get("annotations"))
        status = _s(a.get("status") or body.get("status")).lower()
        if status not in ("firing", "resolved"):
            raise AdapterRefused(f"An alert's status is {status or 'missing'}; "
                                 "expected firing or resolved")
        links = [x for x in (_link(a.get("generatorURL"), "Source"),
                             _link(a.get("silenceURL"), "Silence"),
                             _link(a.get("dashboardURL"), "Dashboard"),
                             _link(a.get("panelURL"), "Panel")) if x]
        value = None
        values = a.get("values")
        if isinstance(values, Mapping) and values:
            try:
                value = float(next(iter(values.values())))
            except (TypeError, ValueError):
                value = None
        name = labels.get("alertname") or "ExternalAlert"
        out.append(NormalizedEvent(
            action="trigger" if status == "firing" else "resolve",
            external_ref=_s(a.get("fingerprint"), 120) or _ref(name, sorted(labels.items())),
            alertname=name[:120], summary=ann.get("summary") or ann.get("description") or name,
            description=ann.get("description", ""),
            severity=severity(labels.get("severity")),
            service=labels.get("service") or labels.get("job") or labels.get("app") or None,
            environment=labels.get("environment") or labels.get("env") or None,
            labels=labels, annotations=ann, links=links, value=value,
            timestamp=_ts(a.get("endsAt") if status == "resolved" else a.get("startsAt")),
            tool=tool))
    if not out:
        raise AdapterRefused("The webhook has no alerts in it")
    return out


def grafana(headers, raw, source) -> list[NormalizedEvent]:
    return alertmanager(headers, raw, source, tool="grafana")


# --------------------------------------------------------------------------- #
# PagerDuty Events v2: the generic event
# --------------------------------------------------------------------------- #
_PD_ACTIONS = {"trigger": "trigger", "acknowledge": "acknowledge", "resolve": "resolve"}


def pagerduty(headers, raw, source) -> list[NormalizedEvent]:
    body = _json_body(raw)
    items = body if isinstance(body, list) else [body]
    out = []
    for ev in items[:MAX_EVENTS]:
        if not isinstance(ev, Mapping):
            raise AdapterRefused("An event must be a JSON object")
        action = _PD_ACTIONS.get(_s(ev.get("event_action")).lower())
        if not action:
            raise AdapterRefused("event_action must be trigger, acknowledge or resolve")
        payload = ev.get("payload") if isinstance(ev.get("payload"), Mapping) else {}
        dedup = _s(ev.get("dedup_key"), 255)
        if action != "trigger" and not dedup:
            raise AdapterRefused(f"An {action} needs the dedup_key of the alert it ends")
        summary = _s(payload.get("summary"), 1024)
        if action == "trigger" and not summary:
            raise AdapterRefused("A trigger needs payload.summary")
        labels = _labels(ev.get("labels"))
        details = payload.get("custom_details")
        links = [x for x in (_link(lk.get("href"), _s(lk.get("text"), 80) or "Link")
                             for lk in (ev.get("links") or [])[:10] if isinstance(lk, Mapping))
                 if x]
        name = labels.get("alertname") or _s(payload.get("class"), 120) or summary[:80] \
            or "ExternalEvent"
        value = None
        if isinstance(details, Mapping) and isinstance(details.get("value"), (int, float)):
            value = float(details["value"])
        out.append(NormalizedEvent(
            action=action, external_ref=dedup or _ref(summary, payload.get("source")),
            alertname="".join(c if c.isalnum() or c in "_:" else "_" for c in name)[:120],
            summary=summary or name,
            description=(json.dumps(details, default=str)[:2000] if isinstance(details, Mapping)
                         else _s(details, 2000)),
            severity=severity(payload.get("severity")),
            service=labels.get("service") or _s(payload.get("source"), 200) or None,
            environment=labels.get("environment") or None, labels=labels,
            annotations={"summary": summary} if summary else {}, links=links, value=value,
            timestamp=_ts(payload.get("timestamp")), tool="pagerduty_v2"))
    return out


generic = pagerduty


# --------------------------------------------------------------------------- #
# Sentry
# --------------------------------------------------------------------------- #
def sentry(headers, raw, source) -> list[NormalizedEvent]:
    body = _json_body(raw)
    if not isinstance(body, Mapping):
        raise AdapterRefused("Not a Sentry webhook")
    resource = _s({k.lower(): v for k, v in headers.items()}.get("sentry-hook-resource")).lower()
    action = _s(body.get("action")).lower()
    data = body.get("data") if isinstance(body.get("data"), Mapping) else {}
    if "metric_alert" in data or resource == "metric_alert":
        ma = data.get("metric_alert") if isinstance(data.get("metric_alert"), Mapping) else {}
        rule = ma.get("alert_rule") if isinstance(ma.get("alert_rule"), Mapping) else {}
        title = _s(data.get("description_title") or ma.get("title") or rule.get("name"))
        return [NormalizedEvent(
            action="resolve" if action == "resolved" else "trigger",
            external_ref="metric:" + _s(ma.get("id") or rule.get("id") or _ref(title), 80),
            alertname="SentryMetricAlert", summary=title or "Sentry metric alert",
            description=_s(data.get("description_text"), 2000),
            severity="critical" if action == "critical" else "warning",
            links=[x for x in (_link(data.get("web_url"), "Sentry"),) if x], tool="sentry")]
    issue = data.get("issue") if isinstance(data.get("issue"), Mapping) else None
    event = data.get("event") if isinstance(data.get("event"), Mapping) else None
    if issue is not None:
        project = issue.get("project") if isinstance(issue.get("project"), Mapping) else {}
        return [NormalizedEvent(
            action="resolve" if action in ("resolved", "ignored", "archived") else "trigger",
            external_ref="issue:" + _s(issue.get("id"), 80),
            alertname="SentryIssue", summary=_s(issue.get("title")) or "Sentry issue",
            description=_s(issue.get("culprit"), 1000),
            severity=severity(issue.get("level") or "error"),
            service=_s(project.get("slug"), 200) or None,
            links=[x for x in (_link(issue.get("web_url") or issue.get("permalink"),
                                     "Sentry"),) if x], tool="sentry")]
    if event is not None:
        return [NormalizedEvent(
            action="trigger",
            external_ref="issue:" + _s(event.get("issue_id") or _ref(event.get("title")), 80),
            alertname="SentryIssue", summary=_s(event.get("title")) or "Sentry issue alert",
            description=_s(data.get("triggered_rule"), 500),
            severity=severity(event.get("level") or "error"),
            service=_s(event.get("project"), 200) or None,
            links=[x for x in (_link(event.get("web_url"), "Sentry"),) if x], tool="sentry")]
    raise AdapterRefused("A Sentry webhook BOSS does not read (issue, event_alert and "
                         "metric_alert are read)")


# --------------------------------------------------------------------------- #
# Datadog (BOSS supplies the payload template)
# --------------------------------------------------------------------------- #
DATADOG_TEMPLATE = {
    "alert_id": "$ALERT_ID", "aggreg_key": "$AGGREG_KEY", "transition": "$ALERT_TRANSITION",
    "title": "$EVENT_TITLE", "body": "$EVENT_MSG", "priority": "$PRIORITY", "tags": "$TAGS",
    "link": "$LINK", "date": "$DATE", "alert_type": "$ALERT_TYPE",
}


def datadog(headers, raw, source) -> list[NormalizedEvent]:
    body = _json_body(raw)
    if not isinstance(body, Mapping) or not body.get("alert_id"):
        raise AdapterRefused("Not BOSS's Datadog payload: paste the template BOSS gave you")
    transition = _s(body.get("transition")).lower()
    tags = {}
    for tag in _s(body.get("tags"), 4000).split(","):
        k, _, v = tag.strip().partition(":")
        if k and v:
            tags[k[:100]] = v[:300]
    return [NormalizedEvent(
        action="resolve" if transition in ("recovered", "resolved") else "trigger",
        external_ref=f"{_s(body.get('alert_id'), 60)}:{_s(body.get('aggreg_key'), 60)}",
        alertname="DatadogMonitor", summary=_s(body.get("title")) or "Datadog monitor",
        description=_s(body.get("body"), 2000),
        severity=severity(body.get("alert_type") or body.get("priority")),
        service=tags.get("service"), environment=tags.get("env"), labels=tags,
        links=[x for x in (_link(body.get("link"), "Datadog"),) if x],
        timestamp=_ts(body.get("date")), tool="datadog")]


# --------------------------------------------------------------------------- #
# CloudWatch via SNS (the envelope is checked and confirmed in alert_ingest)
# --------------------------------------------------------------------------- #
def sns_envelope(raw: bytes) -> dict[str, Any]:
    body = _json_body(raw)
    if not isinstance(body, Mapping) or body.get("Type") not in (
            "Notification", "SubscriptionConfirmation", "UnsubscribeConfirmation"):
        raise AdapterRefused("Not an SNS message")
    return dict(body)


def cloudwatch(headers, raw, source) -> list[NormalizedEvent]:
    env = sns_envelope(raw)
    if env["Type"] != "Notification":
        return []
    try:
        msg = json.loads(env.get("Message") or "{}")
    except ValueError:
        raise AdapterRefused("The SNS message is not a CloudWatch alarm") from None
    if not isinstance(msg, Mapping) or not msg.get("AlarmName"):
        raise AdapterRefused("The SNS message is not a CloudWatch alarm")
    state = _s(msg.get("NewStateValue")).upper()
    if state == "INSUFFICIENT_DATA":
        return []          # no data never resolves anything (spec 4.3)
    trigger = msg.get("Trigger") if isinstance(msg.get("Trigger"), Mapping) else {}
    dims = {_s(d.get("name"), 100): _s(d.get("value"), 300)
            for d in (trigger.get("Dimensions") or []) if isinstance(d, Mapping)}
    ref = _s(msg.get("AlarmArn"), 300) + "|" + ",".join(f"{k}={v}" for k, v in sorted(dims.items()))
    return [NormalizedEvent(
        action="trigger" if state == "ALARM" else "resolve", external_ref=_ref(ref),
        alertname="CloudWatchAlarm", summary=_s(msg.get("AlarmName")),
        description=_s(msg.get("NewStateReason"), 2000), severity="critical",
        service=dims.get("ServiceName") or dims.get("FunctionName") or None,
        labels={**dims, "metric": _s(trigger.get("MetricName"), 120),
                "namespace": _s(trigger.get("Namespace"), 120)},
        timestamp=_ts(msg.get("StateChangeTime")), tool="cloudwatch")]


def heartbeat(headers, raw, source) -> list[NormalizedEvent]:
    return [NormalizedEvent(action="ping", external_ref=str(source.get("id") or ""),
                            alertname="Heartbeat", tool="heartbeat")]


#: path kind -> (adapter, the signal_sources.kind it needs)
ADAPTERS: dict[str, tuple[Callable[..., list[NormalizedEvent]], tuple[str, ...]]] = {
    "alertmanager": (alertmanager, ("alertmanager",)),
    "grafana": (grafana, ("grafana",)),
    "pagerduty": (pagerduty, ("pagerduty_v2", "generic")),
    "events": (generic, ("generic", "pagerduty_v2")),
    "sentry": (sentry, ("sentry",)),
    "datadog": (datadog, ("datadog",)),
    "cloudwatch": (cloudwatch, ("cloudwatch_sns",)),
    "heartbeat": (heartbeat, ("heartbeat",)),
}


__all__ = ["ADAPTERS", "AdapterRefused", "DATADOG_TEMPLATE", "NormalizedEvent", "alertmanager",
           "cloudwatch", "datadog", "generic", "grafana", "heartbeat", "pagerduty", "sentry",
           "severity", "sns_envelope"]
