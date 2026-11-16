"""Pin where a built app may run, without moving the control plane.

An admin chooses a geography (the EU is one). A deploy whose target geography
is not that pin is refused. No pin means the deploy keeps the path it already
had. Geography is a label compared here. This module never calls a cloud.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import text

# A cloud region id has a hyphenated shape ending in a number. Matching the
# shape refuses the label. The names are not listed, and nothing is called.
_CLOUD_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,3}-\d[a-z]?$")

_ADMIN_ROLES = frozenset({"admin", "owner", "org_admin"})

_NOT_CONTACTED = "That label is a cloud region. It was not contacted."
_NOT_THE_PIN = "Target geography is not the pin."


class ResidencyRefused(Exception):
    """A pin or a deploy the control plane will not accept."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def geographies() -> list[dict[str, str]]:
    """The labels an admin may pin. None of these is a cloud region id."""
    return [
        {"id": "eu", "label": "European Union", "kind": "geography"},
        {"id": "uk", "label": "United Kingdom", "kind": "geography"},
        {"id": "us", "label": "United States", "kind": "geography"},
        {"id": "apac", "label": "Asia Pacific", "kind": "geography"},
    ]


def _roles(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return set()
    if not isinstance(value, list):
        return set()
    return {str(item).strip().lower() for item in value if str(item).strip()}


def _is_admin(connection: Any, project_id: str, actor: str) -> bool:
    actor = (actor or "").strip()
    if not actor:
        return False
    from boss_engine.identity.instance_admin import is_admin

    if is_admin(connection, actor):
        return True
    member = connection.execute(
        text(
            "SELECT role_keys FROM project_members "
            "WHERE project_id = :p AND member_id = :a AND status = 'active'"
        ),
        {"p": project_id, "a": actor},
    ).mappings().first()
    if member and _roles(member["role_keys"]) & _ADMIN_ROLES:
        return True
    org_member = connection.execute(
        text(
            "SELECT om.role_keys FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id "
            "JOIN org_members om ON om.org_id = w.org_id "
            "WHERE p.id = :p AND om.member_id = :a AND om.status = 'active'"
        ),
        {"p": project_id, "a": actor},
    ).mappings().first()
    return bool(org_member and _roles(org_member["role_keys"]) & _ADMIN_ROLES)


def _require_admin(connection: Any, project_id: str, actor: str) -> None:
    if not _is_admin(connection, project_id, actor):
        raise ResidencyRefused("Only an admin can pin.", code="not_admin")


def _parse_geography(value: str) -> str:
    raw = (value or "").strip().lower()
    if _CLOUD_REGION.match(raw):
        raise ResidencyRefused(_NOT_CONTACTED, code="cloud_region")
    known = {item["id"] for item in geographies()}
    if raw not in known:
        raise ResidencyRefused("Unknown geography.", code="unknown_geography")
    return raw


def _load(connection: Any, project_id: str, app_key: str) -> Optional[dict[str, Any]]:
    return connection.execute(
        text(
            "SELECT id, geography, control_plane_host FROM app_residency_pins "
            "WHERE project_id = :p AND app_key = :a"
        ),
        {"p": project_id, "a": app_key},
    ).mappings().first()


def _host(row: Optional[dict[str, Any]], fallback: str) -> str:
    """The stored control-plane host wins. A later call cannot replace it."""
    if row is None:
        return fallback
    stored = row.get("control_plane_host")
    if stored:
        return str(stored)
    return fallback


def _view(
    *,
    project_id: str,
    app_key: str,
    geography: Optional[str],
    control_plane_host: str,
) -> dict[str, Any]:
    pinned_to = geography or None
    return {
        "project_id": project_id,
        "app_key": app_key,
        "geography": pinned_to,
        "pinned": pinned_to is not None,
        "control_plane_host": control_plane_host,
        "cloud_contacted": False,
    }


def read_pin(
    connection: Any,
    *,
    project_id: str,
    app_key: str,
    control_plane_host: str,
) -> dict[str, Any]:
    row = _load(connection, project_id, app_key)
    geography = (row.get("geography") if row else None) or None
    return _view(
        project_id=project_id,
        app_key=app_key,
        geography=geography,
        control_plane_host=_host(row, control_plane_host),
    )


def set_pin(
    connection: Any,
    *,
    project_id: str,
    app_key: str,
    geography: str,
    actor: str,
    control_plane_host: str,
) -> dict[str, Any]:
    _require_admin(connection, project_id, actor)
    chosen = _parse_geography(geography)
    row = _load(connection, project_id, app_key)
    if row is None:
        connection.execute(
            text(
                "INSERT INTO app_residency_pins "
                "(id, project_id, app_key, geography, control_plane_host, pinned_by) "
                "VALUES (:i, :p, :a, :g, :h, :by)"
            ),
            {
                "i": f"rsp_{uuid4().hex[:16]}",
                "p": project_id,
                "a": app_key,
                "g": chosen,
                "h": control_plane_host,
                "by": actor,
            },
        )
        host = control_plane_host
    else:
        # The host column is left alone on purpose. Changing geography is not
        # a move of the control plane.
        host = _host(row, control_plane_host)
        connection.execute(
            text(
                "UPDATE app_residency_pins SET geography = :g, pinned_by = :by, "
                "updated_at = now() WHERE id = :i"
            ),
            {"g": chosen, "by": actor, "i": row["id"]},
        )
    return _view(
        project_id=project_id,
        app_key=app_key,
        geography=chosen,
        control_plane_host=host,
    )


def clear_pin(
    connection: Any, *, project_id: str, app_key: str, actor: str
) -> dict[str, Any]:
    _require_admin(connection, project_id, actor)
    row = _load(connection, project_id, app_key)
    host = _host(row, "")
    if row is not None:
        connection.execute(
            text(
                "UPDATE app_residency_pins SET geography = NULL, pinned_by = :by, "
                "updated_at = now() WHERE id = :i"
            ),
            {"by": actor, "i": row["id"]},
        )
    return _view(
        project_id=project_id,
        app_key=app_key,
        geography=None,
        control_plane_host=host,
    )


def admit_deploy(
    connection: Any,
    *,
    project_id: str,
    app_key: str,
    target_geography: str,
    app_path: str,
    control_plane_host: str,
) -> dict[str, Any]:
    """Allow today's path when nothing is pinned. Otherwise the target must match.

    `app_path` is returned as given. A pin never rewrites it into a cloud URL,
    and `cloud_contacted` is always false because no region API is called.
    """
    row = _load(connection, project_id, app_key)
    geography = (row.get("geography") if row else None) or None
    result = _view(
        project_id=project_id,
        app_key=app_key,
        geography=geography,
        control_plane_host=_host(row, control_plane_host),
    )
    result["app_path"] = app_path
    result["target_geography"] = target_geography
    if geography is None:
        result["allowed"] = True
        result["path"] = "today"
        result["reason"] = None
        return result
    target = (target_geography or "").strip().lower()
    if _CLOUD_REGION.match(target):
        result["allowed"] = False
        result["path"] = "refused"
        result["reason"] = _NOT_CONTACTED
        return result
    if target != geography:
        result["allowed"] = False
        result["path"] = "refused"
        result["reason"] = _NOT_THE_PIN
        return result
    result["allowed"] = True
    result["path"] = "pinned"
    result["reason"] = None
    return result
