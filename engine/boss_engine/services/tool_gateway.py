"""Whether one build-agent tool call may run (BOSSAPP-813).

The native build loop asks this before it invokes the tool. ``allow`` runs.
``ask`` waits: the call does not run. ``off`` does not run. A missing entry
is ``allow``, which is what an unconfigured build does today. Selectors,
argument hashes, always-allow rules and an audit log are not this slice.
"""
from __future__ import annotations

from typing import Any

RUN = "run"
WAIT = "wait"
DENY = "deny"

_ALLOW = "allow"
_ASK = "ask"
_OFF = "off"


def verdict(name: str, policy: Any) -> str:
    """``run``, ``wait`` or ``deny`` for this tool name. Unknown words deny."""
    if not isinstance(policy, dict):
        return RUN
    word = str(policy.get(name, _ALLOW) or _ALLOW).strip().lower()
    if word == _ALLOW:
        return RUN
    if word == _ASK:
        return WAIT
    if word == _OFF:
        return DENY
    return DENY
