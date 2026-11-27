"""A git remote URL with nothing secret in it: the one scrubber for every remote BOSS shows.

A remote can carry a credential three ways: userinfo (`https://user:token@host/…`, where an
unencoded password may itself hold `@`), a query (`?private_token=…`) and a fragment
(`#token=…`). All three go. The host installer (scripts/install/self_fix_installer.py, which runs
with python3 alone and cannot import the engine) keeps a twin, `_public_url`, held to the same
cases by engine/tests/test_the_installer_records_where_boss_is_installed.py
(`test_an_origin_never_carries_a_credential_on_either_side`).
"""
from __future__ import annotations

import re

_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*://)(.*)$", re.I)


def public_remote(url: str) -> str:
    """`url` without userinfo, query or fragment (an scp-style `git@host:o/r` is kept)."""
    text = (url or "").strip()
    text = re.split(r"[?#]", text, maxsplit=1)[0]
    found = _SCHEME.match(text)
    if not found:
        return text
    scheme, rest = found.groups()
    slash = rest.find("/")
    at = rest.rfind("@", 0, slash if slash >= 0 else len(rest))
    return scheme + (rest[at + 1:] if at >= 0 else rest)


__all__ = ["public_remote"]
