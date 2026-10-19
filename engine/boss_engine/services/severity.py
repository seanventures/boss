"""Severity levels SEV1-SEV4: one vocabulary for rules, alerts, incidents, routing and messages.

A rule's `labels.severity` may say `sev1`..`sev4` (what the console writes) or the older
Prometheus words `critical`, `warning` and `info` (built-ins, yaml rules, outside tools). Both
are read through `level`, so the two vocabularies can never disagree:

    critical -> SEV1    warning -> SEV3    info -> SEV4    anything else -> SEV3

Routing matches on the normalised `sev` label (`sev="sev1"`), which `monitoring.alert_labels`
adds to every alert, and urgency follows the level: SEV1 and SEV2 notify at once.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Union

LEVELS = ("sev1", "sev2", "sev3", "sev4")

CATALOG: dict[str, dict[str, str]] = {
    "sev1": {"label": "SEV1", "word": "Critical", "meaning": "Customer-facing outage",
             "routing": "Pages: high urgency, notifies at once, ignores quiet hours",
             "urgency": "high", "legacy": "critical"},
    "sev2": {"label": "SEV2", "word": "Major", "meaning": "Major degradation",
             "routing": "High urgency: notifies at once", "urgency": "high",
             "legacy": "critical"},
    "sev3": {"label": "SEV3", "word": "Minor", "meaning": "Minor or partial problem",
             "routing": "Low urgency: waits out quiet hours", "urgency": "low",
             "legacy": "warning"},
    "sev4": {"label": "SEV4", "word": "Info", "meaning": "Informational",
             "routing": "Low urgency: console and the daily digest", "urgency": "low",
             "legacy": "info"},
}
_ALIASES = {"critical": "sev1", "crit": "sev1", "page": "sev1", "p1": "sev1", "high": "sev2",
            "p2": "sev2", "major": "sev2", "error": "sev2", "warning": "sev3", "warn": "sev3",
            "minor": "sev3", "p3": "sev3", "medium": "sev3", "info": "sev4", "low": "sev4",
            "p4": "sev4", "p5": "sev4", "informational": "sev4", "none": "sev4"}
#: What a person may write as a rule's severity (validated on save).
ACCEPTED = LEVELS + ("critical", "warning", "info")


def level(value: Union[None, str, Mapping[str, Any]]) -> str:
    """'sev1'..'sev4' from a severity word or a label set (its `severity`)."""
    if isinstance(value, Mapping):
        value = value.get("severity")
    word = str(value or "").strip().lower().replace(" ", "")
    if word in LEVELS:
        return word
    return _ALIASES.get(word, "sev3")


def accepted(value: Any) -> bool:
    return str(value or "").strip().lower() in ACCEPTED


def label(value: Any) -> str:
    return CATALOG[level(value)]["label"]


def urgency(value: Any) -> str:
    return CATALOG[level(value)]["urgency"]


def paging(value: Any) -> bool:
    """SEV1 and SEV2: what the older code called `critical`."""
    return urgency(value) == "high"


def legacy(value: Any) -> str:
    """critical | warning | info, for the tables whose CHECK still speaks the older words."""
    return CATALOG[level(value)]["legacy"]


def worst(values: Any) -> Optional[str]:
    got = [level(v) for v in values or [] if v is not None]
    return min(got) if got else None


def catalog() -> list[dict[str, str]]:
    return [{"key": k, "label": v["label"], "word": v["word"], "meaning": v["meaning"],
             "routing": v["routing"]} for k, v in CATALOG.items()]


def tip(value: Any) -> str:
    c = CATALOG[level(value)]
    return f"{c['label']} · {c['word']}: {c['meaning']}. {c['routing']}"


__all__ = ["ACCEPTED", "CATALOG", "LEVELS", "accepted", "catalog", "label", "legacy", "level",
           "paging", "tip", "urgency", "worst"]
