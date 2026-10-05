"""Optimistic concurrency for Govern's writes: `If-Match` is optional and, when sent, guarding.

Two operators editing the same limit used to race silently — the second save simply won. A
Govern edit now sends the version it was shown; if the limit changed since, the write is
refused with 412 and says who changed it. A caller that sends no `If-Match` (the chat command,
scripts, Setup) gets exactly the old behaviour.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request


def govern_actor(claimed: Optional[str], principal: Optional[str] = None) -> str:
    """THE ONE ACTOR RULE for every Govern write (settings, Pace, monthly cap, token guides,
    FinOps): the signed-in operator the console names wins; the API key's principal only when
    no one is named. The console reaches the API with one key, so preferring the key recorded
    every change as that key's principal (`ceo`) and checked its rights instead of the
    operator's."""
    for candidate in (claimed, principal):
        value = str(candidate or "").strip()
        if value:
            return value
    return "executive_console"


def sent_version(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    raw = request.headers.get("if-match")
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip().strip('"')


def require_fresh(request: Optional[Request], current: str, changed_by: Optional[str]) -> None:
    """412 when the caller's version is not the current one; nothing when it sent none."""
    sent = sent_version(request)
    if sent is None or sent == current:
        return
    who = changed_by or "someone"
    raise HTTPException(status_code=412, detail={
        "message": f"Changed by {who} just now",
        "changed_by": changed_by,
        "version": current,
    })
