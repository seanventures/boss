"""A previewed app's OWN cookies, and nothing else (sign-in inside a preview).

A preview used to get no cookie at all: the gateway dropped the browser's Cookie header and every
Set-Cookie, so an app that keeps a session (login-oauth, any server-side session) could never stay
signed in inside a preview. Cookies are not port-scoped, so the browser sends the console's cookies
to the preview origin too; forwarding the header as-is would hand a tenant app the operator's
session. The rule here forwards the app's own cookies and provably nothing else:

  * OUT (the app's Set-Cookie): the cookie is RENAMED with this preview's tag
    (`bp<12 hex of sha256(preview id)>_<name>`), its Path forced to `/v1/previews/<id>/` and any
    Domain removed (host-only). A Set-Cookie that names a BOSS cookie (`__Host-boss_*`,
    `__Secure-boss_*`, `boss_*`, `boss-*`), or a Path under `/v1/previews/` that is not this
    preview's, is dropped, never relayed.
  * IN (the browser's Cookie): only cookies carrying THIS preview's tag pass, with the tag removed.
    The console session, its pre-auth and device cookies, every other `boss_*` cookie and every
    other preview's cookies are dropped by construction: none of them carries this tag.

The browser keeps a preview's cookies to that preview's path; the tag makes the server side not
depend on it (a cookie that reached the wrong path still names its own preview).
`apps/api/app/preview_origin.py` applies the same checks again at the preview origin's edge.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional

#: A preview id this module will mint cookies for: one plain path segment.
_PREVIEW_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
#: BOSS's own cookie names (the console session, pre-auth, device, app sessions, boss_tz, ...).
_BOSS_COOKIE = re.compile(r"^(?:__host-|__secure-)?boss[_-]", re.I)
#: A cookie name (RFC 6265 token).
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def cookie_path(preview_id: str) -> str:
    return f"/v1/previews/{preview_id}/"


def cookie_tag(preview_id: str) -> str:
    return "bp" + hashlib.sha256(preview_id.encode("utf-8")).hexdigest()[:12] + "_"


def is_boss_cookie(name: str) -> bool:
    return bool(_BOSS_COOKIE.match((name or "").strip()))


def _ok_id(preview_id: Optional[str]) -> bool:
    return bool(preview_id) and bool(_PREVIEW_ID.match(str(preview_id)))


def _pairs(headers: Iterable[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for header in headers:
        for part in str(header or "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name.strip():
                out.append((name.strip(), value.strip()))
    return out


def own_cookie_pairs(preview_id: str, headers: Iterable[str]) -> list[tuple[str, str]]:
    """The (tagged name, value) pairs of the Cookie header(s) that belong to this preview."""
    if not _ok_id(preview_id):
        return []
    tag = cookie_tag(preview_id)
    return [(n, v) for n, v in _pairs(headers)
            if n.startswith(tag) and len(n) > len(tag) and not is_boss_cookie(n[len(tag):])]


def inbound_cookie_header(preview_id: str, header: Optional[str]) -> Optional[str]:
    """The Cookie header the previewed app receives: its own cookies, untagged; None when none."""
    tag_len = len(cookie_tag(preview_id)) if _ok_id(preview_id) else 0
    pairs = own_cookie_pairs(preview_id, [header or ""])
    return "; ".join(f"{n[tag_len:]}={v}" for n, v in pairs) or None


def outbound_set_cookie(preview_id: str, value: str) -> Optional[str]:
    """The app's Set-Cookie as the browser may store it (tagged, scoped to this preview), or None
    when it must not be relayed."""
    if not _ok_id(preview_id) or not value or "\n" in value or "\r" in value:
        return None
    first, *attributes = str(value).split(";")
    name, sep, cookie_value = first.strip().partition("=")
    name = name.strip()
    if not sep or not _TOKEN.match(name) or is_boss_cookie(name):
        return None
    own = cookie_path(preview_id)
    kept: list[str] = []
    for raw in attributes:
        key, _, attr_value = raw.strip().partition("=")
        low = key.strip().lower()
        if not low:
            continue
        if low == "path":
            path = attr_value.strip()
            if path.lower().startswith("/v1/previews/") and not path.startswith(own):
                return None   # aimed at another preview (or the gateway itself): never relayed
            continue          # every path the app names becomes this preview's
        if low == "domain":
            continue          # host-only: a preview never widens a cookie to other hosts
        kept.append(raw.strip())
    return "; ".join([f"{cookie_tag(preview_id)}{name}={cookie_value.strip()}", *kept, f"Path={own}"])


def is_relayable_set_cookie(preview_id: str, value: str) -> bool:
    """The edge check: a Set-Cookie leaves the preview origin only when it is one
    `outbound_set_cookie` made for THIS preview (tagged, its path exactly, no Domain)."""
    if not _ok_id(preview_id) or not value or "\n" in value or "\r" in value:
        return False
    first, *attributes = str(value).split(";")
    name = first.strip().partition("=")[0].strip()
    tag = cookie_tag(preview_id)
    if not name.startswith(tag) or len(name) == len(tag) or is_boss_cookie(name[len(tag):]):
        return False
    paths = []
    for raw in attributes:
        key, _, attr_value = raw.strip().partition("=")
        if key.strip().lower() == "domain":
            return False
        if key.strip().lower() == "path":
            paths.append(attr_value.strip())
    return paths == [cookie_path(preview_id)]
