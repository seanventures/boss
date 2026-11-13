"""BOSSAPP-872: instance admins, separate from org roles.

Org Owner was the top role, so on a multi-org install whoever ran the install (plugins,
extensions, backups) had to be an Owner of an org — with that org's data. An INSTANCE ADMIN runs
the install and holds only the ``instance:*`` permissions (``instance:read``, ``instance:manage``),
whatever org role they hold; none is needed.

THE LAYER (``layer``, read by authz._held — the one place every holding is computed, so the PDP,
effective access and "why" all agree):

* No active instance admin: nothing changes. Org Owner and Admin hold ``instance:*`` through the
  catalogue (packages/identity/catalogue.yaml), exactly as they held the permissions the instance
  routes used before. A single-org install never has to think about this.
* Once one is named: ``instance:*`` comes ONLY from being an instance admin; an org role no longer
  confers it. An instance admin holds ``instance:*`` in their own org — the scope those routes
  resolve to — and nothing else through this layer.

Naming or removing one bumps every org's identity epoch, so every cached holding refetches.
The first one is named by someone who holds ``instance:manage`` (an Owner or Admin, before any
exists); the last one cannot remove themselves (the install would have no one to run it).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

#: What being an instance admin confers. Every one is in the catalogue (codegen checks keys).
PERMISSIONS = ("instance:read", "instance:manage")
ROLE_LABEL = "instance_admin"


class InstanceAdminError(ValueError):
    """A refusal said for a person."""


@dataclass(frozen=True)
class Layer:
    separated: bool          # at least one active instance admin exists
    is_admin: bool           # this principal is one


_LAYER_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM identity_instance_admins WHERE ended_at IS NULL) AS separated, "
    "       EXISTS (SELECT 1 FROM identity_instance_admins WHERE ended_at IS NULL "
    "               AND principal_id = :p) AS is_admin")


def layer(conn: Any, principal_id: str) -> Layer:
    """Read the instance layer for one principal (one round trip)."""
    row = conn.execute(_LAYER_SQL, {"p": principal_id}).mappings().first()
    return Layer(bool(row and row["separated"]), bool(row and row["is_admin"]))


def apply(why: dict, conn: Any, principal_id: str, why_type: Any) -> None:
    """Fold the layer into a holding being computed (authz._held), in place."""
    found = layer(conn, principal_id)
    if found.separated:
        for key in [k for k in why if k.startswith("instance:")]:
            why.pop(key, None)
    if found.is_admin:
        for key in PERMISSIONS:
            why.setdefault(key, []).append(why_type(
                role_key=ROLE_LABEL, binding_id=f"instance:{principal_id}", binding_scope="org"))


def _bump_all(conn: Any, reason: str) -> None:
    from . import epoch

    for (org_id,) in conn.execute(text("SELECT id FROM orgs")).all():
        epoch.bump(str(org_id), db=conn, reason=reason)


def list_admins(conn: Any, *, include_ended: bool = False) -> list[dict[str, Any]]:
    where = "" if include_ended else " WHERE a.ended_at IS NULL"
    rows = conn.execute(text(
        "SELECT a.principal_id, p.display_name, p.org_id, p.kind, a.granted_by, a.granted_at, "
        "       a.ended_by, a.ended_at "
        "FROM identity_instance_admins a LEFT JOIN identity_principals p ON p.id = a.principal_id"
        + where + " ORDER BY a.granted_at"), {}).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        for k in ("granted_at", "ended_at"):
            item[k] = item[k].isoformat() if item.get(k) else None
        out.append(item)
    return out


def grant(conn: Any, principal_id: str, *, actor: str) -> dict[str, Any]:
    """Name a person an instance admin. Only a person (a usr_ principal that is active)."""
    found = conn.execute(text(
        "SELECT kind, status FROM identity_principals WHERE id = :p"),
        {"p": principal_id}).mappings().first()
    if not found or found["kind"] != "user":
        raise InvalidPrincipal("Only a person can be an instance admin.")
    if found["status"] != "active":
        raise InvalidPrincipal("That person is not active.")
    conn.execute(text(
        "INSERT INTO identity_instance_admins (principal_id, granted_by) VALUES (:p, :a) "
        "ON CONFLICT (principal_id) DO UPDATE SET granted_by = EXCLUDED.granted_by, "
        "  granted_at = now(), ended_by = NULL, ended_at = NULL "
        "WHERE identity_instance_admins.ended_at IS NOT NULL"),
        {"p": principal_id, "a": actor})
    _bump_all(conn, "instance_admin.granted")
    return {"principal_id": principal_id, "instance_admin": True}


def revoke(conn: Any, principal_id: str, *, actor: str) -> dict[str, Any]:
    live = conn.execute(text(
        "SELECT principal_id FROM identity_instance_admins WHERE ended_at IS NULL FOR UPDATE"
    )).scalars().all()
    if principal_id not in live:
        raise InstanceAdminError("That principal is not an instance admin.")
    if len(live) == 1:
        raise InstanceAdminError(
            "This is the last instance admin. Name another first: an install needs someone to run it.")
    conn.execute(text(
        "UPDATE identity_instance_admins SET ended_by = :a, ended_at = now() "
        "WHERE principal_id = :p AND ended_at IS NULL"), {"p": principal_id, "a": actor})
    _bump_all(conn, "instance_admin.revoked")
    return {"principal_id": principal_id, "instance_admin": False}


class InvalidPrincipal(InstanceAdminError):
    pass


def list_backups(conn: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """The install's self-backups (BOSSAPP-838), newest first: an instance read."""
    rows = conn.execute(text(
        "SELECT id, kind, object_key, bytes, checksum, started_at, finished_at, verified_at, "
        "       status, error FROM backups ORDER BY started_at DESC LIMIT :n"),
        {"n": max(1, min(int(limit or 50), 500))}).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        for k in ("started_at", "finished_at", "verified_at"):
            item[k] = item[k].isoformat() if item.get(k) else None
        out.append(item)
    return out


def is_admin(conn: Any, principal_id: Optional[str]) -> bool:
    return bool(principal_id) and layer(conn, str(principal_id)).is_admin
