"""Pure version-comparison logic for the dependency freshness report."""

from __future__ import annotations

import re
from enum import Enum


class Freshness(str, Enum):
    CURRENT = "current"
    OUTDATED = "outdated"
    UNKNOWN = "unknown"


_LEADING_NUMERIC_SEGMENTS = re.compile(r"^\d+(?:\.\d+)*")


def _parse_version(v: str) -> tuple[int, ...] | None:
    """Parse the leading dotted-numeric run of a version string.

    Pre-release/build suffixes (``rc1``, ``+build5``) are stripped rather than
    rejected, so e.g. ``"1.2.3rc1"`` parses as ``(1, 2, 3)``.
    """
    if not v:
        return None
    match = _LEADING_NUMERIC_SEGMENTS.match(v.strip())
    if not match:
        return None
    segments = tuple(int(part) for part in match.group(0).split("."))
    return segments or None


def classify(current: str | None, latest: str | None) -> Freshness:
    try:
        current_parsed = _parse_version(current) if current else None
        latest_parsed = _parse_version(latest) if latest else None
        if current_parsed is None or latest_parsed is None:
            return Freshness.UNKNOWN

        length = max(len(current_parsed), len(latest_parsed))
        current_padded = current_parsed + (0,) * (length - len(current_parsed))
        latest_padded = latest_parsed + (0,) * (length - len(latest_parsed))

        if current_padded < latest_padded:
            return Freshness.OUTDATED
        return Freshness.CURRENT
    except Exception:
        return Freshness.UNKNOWN
