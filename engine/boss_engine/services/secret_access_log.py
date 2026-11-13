"""BOSSAPP-865: the secret access log, secret versions, and user / agent scoped secrets.

"Which run read the prod Stripe key?" had no answer: a resolution left no trace. Now every
resolution through ``secret_backends.resolve_secret_ref`` (THE place a reference becomes a value)
appends one ``secret_access_events`` row — resolved or refused, with the run, the person and the
agent it was for, and the version it read. The table is append-only (the database rejects update
and delete). The value is never in it.

Versions: each write of a reference (``secrets_service.put_secret``) adds a ``secret_versions``
row (fingerprint, last4, who, when — what BOSS may remember of a secret, never the material) and
moves ``secret_refs.version``, so an access row says which version it read.

Scopes: besides project / workspace / org (and a deployment target or host), a reference may be
scoped to ONE person (``scope_type='user'``, ``scope_id`` a ``usr_`` principal) or ONE agent
(``scope_type='agent'``, ``scope_id`` a role key). Such a reference resolves only when the code
runs for that person or as that agent (connection_credentials.principal_from_context); anyone
else is refused, and the refusal is logged.

Every write here runs in its own savepoint and never fails the caller: a resolution must not
break because its log row could not be written (an install whose schema predates the table).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

from .core import make_id

#: The scopes that name one subject rather than a container.
SUBJECT_SCOPES = ("user", "agent")
#: An identical access (same reference, run, person, agent, outcome) inside this window is one
#: event: a model lane that resolves its key on every call would otherwise write a row per call.
REPEAT_SECONDS = 60.0

_SEEN: dict[tuple, float] = {}
_SEEN_LOCK = threading.Lock()


class _Who:
    __slots__ = ("person_id", "agent_role", "principal", "run_id")

    def __init__(self, person_id=None, agent_role=None, principal=None, run_id=None):
        self.person_id, self.agent_role = person_id, agent_role
        self.principal, self.run_id = principal, run_id


def who_is_accessing(connection: Any, project_id: Optional[str]) -> _Who:
    """The person, agent, verified principal and run this code resolves a secret for. Best
    effort: an unknown field stays None."""
    person = agent = principal = None
    try:
        from .connection_credentials import principal_from_context

        cp = principal_from_context(connection, str(project_id or ""))
        person, agent = cp.person_id, cp.agent_role
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..identity import lineage

        acting = lineage.current()
        if acting is not None:
            principal = acting.principal_id
    except Exception:  # noqa: BLE001
        pass
    if not agent:
        try:
            from .llm_telemetry import _llm_ctx

            agent = (_llm_ctx.get() or {}).get("role_key") or None
        except Exception:  # noqa: BLE001
            pass
    from .run_secret_redaction import current_run_id

    return _Who(person_id=person, agent_role=agent, principal=principal, run_id=current_run_id())


def scope_admits(row: Any, who: _Who) -> Optional[str]:
    """None when this reference may resolve for `who`; else the refusal's reason. Only the
    subject scopes are checked here (the container scopes are resolve_secret_ref's own rule)."""
    scope = str(row.get("scope_type") or "project")
    if scope not in SUBJECT_SCOPES:
        return None
    subject = str(row.get("scope_id") or "")
    if scope == "user":
        return None if who.person_id and who.person_id == subject else \
            "scoped to another person"
    return None if who.agent_role and who.agent_role.strip().lower() == subject.strip().lower() \
        else "scoped to another agent"


def _repeat(key: tuple) -> bool:
    now = time.monotonic()
    with _SEEN_LOCK:
        last = _SEEN.get(key)
        if last is not None and now - last < REPEAT_SECONDS:
            return True
        _SEEN[key] = now
        if len(_SEEN) > 5000:
            for k in [k for k, t in _SEEN.items() if now - t >= REPEAT_SECONDS]:
                _SEEN.pop(k, None)
    return False


def record_access(connection: Any, *, secret_ref_id: str, project_id: Optional[str],
                  outcome: str, who: Optional[_Who] = None, reason: Optional[str] = None
                  ) -> Optional[str]:
    """Append one access row (in its own savepoint). Returns its id, or None when nothing was
    written (a repeat inside REPEAT_SECONDS, a connection that cannot hold a savepoint, a schema
    without the table). Never raises."""
    if outcome not in ("resolved", "refused"):
        raise ValueError(f"unknown outcome {outcome!r}")
    try:
        who = who or who_is_accessing(connection, project_id)
        key = (secret_ref_id, who.run_id, who.person_id, who.agent_role, who.principal, outcome,
               reason)
        if _repeat(key):
            return None
        row_id = make_id("sae")
        with connection.begin_nested():
            version = connection.execute(text(
                "SELECT version FROM secret_refs WHERE id = :i"), {"i": secret_ref_id}).scalar()
            connection.execute(text(
                "INSERT INTO secret_access_events (id, secret_ref_id, project_id, version, outcome, "
                " reason, run_id, person_id, agent_role, principal) VALUES (:id, :ref, :p, :v, "
                " :o, :r, :run, :person, :agent, :principal)"),
                {"id": row_id, "ref": secret_ref_id, "p": project_id, "v": version,
                 "o": outcome, "r": (reason or "")[:300] or None, "run": who.run_id,
                 "person": who.person_id, "agent": who.agent_role, "principal": who.principal})
        return row_id
    except Exception:  # noqa: BLE001 — the log never fails a resolution
        return None


def record_version(connection: Any, *, secret_ref_id: str, fingerprint: Optional[str],
                   last4: Optional[str], actor: str) -> Optional[int]:
    """A write of this reference: the next version (1 for the first), recorded and set on the
    reference. Returns the version, or None when it could not be recorded. Never raises."""
    try:
        with connection.begin_nested():
            version = int(connection.execute(text(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM secret_versions "
                "WHERE secret_ref_id = :i"), {"i": secret_ref_id}).scalar() or 1)
            connection.execute(text(
                "INSERT INTO secret_versions (id, secret_ref_id, version, fingerprint, last4, "
                " created_by) VALUES (:id, :ref, :v, :f, :l, :by)"),
                {"id": make_id("sver"), "ref": secret_ref_id, "v": version, "f": fingerprint,
                 "l": last4, "by": actor})
            connection.execute(text("UPDATE secret_refs SET version = :v WHERE id = :i"),
                               {"v": version, "i": secret_ref_id})
        return version
    except Exception:  # noqa: BLE001
        return None


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def access_log(connection: Any, secret_ref_id: str, *, limit: int = 100,
               before: Optional[str] = None) -> dict[str, Any]:
    """The reference's versions and its most recent access events (newest first). Metadata
    only, like every other secret read."""
    limit = max(1, min(int(limit or 100), 500))
    params: dict[str, Any] = {"i": secret_ref_id, "n": limit}
    cursor = ""
    if before:
        cursor = " AND accessed_at < CAST(:before AS timestamptz)"
        params["before"] = before
    events = connection.execute(text(
        "SELECT id, version, outcome, reason, run_id, person_id, agent_role, principal, accessed_at "
        "FROM secret_access_events WHERE secret_ref_id = :i" + cursor +
        " ORDER BY accessed_at DESC, id DESC LIMIT :n"), params).mappings().all()
    versions = connection.execute(text(
        "SELECT version, fingerprint, last4, created_by, created_at FROM secret_versions "
        "WHERE secret_ref_id = :i ORDER BY version DESC"), {"i": secret_ref_id}).mappings().all()
    current = connection.execute(text(
        "SELECT version, scope_type, scope_id FROM secret_refs WHERE id = :i"),
        {"i": secret_ref_id}).mappings().first()
    return {
        "secret_ref_id": secret_ref_id,
        "version": (current or {}).get("version"),
        "scope_type": (current or {}).get("scope_type"),
        "scope_id": (current or {}).get("scope_id"),
        "versions": [{**dict(v), "created_at": _iso(v["created_at"])} for v in versions],
        "events": [{**dict(e), "accessed_at": _iso(e["accessed_at"])} for e in events],
    }


def reset_for_tests() -> None:
    with _SEEN_LOCK:
        _SEEN.clear()
