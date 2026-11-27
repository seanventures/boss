"""Shared mail parsing, RFC 8601 DMARC verification, and RFC 3834 loop-guard helpers (P9c-1).

Used by both Gmail and Outlook intake adapters and by `services/intake_identity.py`.
`From` alone is spoofable and NEVER resolves to a BOSS person: only an email whose
`Authentication-Results` proves `dmarc=pass` aligned with the `From` domain (and whose domain is
an active verified domain of the org) can map to a BOSS principal (EVENTING_LLD.md §6.2).
"""
from __future__ import annotations

import email.utils
import re
from typing import Any, Iterable, Mapping, Optional

_DMARC_RESULT = re.compile(r"\bdmarc\s*=\s*([a-z0-9_-]+)", re.IGNORECASE)
_HEADER_FROM = re.compile(r"\bheader\.from\s*=\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_SPF_RESULT = re.compile(r"\bspf\s*=\s*([a-z0-9_-]+)", re.IGNORECASE)
_DKIM_RESULT = re.compile(r"\bdkim\s*=\s*([a-z0-9_-]+)", re.IGNORECASE)


def parse_from(raw_from: str) -> tuple[str, str]:
    """Return `(display_name, email_lower)` from an RFC 5322 From header."""
    display, addr = email.utils.parseaddr(str(raw_from or ""))
    addr_clean = addr.strip().lower()
    if "@" not in addr_clean:
        addr_clean = ""
    return display.strip(), addr_clean


def email_domain(addr: str) -> str:
    addr = str(addr or "").strip().lower()
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip().rstrip(".")


def normalize_headers(raw_headers: Any) -> dict[str, list[str]]:
    """Normalise Gmail `[{name, value}]`, Graph `[{name, value}]`, or dict headers to
    `{lower_name: [values...]}` preserving multi-valued headers like `Authentication-Results`."""
    out: dict[str, list[str]] = {}
    if isinstance(raw_headers, Mapping):
        for k, v in raw_headers.items():
            key = str(k).strip().lower()
            if not key:
                continue
            if isinstance(v, list):
                out.setdefault(key, []).extend(str(item) for item in v if item is not None)
            elif v is not None:
                out.setdefault(key, []).append(str(v))
    elif isinstance(raw_headers, Iterable) and not isinstance(raw_headers, (str, bytes)):
        for item in raw_headers:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("name") or item.get("key") or "").strip().lower()
            val = item.get("value")
            if key and val is not None:
                out.setdefault(key, []).append(str(val))
    return out


def first_header(headers: Mapping[str, list[str]], name: str) -> str:
    vals = headers.get(name.lower()) or []
    return str(vals[0]).strip() if vals else ""


def parse_dmarc(auth_results_values: Iterable[str], from_email: str) -> dict[str, Any]:
    """Evaluate DMARC status from `Authentication-Results` header values against `from_email`.

    Fail-closed: returns `passed=True` ONLY when an `Authentication-Results` header explicitly
    reports `dmarc=pass` AND (when `header.from` is stated) its domain matches `from_email`'s
    domain, AND no later/conflicting header reports `dmarc=fail` for the same From domain.
    """
    from_dom = email_domain(from_email)
    verdict = "none"
    header_from: Optional[str] = None
    spf = "none"
    dkim = "none"
    saw_pass = False
    saw_fail = False

    for raw in auth_results_values or ():
        line = str(raw or "")
        if not line.strip():
            continue
        m_spf = _SPF_RESULT.search(line)
        if m_spf and spf == "none":
            spf = m_spf.group(1).lower()
        m_dkim = _DKIM_RESULT.search(line)
        if m_dkim and dkim == "none":
            dkim = m_dkim.group(1).lower()
        m_dmarc = _DMARC_RESULT.search(line)
        if not m_dmarc:
            continue
        res = m_dmarc.group(1).lower()
        m_hf = _HEADER_FROM.search(line)
        hf = m_hf.group(1).strip().lower().rstrip(".") if m_hf else None
        if res == "pass":
            if from_dom and (hf is None or hf == from_dom):
                saw_pass = True
                header_from = hf or from_dom
                verdict = "pass"
            else:
                saw_fail = True
                header_from = hf
                verdict = "fail_alignment"
        elif res in ("fail", "permerror", "temperror", "softfail"):
            saw_fail = True
            header_from = hf or header_from
            verdict = res
        elif verdict == "none":
            verdict = res
            header_from = hf or header_from

    passed = bool(from_dom and saw_pass and not saw_fail)
    if saw_fail and verdict == "pass":
        verdict = "fail"
    return {
        "verdict": verdict,
        "passed": passed,
        "from_domain": from_dom,
        "header_from": header_from,
        "spf": spf,
        "dkim": dkim,
    }


def is_auto_submitted(headers: Mapping[str, list[str]]) -> bool:
    """RFC 3834 loop guard: any `Auto-Submitted` value other than `no` is auto-generated."""
    val = first_header(headers, "auto-submitted").lower()
    if val and val != "no":
        return True
    precedence = first_header(headers, "precedence").lower()
    if precedence in ("bulk", "junk", "auto_reply"):
        return True
    if first_header(headers, "x-autoreply") or first_header(headers, "x-autorespond"):
        return True
    return False


def extract_boss_causality(headers: Mapping[str, list[str]]) -> Optional[dict[str, Any]]:
    """Read BOSS causality headers (`X-BOSS-Run`, `X-BOSS-Deployment`, `X-BOSS-Hops`)."""
    run_id = first_header(headers, "x-boss-run")
    dep_id = first_header(headers, "x-boss-deployment")
    raw_hops = first_header(headers, "x-boss-hops")
    if not (run_id or dep_id or raw_hops):
        return None
    try:
        hops = int(raw_hops) if raw_hops else 1
    except ValueError:
        hops = 1
    return {"run_id": run_id or None, "deployment_id": dep_id or None, "hops": hops}


__all__ = [
    "email_domain",
    "extract_boss_causality",
    "first_header",
    "is_auto_submitted",
    "normalize_headers",
    "parse_dmarc",
    "parse_from",
]
