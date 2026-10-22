"""Scrub secrets and configured PII before anything is stored or embedded.

Scrubbed spans are counted. The original span is not logged.
"""
from __future__ import annotations

import re

from ..knowledge_context import _redact_secrets

_PII: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"\b(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
)


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """Return scrubbed text and a count of spans removed."""
    if not text:
        return text or "", {}
    cleaned, summary = _redact_secrets(text)
    counts = dict(summary)
    for label, pattern in _PII:
        found = 0

        def _sub(_match: re.Match[str], _label: str = label) -> str:
            nonlocal found
            found += 1
            return f"[{_label}]"

        cleaned = pattern.sub(_sub, cleaned)
        if found:
            counts[label] = found
    return cleaned, counts


def contains_secret_marker(text: str) -> bool:
    """True when a known secret shape is still present."""
    _, counts = _redact_secrets(text or "")
    return bool(counts)
