#!/usr/bin/env python3
"""Render and validate the sealed-e2e-canary README note (BOSS-351 slice 9).

The note is a single, honest line that marks a PR as a narrow sealed E2E
canary: it names the plane, says the change holds for a human Decision,
and never claims a merge happened. This module stays consistent with the
canary contract defined in scripts/qa/sealed_e2e_live.py.
"""

from __future__ import annotations

from typing import Any

from scripts.qa.sealed_e2e_live import CANARY_TITLE, PLANE

CANARY_MARKER = CANARY_TITLE
DEFAULT_PLANE_ID = PLANE

_MERGE_CLAIM_PHRASES = (
    "merged",
    "auto-merge",
    "merge complete",
    "merge is complete",
    "will merge",
    "merging now",
    "merge shortly",
)


def render_canary_note(plane_id: str = DEFAULT_PLANE_ID) -> str:
    """Render the one-line sealed-e2e-canary README note."""
    plane_id = (plane_id or "").strip()
    if not plane_id:
        raise ValueError("plane_id must not be blank")
    return (
        f"Note: this PR is a {CANARY_MARKER} for {plane_id}; "
        "it holds for a human Decision and no merge is attempted."
    )


def extract_canary_note_lines(text: str) -> list[str]:
    """Return lines mentioning the canary marker, skipping fenced code blocks."""
    lines: list[str] = []
    in_code_block = False
    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if CANARY_MARKER in raw_line:
            lines.append(raw_line.strip())
    return lines


def validate_canary_note(
    text: str, plane_id: str = DEFAULT_PLANE_ID
) -> dict[str, Any]:
    """Validate that ``text`` (e.g. a README) contains the canary note."""
    lines = extract_canary_note_lines(text)
    if not lines:
        return {"ok": False, "missing": ["marker"], "note": None}

    missing: list[str] = []
    if len(lines) > 1:
        missing.append("single_line")

    note = lines[0] if len(lines) == 1 else None

    if not any(plane_id in candidate for candidate in lines):
        missing.append("plane_id")

    if any(
        phrase in candidate.lower()
        for candidate in lines
        for phrase in _MERGE_CLAIM_PHRASES
    ):
        missing.append("no_merge_claim")

    return {"ok": not missing, "missing": missing, "note": note}
