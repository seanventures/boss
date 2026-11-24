"""A DOWNLOAD THE NETWORK REFUSED SAYS WHICH SETTING FIXES IT (BOSSAPP-1435, BOSSAPP-1436).

On a company network three things refuse a build's download, and each is fixed by ONE setting in
the BOSS host's .env (docs/community/ENTERPRISE_NETWORK.md), never by changing the app's code:

  company_ca     a TLS-inspecting gateway re-signs the registry's certificate and BOSS has no
                 company CA for it (or the one it has does not cover it): BOSS_EXTRA_CA_FILE;
  egress_fence   BOSS's own sandbox fence (boss-egress) refused the host: BOSS_EGRESS_CONNECT_ALLOW
                 (or BOSS_EGRESS_PRIVATE_ALLOW for an internal registry on a private address);
  company_proxy  the company proxy BOSS goes out through (BOSS_UPSTREAM_PROXY) refused the host:
                 the company's IT allows it, or BOSS_UPSTREAM_NO_PROXY when it is internal.

Builds classified these (suite_environment.classify_install) as "the registry's certificate is
not trusted" or "refused access", and the card said neither the setting nor the host, so a person
could not act on it. `explain` reads the failure's own words and returns what happened, who acts
and what they do, naming the host and the setting — never a setting's value. Every surface that
shows such a failure (the build's blocked reason, the failure-owner card) uses it.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

#: The fence's 403 reason phrase (apps/egress/boss_egress/connect.py, refusal_reason).
_FENCE = re.compile(r"Blocked by BOSS network fence: (?P<rest>[^\n\"'\\]{3,400})")
#: The fence's 502 when the company proxy refused the chained CONNECT (connect.py, handle).
_UPSTREAM = re.compile(r"\(BOSS_UPSTREAM_PROXY\) refused (?P<host>[A-Za-z0-9.*-]+):(?P<port>\d+)"
                       r"(?:\s*[:(]\s*(?P<status>[^\n\"')]{0,80}))?")
#: A certificate no trusted root vouches for: the signature of a TLS-inspecting gateway BOSS has
#: no CA for. (An EXPIRED certificate or a host-name mismatch is not this, and is not fixed by a
#: CA, so neither is here.)
_UNKNOWN_ISSUER = re.compile(
    r"SELF_SIGNED_CERT_IN_CHAIN|DEPTH_ZERO_SELF_SIGNED_CERT|UNABLE_TO_GET_ISSUER_CERT(?:_LOCALLY)?"
    r"|UNABLE_TO_VERIFY_LEAF_SIGNATURE|self[- ]signed certificate(?: in certificate chain)?"
    r"|unable to get local issuer certificate|certificate signed by unknown authority"
    r"|PKIX path building failed|unable to find valid certification path"
    r"|CERTIFICATE_VERIFY_FAILED|SSLCertVerificationError|certificate verify failed", re.I)
_NOT_A_CA_PROBLEM = re.compile(r"certificate has expired|CERT_HAS_EXPIRED|hostname mismatch"
                               r"|ERR_TLS_CERT_ALTNAME_INVALID|doesn't match|not valid for", re.I)
_URL_HOST = re.compile(r"https?://(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}|[0-9.]{7,15})"
                       r"(?::(?P<port>\d+))?")
_PIP_HOST = re.compile(r"host='(?P<host>[^']+)'")
_HOST_PORT = re.compile(r"\b(?P<host>[a-z0-9][a-z0-9.-]*\.[a-z]{2,}):(?P<port>443|\d{4,5})\b", re.I)
_SAFE = re.compile(r"[^A-Za-z0-9.*:-]")

#: Where the containers see the company CA (enterprise_network.CA_CONTAINER_PATH).
CA_PATH = "/etc/boss/extra-ca.pem"
APPLY = "runs `boss upgrade` on the BOSS host"
CODES = ("company_ca", "egress_fence", "company_proxy")


def ca_configured(path: str = CA_PATH) -> Optional[bool]:
    """Does this install trust a company CA? None when this process cannot tell (not a BOSS
    container: the file is not mounted)."""
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return None


def _line(text: str, at: int) -> str:
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    return text[start:end if end >= 0 else len(text)]


def _host(text: str, near: str) -> str:
    for source in (near, text):
        for pattern in (_URL_HOST, _PIP_HOST, _HOST_PORT):
            m = pattern.search(source)
            if m:
                host = _SAFE.sub("", m.group("host").lower())[:120]
                port = m.groupdict().get("port")
                return host if not port or port == "443" else f"{host}:{port}"
    return ""


def _fence(rest: str) -> Optional[dict[str, str]]:
    rest = rest.strip()
    m = re.match(r"(?:port (?P<port>\d+) is not open for )?(?P<host>[a-z0-9.*-]+)", rest, re.I)
    host = _SAFE.sub("", m.group("host").lower()).rstrip(".") if m else ""
    where = f"{host}:{m.group('port')}" if m and m.group("port") else host
    if "GitHub proxy" in rest:
        return None                          # GitHub goes through BOSS's own proxy: not a setting
    if "private address" in rest:
        return {"kind": "egress_fence", "host": where, "setting": "BOSS_EGRESS_PRIVATE_ALLOW",
                "what": f"BOSS's network fence refused {where}: it resolves to a private address.",
                "action": (f"If {host} is your internal registry, an operator adds {where} to "
                           f"BOSS_EGRESS_PRIVATE_ALLOW in .env and {APPLY}.")}
    if "does not resolve" in rest:
        return {"kind": "egress_fence", "host": where, "setting": "BOSS_UPSTREAM_PROXY",
                "what": f"BOSS's network fence could not resolve {where}.",
                "action": ("An operator checks the BOSS host's DNS, or sets BOSS_UPSTREAM_PROXY "
                           f"in .env when this network reaches out only through a proxy, and {APPLY}.")}
    return {"kind": "egress_fence", "host": where, "setting": "BOSS_EGRESS_CONNECT_ALLOW",
            "what": f"BOSS's network fence refused {where}: it is not on the allowlist.",
            "action": (f"An operator adds {where} to BOSS_EGRESS_CONNECT_ALLOW in .env and "
                       f"{APPLY}.")}


def explain(error: Any, *, ca: Optional[bool] = None) -> Optional[dict[str, str]]:
    """{kind, host, setting, what, action} when `error` is a download the network refused for
    one of CODES, else None. `ca`: whether a company CA is configured (default: read the mount)."""
    text = str(error or "")
    if not text:
        return None
    fence = _FENCE.search(text)
    if fence:
        # The phrase itself may hold an apostrophe ("BOSS's GitHub proxy"): read it whole.
        return _fence(text[fence.start("rest"):fence.start("rest") + 300].split("\n", 1)[0])
    up = _UPSTREAM.search(text)
    if up:
        status = " ".join((up.group("status") or "").split())[:60]
        if re.search(r"\b407\b", status):
            return None                      # the proxy wants a login: registry_access says so
        host = _SAFE.sub("", up.group("host").lower())
        where = host if up.group("port") == "443" else f"{host}:{up.group('port')}"
        said = f" ({status})" if status else ""
        return {"kind": "company_proxy", "host": where, "setting": "BOSS_UPSTREAM_NO_PROXY",
                "what": f"Your company proxy refused {where}{said}.",
                "action": (f"Ask IT to allow {where} on the company proxy. If {host} is inside "
                           f"your network, an operator adds it to BOSS_UPSTREAM_NO_PROXY in .env "
                           f"and {APPLY}.")}
    tls = _UNKNOWN_ISSUER.search(text)
    if tls:
        near = _line(text, tls.start())
        if _NOT_A_CA_PROBLEM.search(near):
            return None
        host = _host(text, near)
        who = host or "A download"
        have = ca_configured() if ca is None else ca
        if have:
            return {"kind": "company_ca", "host": host, "setting": "BOSS_EXTRA_CA_FILE",
                    "what": (f"{who} presented a certificate that the company CA BOSS has "
                             "(BOSS_EXTRA_CA_FILE) does not cover."),
                    "action": ("An operator replaces that file with the gateway's ROOT CA (PEM, "
                               f"not an intermediate) and {APPLY}.")}
        return {"kind": "company_ca", "host": host, "setting": "BOSS_EXTRA_CA_FILE",
                "what": (f"{who} presented a certificate no trusted CA signs (a TLS-inspecting "
                         "gateway re-signs traffic here), and BOSS has no company CA."),
                "action": ("An operator saves the company's root CA (PEM) on the BOSS host, sets "
                           f"BOSS_EXTRA_CA_FILE in .env to its path and {APPLY}.")}
    return None


def sentence(error: Any, *, then: str = "", ca: Optional[bool] = None) -> str:
    """explain() as one line for a person ("" when it does not apply)."""
    got = explain(error, ca=ca)
    if not got:
        return ""
    return " ".join(p for p in (got["what"], got["action"], then) if p)
