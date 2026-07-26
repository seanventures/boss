"""PII sanitizer — redacts sensitive values before content crosses a data boundary to an external
model or tool (spec §7.4, §25). Pattern-based and deterministic so it's safe to run in the hot
path; returns the redacted text plus a summary of what was masked (for the audit trail).

Apply this only to data-boundary-sensitive content (e.g. customer data on a cloud task) — NOT to
code the factory is writing, where masking would corrupt the change. Callers opt in explicitly.
"""
from __future__ import annotations

import re

# (label, compiled pattern). Order matters: most-specific first so a token isn't half-masked.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("SECRET", re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{16,}\b")),
    ("BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE)),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL)),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("PHONE", re.compile(r"\b\+?\d[\d().\-\s]{8,}\d\b")),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return (redacted_text, {label: count}). Each match becomes ``[LABEL]``."""
    if not text:
        return text, {}
    summary: dict[str, int] = {}
    out = text
    for label, pattern in _PATTERNS:
        count = 0

        def _sub(_m, _label=label):
            nonlocal count
            count += 1
            return f"[{_label}]"

        out = pattern.sub(_sub, out)
        if count:
            summary[label] = count
    return out, summary


def has_pii(text: str) -> bool:
    return bool(redact(text)[1])
