"""EVIDENCE IS REDACTED BEFORE IT IS STORED OR SHOWN (spec §4.7 S4.3, S4.4).

THE redaction filter (identity/redaction.py) first: credential-named keys, BOSS credential shapes,
JWTs, Bearer/Basic values, Cookie/Authorization lines, credential-named query parameters, and every
secret the install is configured with. Then what only a scanner's output carries:

* a hardcoded-secret finding's snippet shows the secret: every string literal in it is masked
  (the rule and the line are the finding; the value is not needed and must not travel);
* well-known token shapes a scan of someone's code meets (cloud and SaaS keys, private keys);
* a ZAP request/response excerpt's Set-Cookie / Cookie values.

Then the whole evidence object is held to 4 KB, cutting the longest strings first.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..identity import redaction as _identity
from .findings import MAX_EVIDENCE_BYTES

REDACTED = _identity.REDACTED

_TOKEN_SHAPES = re.compile(
    r"(?:AKIA|ASIA)[0-9A-Z]{16}"                        # AWS access key id
    r"|gh[pousr]_[A-Za-z0-9]{30,}"                      # GitHub tokens
    r"|github_pat_[A-Za-z0-9_]{30,}"
    r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"     # Stripe
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"                   # Slack
    r"|AIza[0-9A-Za-z_-]{35}"                           # Google API key
    r"|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"            # model provider keys
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")
_COOKIE_VALUES = re.compile(r"(?im)^((?:set-)?cookie\s*:\s*)(.*)$")
_COOKIE_PAIR = re.compile(r"([A-Za-z0-9_.-]+)=([^;\s]+)")
_STRING_LITERAL = re.compile(r"(\"|'|`)((?:\\.|(?!\1).){1,})\1")

#: Rules whose finding IS a secret in the snippet: CWE-798/259/321 or a secrets pack rule.
_SECRET_CWES = ("CWE-798", "CWE-259", "CWE-321", "CWE-312", "CWE-522")


def is_secret_rule(rule_id: str, cwe: str | None) -> bool:
    rid = (rule_id or "").lower()
    if cwe and any(c in cwe for c in _SECRET_CWES):
        return True
    return rid.startswith("generic.secrets.") or "hardcode" in rid or ".secrets." in rid


def mask_literals(snippet: str) -> str:
    return _STRING_LITERAL.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(1)}", snippet)


def redact_text(text: str) -> str:
    out = _identity.redact_text(str(text))
    out = _TOKEN_SHAPES.sub(REDACTED, out)

    def _cookies(m: re.Match) -> str:
        return m.group(1) + _COOKIE_PAIR.sub(lambda p: f"{p.group(1)}={REDACTED}", m.group(2))

    return _COOKIE_VALUES.sub(_cookies, out)


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_walk(v) for v in value]
    return value


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def cap(evidence: dict[str, Any], limit: int = MAX_EVIDENCE_BYTES) -> dict[str, Any]:
    """Hold the evidence to `limit` bytes of JSON: halve the longest string until it fits, then
    drop keys from the end. Marks `truncated: true` when it cut anything."""
    ev = dict(evidence)
    if _size(ev) <= limit:
        return ev
    ev["truncated"] = True
    for _ in range(64):
        if _size(ev) <= limit:
            return ev
        strings = [(len(v), k) for k, v in ev.items() if isinstance(v, str)]
        if not strings or max(strings)[0] <= 32:
            break
        _n, key = max(strings)
        ev[key] = ev[key][: len(ev[key]) // 2] + "…"
    for key in [k for k in list(ev) if k != "truncated"][::-1]:
        if _size(ev) <= limit:
            break
        ev.pop(key)
    return ev


def redact_evidence(evidence: dict[str, Any], *, rule_id: str = "", cwe: str | None = None) -> dict[str, Any]:
    ev = _identity.redact(dict(evidence or {}))         # credential-NAMED keys first
    ev = _walk(ev)
    if is_secret_rule(rule_id, cwe) and isinstance(ev.get("snippet"), str):
        ev["snippet"] = mask_literals(ev["snippet"])
    return cap(ev)
