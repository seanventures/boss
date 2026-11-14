"""Finding ticket references in free text.

A Jira/Plane-style key (``KAN-12``) has the same shape as ``UTF-8``, ``SHA-256``, ``ISO-8601``
or ``RFC-7231``. So a key is accepted ONLY when its prefix is a known project key of a connected
system; without known keys a connector recognises its URLs and nothing else.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

#: The shape of an issue key. Group 1 = project key, group 2 = number.
KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9_\-/.#])([A-Z][A-Z0-9_]+)-(\d+)(?![A-Za-z0-9_\-])")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def find_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:!?") for m in _URL.finditer(text or "")]


def strip_urls(text: str) -> str:
    """The text with its URLs blanked, so a key inside a URL is not also read as a bare key."""
    return _URL.sub(" ", text or "")


def find_keys(text: str, known_keys: Optional[Iterable[str]]) -> list[tuple[str, str]]:
    """(``PROJECT-N``, ``PROJECT``) for every key whose project is known, in order, once each."""
    known = {str(k).strip().upper() for k in (known_keys or ()) if str(k).strip()}
    if not known:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in KEY_PATTERN.finditer(strip_urls(text)):
        project, number = match.group(1), match.group(2)
        if project not in known or number.startswith("0"):
            continue
        key = f"{project}-{number}"
        if key not in seen:
            seen.add(key)
            out.append((key, project))
    return out
