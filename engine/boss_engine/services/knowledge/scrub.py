"""Scrubbing before anything reaches the Archivist's model or the knowledge store (spec §3.4, §3.8).

Two passes over every source text and again over every fact the model returns (a model can echo
what it was shown):

1. **Secrets**, through `knowledge_context._redact_secrets`, the same credential patterns the
   context packer uses (extended for Phase 2).
2. **PII**, by a detector configured per org (`kg_knowledge_settings.pii`): email, phone, IP
   address, payment card (Luhn-checked), national id, plus custom `{label, pattern}` entries.

Only the counts of scrubbed spans are kept (`kg_ingest_log.scrubbed`); the spans are never logged.
"""
from __future__ import annotations

import re
from typing import Any

from ..knowledge_context import _redact_secrets

DEFAULT_PII: dict[str, Any] = {"email": True, "phone": True, "ip": True, "card": True, "national_id": True,
                               "custom": []}

_PII: dict[str, re.Pattern[str]] = {        # most specific first: a card is not a phone number
    "card": re.compile(r"\b\d(?:[ \-]?\d){12,18}\b"),
    "national_id": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    "ip": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
                     r"|\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"),
    "phone": re.compile(r"(?<![\w.])\+?\d{1,3}[ .\-]?\(?\d{2,4}\)?[ .\-]\d{3,4}[ .\-]\d{3,4}(?![\w.])"),
}


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def scrub(text: str, pii: dict[str, Any] | None = None) -> tuple[str, dict[str, int]]:
    """`text` with secrets and configured PII replaced by `[LABEL]`, and the count per label."""
    if not text:
        return text or "", {}
    out, counts = _redact_secrets(text)
    counts = dict(counts)
    config = {**DEFAULT_PII, **(pii or {})}
    patterns = [(label.upper(), pat) for label, pat in _PII.items() if config.get(label)]
    for entry in config.get("custom") or []:
        try:
            patterns.append((str(entry["label"]).upper(), re.compile(entry["pattern"])))
        except (KeyError, TypeError, re.error):
            continue                                   # a bad custom rule never blocks ingestion
    for label, pattern in patterns:
        n = 0

        def sub(m: re.Match[str], _label: str = label) -> str:
            nonlocal n
            if _label == "CARD":
                digits = re.sub(r"\D", "", m.group(0))
                if not (13 <= len(digits) <= 19 and _luhn(digits)):
                    return m.group(0)
            n += 1
            return f"[{_label}]"

        out = pattern.sub(sub, out)
        if n:
            counts[label] = counts.get(label, 0) + n
    return out, counts


def merge_counts(*parts: dict[str, int]) -> dict[str, int]:
    total: dict[str, int] = {}
    for part in parts:
        for k, v in part.items():
            total[k] = total.get(k, 0) + v
    return total
