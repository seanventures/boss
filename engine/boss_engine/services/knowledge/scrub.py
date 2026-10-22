"""Scrub secrets and configured PII before anything is stored or embedded.

Scrubbed spans are counted. The original span is not logged.
"""
from __future__ import annotations

import re

from ..knowledge_context import _redact_secrets

# Most specific first: a card number is not a phone number. A card is scrubbed only when it
# passes the Luhn check, so an order or tracking number of the same length survives.
_PII: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CARD", re.compile(r"\b\d(?:[ \-]?\d){12,18}\b")),
    ("NATIONAL_ID", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("IP", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("PHONE", re.compile(r"\b(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}\b")),
)


def _luhn(digits: str) -> bool:
    total, alternate = 0, False
    for char in reversed(digits):
        value = int(char)
        if alternate:
            value = value * 2 - 9 if value > 4 else value * 2
        total, alternate = total + value, not alternate
    return total % 10 == 0


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """Return scrubbed text and a count of spans removed."""
    if not text:
        return text or "", {}
    cleaned, summary = _redact_secrets(text)
    counts = dict(summary)
    for label, pattern in _PII:
        found = 0

        def _sub(match: re.Match[str], _label: str = label) -> str:
            nonlocal found
            if _label == "CARD":
                digits = re.sub(r"\D", "", match.group(0))
                if not (13 <= len(digits) <= 19 and _luhn(digits)):
                    return match.group(0)
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
