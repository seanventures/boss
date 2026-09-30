"""Deterministic activity-row classifier (BOSS-379).

Pure functions only: no database, no network, no spend. Status comes from
event/action/status names — never invent a success.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from .redaction import redact

ActivityStatus = str  # ok | failed | waiting | stuck | info
ActivitySeverity = str  # info | warn | error

_FAILED_MARKERS = ("fail", "error", "deadletter", "rejected", "blocked")
_WAITING_MARKERS = ("waiting", "queued", "lease")
_STUCK_MARKERS = ("stuck", "stalled", "timeout", "expired")
_OK_MARKERS = ("succeeded", "completed", "certified", "accepted")
_OK_PHRASES = ("handoff succeeded",)
_CLOSED_MARKERS = (
    "succeeded",
    "completed",
    "certified",
    "accepted",
    "cancelled",
    "closed",
    "resolved",
    "done",
)
_DROP_KEYS = frozenset({"token", "secret", "password", "api_key", "authorization"})
_SPLIT = re.compile(r"[._\-]+")


def classify_activity(row: Optional[Mapping[str, Any]] = None) -> dict[str, str]:
    """Return status/stage/severity/title from a source row.

    Lookup order is names only: failed → stuck (if still open) → waiting →
    explicit success → info. ``ok`` is never assigned unless a name/status
    actually says success.
    """
    source = dict(row or {})
    event_type = _text(source.get("event_type"))
    action = _text(source.get("action"))
    status_value = _text(source.get("status"))
    kind = _text(source.get("kind"))
    haystack = " ".join(part for part in (event_type, action, status_value, kind) if part)

    if _contains_any(haystack, _FAILED_MARKERS):
        status: ActivityStatus = "failed"
        severity: ActivitySeverity = "error"
    elif _contains_any(haystack, _STUCK_MARKERS) and _still_open(status_value):
        status = "stuck"
        severity = "warn"
    elif _contains_any(haystack, _WAITING_MARKERS):
        status = "waiting"
        severity = "info"
    elif _contains_any(haystack, _OK_MARKERS) or _contains_any(haystack, _OK_PHRASES):
        status = "ok"
        severity = "info"
    else:
        status = "info"
        severity = "info"

    return {
        "status": status,
        "stage": _text(source.get("stage") or source.get("step_key")),
        "severity": severity,
        "title": _humanize(event_type or action or kind or status_value),
    }


def redact_activity_data(data: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Drop secret-named keys and run ``redaction.redact`` on string values."""
    if not data or not isinstance(data, Mapping):
        return {}
    return _redact_mapping(data)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _contains_any(haystack: str, markers: tuple[str, ...]) -> bool:
    lowered = haystack.lower()
    return any(marker in lowered for marker in markers)


def _still_open(status_value: str) -> bool:
    if not status_value:
        return True
    lowered = status_value.lower()
    return not any(marker in lowered for marker in _CLOSED_MARKERS)


def _humanize(value: str) -> str:
    words = _SPLIT.sub(" ", value).split()
    return " ".join(word.capitalize() for word in words)


def _redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if str(key).lower() in _DROP_KEYS:
            continue
        out[key] = _redact_value(value)
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value
