#!/usr/bin/env python3
"""Render BOSS's refusal the way BOSS said it: "headline — fix".

Every setup refusal is one pill, {state, headline, fix} (setup_actions.SetupActionError). A
client that reads a field the server never sends prints `None`; one that adds its own advice
sends the person somewhere the server did not. Both happened (BOSSAPP-769: "None ... re-run
with --accept-database-fallback", a loop). So every installer client renders through here, and
nothing else is added.

As a command (for shell scripts): reads an HTTP response body on stdin and prints the rendered
refusal, or the fallback given as the first argument when the body holds none.
"""
from __future__ import annotations

import json
import sys
from typing import Any


def render(detail: Any) -> str:
    """{state, headline, fix} -> "headline — fix". Never "None"; never invented advice."""
    if isinstance(detail, dict):
        headline = _text(detail.get("headline")) or _text(detail.get("message"))
        fix = _text(detail.get("fix"))
        if headline and fix:
            return f"{headline} — {fix}"
        return headline or fix
    return _text(detail)


def from_body(body: Any) -> str:
    """A whole response body (bytes, text or parsed): its `detail`, rendered."""
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            body = json.loads(body or "{}")
        except ValueError:
            return ""
    if isinstance(body, dict):
        return render(body.get("detail", body if "headline" in body else None))
    return ""


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = str(value).strip()
    return "" if text in ("None", "null") else text


if __name__ == "__main__":
    fallback = sys.argv[1] if len(sys.argv) > 1 else ""
    print(from_body(sys.stdin.read()) or fallback)
