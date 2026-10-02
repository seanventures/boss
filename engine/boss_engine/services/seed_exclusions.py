"""What an operator took away, so that a seed does not quietly give it back.

WHY THIS EXISTS
---------------
Every install, upgrade, `make seed` and POST /v1/bootstrap/seed runs the same seed, and the seed
was written to converge: it re-grants every skill a role spec names (`sync_role_skills`),
re-registers every built-in MCP server by name (`ensure_mcp_server`), re-installs every bundled
role and rewrites its spec (`_upsert_role_spec`), and re-creates every Agent Studio profile.

Converging is right on a fresh project and wrong on one somebody has been running. On boss-1 the
events table held 48 `skill.assigned_to_agent` rows for 16 grants: the same grants, given back
three times, including ones an operator had taken off an agent on purpose. A remove button whose
effect lasts until the next upgrade is not a remove button, and an operator who learns that stops
trusting every other control on the page.

THE RULE
--------
The seed is additive only. It may add what a project has never had; it may never put back what a
person removed, and it may never overwrite what a person edited. Each operator removal or edit
writes one row here, and every seed path asks this table before it writes. An operator who later
gives the same thing back by hand lifts the row — that is a new decision, and it wins.

Kinds, each named by what the seed would otherwise resurrect:

* ``role_skill``  — subject ``<role_key>:<skill_id>``. Written by unassign, read by
  ``sync_role_skills``.
* ``mcp_server``  — subject is the server NAME, because built-ins are ensured by name and a
  re-registered server gets a new id. Written by delete, read by ``ensure_mcp_server``.
* ``role``        — subject ``<role_key>``. Written when an agent is deleted, read by the
  role installer and the Agent Studio catalogue seed.
* ``role_spec``   — subject ``<role_key>``. Written when an operator edits an agent's mission,
  name or boundaries; read by the role installer, which then leaves the spec alone.

`SEED_PATHS` in tests/test_the_seed_never_undoes_an_operator.py enumerates every seed function
that writes one of these, so a new seed path that forgets to ask fails a test rather than an
operator.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

KINDS = ("role_skill", "mcp_server", "role", "role_spec")


def _check(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown seed exclusion kind {kind!r}; expected one of {KINDS}")
    return kind


def _table_ready(connection: Connection) -> bool:
    # A database whose schema apply has not reached this table yet must still seed. Missing
    # table means nothing has been excluded yet, which is exactly the state before this existed.
    try:
        return connection.execute(
            text("SELECT to_regclass('seed_exclusions')")).scalar() is not None
    except Exception:  # noqa: BLE001 — a non-Postgres test double
        return False


def exclude(connection: Connection, project_id: str, kind: str, subject: str, *,
            actor: str, reason: str = "") -> None:
    """Record that an operator removed or edited `subject`, so no seed undoes it."""
    if not _table_ready(connection):
        return
    connection.execute(
        text(
            "INSERT INTO seed_exclusions (project_id, kind, subject, excluded_by, reason) "
            "VALUES (:p, :k, :s, :by, :why) "
            "ON CONFLICT (project_id, kind, subject) DO UPDATE "
            "SET excluded_by = EXCLUDED.excluded_by, reason = EXCLUDED.reason, created_at = now()"
        ),
        {"p": project_id, "k": _check(kind), "s": str(subject), "by": str(actor or "system"),
         "why": str(reason or "")[:400]},
    )


def lift(connection: Connection, project_id: str, kind: str, subject: str) -> None:
    """An operator gave it back by hand. The newer decision wins."""
    if not _table_ready(connection):
        return
    connection.execute(
        text("DELETE FROM seed_exclusions WHERE project_id = :p AND kind = :k AND subject = :s"),
        {"p": project_id, "k": _check(kind), "s": str(subject)},
    )


def excluded(connection: Connection, project_id: str, kind: str) -> set[str]:
    """Every subject of one kind the seed must leave alone in this project."""
    if not _table_ready(connection):
        return set()
    return {
        str(row)
        for row in connection.execute(
            text("SELECT subject FROM seed_exclusions WHERE project_id = :p AND kind = :k"),
            {"p": project_id, "k": _check(kind)},
        ).scalars().all()
    }


def is_excluded(connection: Connection, project_id: str, kind: str, subject: str) -> bool:
    return str(subject) in excluded(connection, project_id, kind)


def role_skill_subject(role_key: str, skill_id: str) -> str:
    return f"{str(role_key).strip().lower()}:{str(skill_id).strip()}"


def reason_for(connection: Connection, project_id: str, kind: str,
               subject: str) -> Optional[dict]:
    if not _table_ready(connection):
        return None
    row = connection.execute(
        text("SELECT excluded_by, reason, created_at FROM seed_exclusions "
             "WHERE project_id = :p AND kind = :k AND subject = :s"),
        {"p": project_id, "k": _check(kind), "s": str(subject)},
    ).mappings().first()
    return dict(row) if row else None
