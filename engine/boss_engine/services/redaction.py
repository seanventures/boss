"""PII sanitizer — redacts sensitive values before content crosses a data boundary to an external
model or tool (spec §7.4, §25). Pattern-based and deterministic so it's safe to run in the hot
path; returns the redacted text plus a summary of what was masked (for the audit trail).

Apply this only to data-boundary-sensitive content (e.g. customer data on a cloud task) — NOT to
code the factory is writing, where masking would corrupt the change. Callers opt in explicitly.
"""
from __future__ import annotations

import re

# CREDENTIALS, one class: every token shape a provider issues, and every place a secret is
# written as text (an assignment, a header, a PEM block, a JWT). Most specific first so a
# token is never half-masked. These run before the PII patterns and are also usable alone
# (``redact_secrets``) where PII masking would mangle the text (ids, timestamps, titles).
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
        re.DOTALL)),
    ("SECRET", re.compile(r"\bsk-(?:ant|proj|svcacct|admin)-[A-Za-z0-9_\-]{8,}")),
    ("SECRET", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test)?[-_]?[A-Za-z0-9]{16,}[A-Za-z0-9_\-]*")),
    ("SECRET", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{8,}")),
    ("SECRET", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("SECRET", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("SECRET", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}")),
    ("SECRET", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("BEARER", re.compile(r"\b(?:Bearer|Basic|token)\s+[A-Za-z0-9._~+/\-]{16,}=*", re.IGNORECASE)),
    # NAME_KEY=value, "password": "value", export API_TOKEN=value. The name is kept (it says
    # what was there); the value goes. A name must END in the secret word, so
    # `max_total_tokens_per_call=64000` and `key=code_agent_max_turns` are left alone.
    ("SECRET", re.compile(
        r"""(?ix)
        (?P<name>(?<![a-z0-9_.\-])[a-z0-9_.\-]{0,64}?
            (?:api[_\-]?key|access[_\-]?key|secret[_\-]?key|private[_\-]?key|[_\-]key
              |token|secret|password|passwd|pwd|credentials?|client[_\-]?secret))
        (?P<sep>["']?\s*[:=]\s*["']?)
        (?P<value>(?!\[)[^\s"'&,;)}\]]{4,})""")),
]

# (label, compiled pattern). Order matters: most-specific first so a token isn't half-masked.
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("PHONE", re.compile(r"\b\+?\d[\d().\-\s]{8,}\d\b")),
]
_PATTERNS = _SECRET_PATTERNS + _PII_PATTERNS


def _apply(text: str, patterns: list[tuple[str, re.Pattern]]) -> tuple[str, dict[str, int]]:
    if not text:
        return text, {}
    summary: dict[str, int] = {}
    out = text
    for label, pattern in patterns:
        count = 0

        def _sub(m, _label=label):
            nonlocal count
            count += 1
            if "value" in m.re.groupindex:  # an assignment keeps its name
                return f"{m.group('name')}{m.group('sep')}[{_label}]"
            return f"[{_label}]"

        out = pattern.sub(_sub, out)
        if count:
            summary[label] = summary.get(label, 0) + count
    return out, summary


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return (redacted_text, {label: count}). Each match becomes ``[LABEL]``: credentials
    first, then PII."""
    return _apply(text, _PATTERNS)


def redact_secrets(text: str) -> str:
    """Credentials only — for text PII masking would mangle (ids, times, titles, a CSV line)."""
    return _apply(text, _SECRET_PATTERNS)[0] if text else text


def has_pii(text: str) -> bool:
    return bool(redact(text)[1])
