"""HOST ADDRESSES NEVER LEAVE: not in a BOSS recommendation, not in anything bound upstream.

THE FINDING (BOSSAPP-571 spec §7.3, F10). The two real `boss_recommendations` rows of run
wfr_f383649a8b024d97a80c held the deploy host's public address in their symptom and evidence:
the recovery agent's text passes `redact_for_agent`, which removes credentials and keeps
addresses. A recommendation is the seed of a self-fix, and a self-fix's evidence is what a person
may later send to the BOSS team — so an address written here is an address one click from
leaving the machine.

THE RULE, applied in one place (this module) by every writer and reader:
  * IPv4 and IPv6 addresses are masked (`[ip]`), except the unspecified and loopback addresses
    (`0.0.0.0:8000` in "Bind for 0.0.0.0:8000 failed" names no machine);
  * host names are masked (`[host]`): a dotted name that ends in a public suffix (share_scrub's
    measured precision: `stack_deploy.py` and `user.name` stay), the host of every URL, the host
    of `user@host`, and every machine this install knows by name or address (deployment hosts,
    targets' domains and public URLs) wherever it appears as a word;
  * nothing else is touched: file paths, functions, container and project names stay, because a
    diagnosis is useless without them.

Masking is for PROSE (symptoms, evidence, causes, diagnoses). Code is never masked — a masked
diff is a broken diff; the upstream path CHECKS code with `findings()` and refuses to send it.
Never raises: on any failure the text is masked as a whole rather than passed through.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from sqlalchemy import text

IP_MASK = "[ip]"
HOST_MASK = "[host]"

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\d)")
_IPV6 = re.compile(r"(?<![\w:])(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}"
                   r"|(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})+)?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?)"
                   r"(?![\w:])", re.I)
_URL = re.compile(r"\b(?P<scheme>[a-z][a-z0-9+.\-]*://)(?:[^@\s/'\"<>]*@)?"
                  r"(?P<host>\[[0-9a-f:]+\]|[^\s:/'\"<>?#\]]+)", re.I)
_USER_AT_HOST = re.compile(r"\b(?P<user>[A-Za-z_][\w.\-]{0,31})@(?P<host>[A-Za-z0-9][\w\-.]*"
                           r"[A-Za-z0-9])")
_KEEP_V4 = re.compile(r"^(?:0\.0\.0\.0|127(?:\.\d{1,3}){3})$")
_KEEP_V6 = frozenset({"::", "::1"})
#: Not machine names, though they look like words a host list could hold.
_NEVER_KNOWN = frozenset({"", "localhost", "local", "host", "docker", "boss", "default", "none"})


def _v4(match: re.Match) -> str:
    token = match.group(0)
    if _KEEP_V4.match(token):
        return token
    parts = token.split(".")
    if any(int(p) > 255 for p in parts):
        return token  # a version (1.2.3.400) or a counter, not an address
    return IP_MASK


def _v6(match: re.Match) -> str:
    token = match.group(0)
    if token.lower() in _KEEP_V6 or token.count(":") < 2:
        return token
    # `12:30:45` is a time and `a::b` a C++ scope: an address has hex groups only and either
    # eight of them or a `::`.
    if not re.fullmatch(r"[0-9a-fA-F:]+", token):
        return token
    if "::" not in token and token.count(":") != 7:
        return token
    if re.fullmatch(r"\d{1,2}(?::\d{2}){1,2}", token):
        return token
    return IP_MASK


def _url(match: re.Match) -> str:
    host = match.group("host")
    if host.lower() in ("localhost",) or _KEEP_V4.match(host) or host.strip("[]") in _KEEP_V6:
        return match.group(0)
    return f"{match.group('scheme')}{HOST_MASK}"


def _user_at(match: re.Match) -> str:
    host = match.group("host")
    if "." not in host and not _looks_like_machine(host):
        return match.group(0)  # `@decorator`-ish, a package scope, an npm `@x`
    return f"{match.group('user')}@{HOST_MASK}"


def _looks_like_machine(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][\w\-]*\d[\w\-]*|[a-z]+-[a-z0-9\-]+", name or ""))


def _dotted_hosts(work: str, known: set[str]) -> str:
    from .share_scrub import _HOST_CANDIDATE, _is_host

    return _HOST_CANDIDATE.sub(
        lambda m: HOST_MASK if _is_host(m.group(0), known,
                                        m.string[max(0, m.start() - 40):m.start()],
                                        m.string[m.end():m.end() + 1])
        else m.group(0), work)


def _known_words(work: str, known: Iterable[str]) -> str:
    for name in sorted({k for k in known if k and k.lower() not in _NEVER_KNOWN},
                       key=len, reverse=True):
        mask = IP_MASK if _IPV4.fullmatch(name) or ":" in name else HOST_MASK
        work = re.sub(rf"(?<![\w.\-]){re.escape(name)}(?![\w\-]|\.\w)", mask, work, flags=re.I)
    return work


def mask_hosts(value: Any, known: Iterable[str] = ()) -> str:
    """`value` as text with every host address masked (the module docstring's rule)."""
    raw = "" if value is None else str(value)
    if not raw:
        return raw
    try:
        known_set = {str(k).strip().lower() for k in known if str(k or "").strip()}
        work = _URL.sub(_url, raw)
        work = _USER_AT_HOST.sub(_user_at, work)
        work = _known_words(work, known_set)
        work = _IPV6.sub(_v6, work)
        work = _IPV4.sub(_v4, work)
        work = _dotted_hosts(work, known_set)
        return work
    except Exception:  # noqa: BLE001 — never pass an address through on a failure
        return HOST_MASK


def mask_value(value: Any, known: Iterable[str] = (), _depth: int = 0) -> Any:
    """`mask_hosts` over every string of a JSON-shaped value (keys kept)."""
    known = list(known)
    if _depth > 12:
        return HOST_MASK
    if isinstance(value, str):
        return mask_hosts(value, known)
    if isinstance(value, dict):
        return {k: mask_value(v, known, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_value(v, known, _depth + 1) for v in value]
    return value


def findings(value: Any, known: Iterable[str] = ()) -> list[str]:
    """What `mask_hosts` would mask, for a CHECK that must not change the text (code, a diff):
    the distinct masked tokens' kinds and a short context, never the address itself."""
    raw = "" if value is None else str(value)
    masked = mask_hosts(raw, known)
    if masked == raw:
        return []
    out = []
    for line_no, (before, after) in enumerate(zip(raw.splitlines(), masked.splitlines()), 1):
        if before != after:
            kinds = sorted({k for k in (IP_MASK, HOST_MASK) if k in after and k not in before})
            out.append(f"line {line_no}: {', '.join(kinds) or 'address'}")
    return out or ["an address"]


def known_hosts(connection: Optional[Any], project_id: Optional[str] = None) -> list[str]:
    """Every machine this install knows by a name or an address: its deployment hosts, its
    targets' domains and public URLs. Read-only; a partial schema yields what it has."""
    if connection is None:
        return []
    out: set[str] = set()

    def rows(sql: str) -> list:
        try:
            with connection.begin_nested():
                return list(connection.execute(text(sql)).all())
        except Exception:  # noqa: BLE001 — a missing table hides nothing; the patterns still run
            return []

    for name, address in rows("SELECT name, address FROM deployment_hosts"):
        for value in (name, address):
            value = str(value or "").strip()
            if value:
                out.add(value.split("@")[-1])
    for domains, public_url in rows("SELECT domains, public_url FROM deployment_targets"):
        for d in domains if isinstance(domains, list) else []:
            if isinstance(d, str) and d.strip():
                out.add(d.strip())
        if public_url:
            host = re.sub(r"^[a-z]+://", "", str(public_url)).split("/")[0].split(":")[0]
            if host:
                out.add(host)
    return sorted(out)


__all__ = ["HOST_MASK", "IP_MASK", "findings", "known_hosts", "mask_hosts", "mask_value"]
