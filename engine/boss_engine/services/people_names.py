"""The one place an actor id becomes words a person reads.

"Stopped by usr_kdzhawzy7mxmp5hpb3bqlgqojs", "Approved by usr_kdzh…", "Approved by ceo": every
surface that named who did something printed the id it had. This module turns any actor into a
name, and never returns an id: a principal it cannot name reads "a person" (or "an agent").
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

#: An id, not a name: `usr_…`, `agt_…`, `svc_…` with an opaque tail.
_ID = re.compile(r"^(usr|agt|svc|sa|prn)_[a-z0-9]{6,}$", re.IGNORECASE)
_TTL_SECONDS = 60.0
_CACHE: dict[str, tuple[float, Optional[str]]] = {}
_LOCK = threading.Lock()


def looks_like_an_id(actor: Any) -> bool:
    return bool(_ID.match(str(actor or "").strip()))


def _lookup(actor: str, connection: Any = None) -> Optional[str]:
    with _LOCK:
        hit = _CACHE.get(actor)
    if hit and time.monotonic() - hit[0] < _TTL_SECONDS:
        return hit[1]
    from sqlalchemy import text

    name: Optional[str] = None
    try:
        if connection is not None:
            name = connection.execute(text(
                "SELECT display_name FROM identity_principals WHERE id = :i"), {"i": actor}).scalar()
        else:
            from ..config import get_settings
            from ..db import get_engine

            with get_engine(get_settings()).connect() as c:
                name = c.execute(text(
                    "SELECT display_name FROM identity_principals WHERE id = :i"),
                    {"i": actor}).scalar()
    except Exception:  # noqa: BLE001 — a name is a nicety; the words still read without it
        name = None
    name = (str(name).strip() or None) if name else None
    with _LOCK:
        _CACHE[actor] = (time.monotonic(), name)
    return name


def known_name(actor: Any, connection: Any = None) -> Optional[str]:
    """A display name only for a principal id whose directory name is a real name.

    A party word ("ceo", "agent:pm") is None — the console already has words for those.
    A display name that is itself an id is None. The stored id is never replaced; a caller
    that has a name puts it beside the id."""
    pid = str(actor or "").strip()
    if not looks_like_an_id(pid):
        return None
    name = _lookup(pid, connection)
    if not name or looks_like_an_id(name):
        return None
    return name


def principal_name(actor: Any, connection: Any = None) -> Optional[str]:
    """A person's or agent's name for an actor id, or None when there is nothing to name
    (no actor, an automatic approval, a policy)."""
    pid = str(actor or "").strip()
    if not pid or pid == "auto_approval" or pid.startswith("policy:"):
        return None
    if pid.startswith("autonomy:dev:"):
        # "Decided by <Agent> under dev autonomy" (autonomy_gate, BOSSAPP-1441).
        from .autonomy_gate import ACTOR_PREFIX, decided_words

        return decided_words(pid[len(ACTOR_PREFIX):])
    if pid == "ceo":
        return "the owner"
    if not looks_like_an_id(pid):
        return pid                      # already words ("BOSS", "workflow", a role name)
    return _lookup(pid, connection)


def who(actor: Any, connection: Any = None, *, viewer: Optional[str] = None) -> str:
    """Who did it, in words, for a sentence: "you", a name, or "a person". Never an id."""
    pid = str(actor or "").strip()
    if viewer and pid and pid == viewer:
        return "you"
    name = principal_name(pid, connection)
    if name:
        return name
    if pid.lower().startswith("agt_"):
        return "an agent"
    return "a person"
