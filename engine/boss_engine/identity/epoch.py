"""The identity epoch: one counter per org that every identity mutation bumps (IDENTITY.md §6.6).

Stream S2. The PDP caches each principal's effective permissions per (principal, scope, epoch), and
it reads the epoch on EVERY decision (a primary-key lookup on identity_settings). So a mutation is
visible to the very next request in every process, with no listener to lose and no TTL to wait out:
revocation latency is the commit, not "≤5 s". `bump` still NOTIFYs `identity_epoch` for consumers
that hold state outside a request (the egress proxy, the console socket).

Every function takes `db=`: a SQLAlchemy Connection, used in the caller's transaction. `None` opens
a short connection of its own.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import text

NOTIFY_CHANNEL = "identity_epoch"


@contextmanager
def connection(db: Any = None, *, write: bool = False) -> Iterator[Any]:
    """The caller's connection, or a short-lived one (a transaction when `write`)."""
    if db is not None:
        yield db
        return
    from ..db import get_engine

    engine = get_engine()
    if write:
        with engine.begin() as conn:
            yield conn
    else:
        with engine.connect() as conn:
            yield conn


def read(org_id: str, *, db: Any = None) -> int:
    with connection(db) as conn:
        value = conn.execute(text("SELECT epoch FROM identity_settings WHERE org_id = :o"),
                             {"o": org_id}).scalar()
    return int(value or 0)


def bump(org_id: str, *, db: Any, reason: str) -> int:
    """Increment the org's epoch in the caller's transaction and NOTIFY it (delivered at commit)."""
    if db is None:
        raise ValueError("bump_epoch runs in the caller's transaction: pass db=")
    epoch = db.execute(text(
        "INSERT INTO identity_settings (org_id, epoch, updated_at) VALUES (:o, 1, now()) "
        "ON CONFLICT (org_id) DO UPDATE SET epoch = identity_settings.epoch + 1, updated_at = now() "
        "RETURNING epoch"), {"o": org_id}).scalar()
    db.execute(text("SELECT pg_notify(:c, :p)"),
               {"c": NOTIFY_CHANNEL, "p": f"{org_id}:{int(epoch)}:{reason[:120]}"})
    return int(epoch)
