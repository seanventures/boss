"""Workspace design system (BOSSAPP-1254).

One system per workspace: a name, colors, type and spacing. A new app reads that
system when it is created and keeps the spec it read. A caller whose org does not
own the workspace gets not_found and none of the saved values. Factory template
files are not where this lives.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text

from .core import make_id

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,24}$")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_FACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+-]{0,59}$")
_TYPE_ROLES = ("display", "body", "utility")


class DesignSystemError(Exception):
    """A refusal with a status, a machine code and a sentence a person can act on."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def body(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _missing() -> DesignSystemError:
    return DesignSystemError(404, "not_found", "No design system for that workspace.")


def _name(value: Any, *, what: str) -> str:
    name = " ".join(str(value or "").split())
    if not name or len(name) > 80 or any(ord(ch) < 32 for ch in name):
        raise DesignSystemError(422, "name_required", f"Name the {what}.")
    return name


def _colors(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value or len(value) > 8:
        raise DesignSystemError(422, "invalid_color", "Name at least one #RRGGBB color.")
    out: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise DesignSystemError(422, "invalid_color", "Color names are short lowercase words.")
        if not isinstance(raw, str) or _HEX.fullmatch(raw) is None:
            raise DesignSystemError(422, "invalid_color", f"{key} needs a #RRGGBB color.")
        out[key] = raw
    return out


def _type_roles(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - set(_TYPE_ROLES):
        raise DesignSystemError(
            422, "invalid_type", "Type needs a display face and a body face.")
    out: dict[str, str] = {}
    for role in _TYPE_ROLES:
        raw = value.get(role)
        if raw is None or raw == "":
            if role == "utility":
                continue
            raise DesignSystemError(
                422, "invalid_type", "Type needs a display face and a body face.")
        face = str(raw).strip() if isinstance(raw, str) else ""
        if _FACE.fullmatch(face) is None:
            raise DesignSystemError(422, "invalid_type", f"The {role} face needs a font name.")
        out[role] = face
    return out


def _spacing(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or not value or len(value) > 8:
        raise DesignSystemError(422, "invalid_spacing", "Spacing needs a positive step.")
    out: dict[str, int] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise DesignSystemError(
                422, "invalid_spacing", "Spacing names are short lowercase words.")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > 160:
            raise DesignSystemError(422, "invalid_spacing", f"{key} needs a step from 1 to 160.")
        out[key] = raw
    return out


def _json_obj(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value)


def _system(row: Any) -> dict[str, Any]:
    updated = row["updated_at"]
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "colors": _json_obj(row["colors"]),
        "type": _json_obj(row["type_roles"]),
        "spacing": {key: int(step) for key, step in _json_obj(row["spacing"]).items()},
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else str(updated),
    }


def _lock_workspace(connection: Any, org_id: str, workspace_id: str) -> None:
    row = connection.execute(text(
        "SELECT id FROM workspaces WHERE id = :w AND org_id = :o FOR UPDATE"),
        {"w": workspace_id, "o": org_id}).first()
    if row is None:
        raise _missing()


def read_system(connection: Any, *, org_id: str, workspace_id: str) -> dict[str, Any] | None:
    """The system this org may see. None when it is unset or the workspace is not theirs."""
    row = connection.execute(text(
        """
        SELECT s.id, s.org_id, s.workspace_id, s.name, s.colors, s.type_roles,
               s.spacing, s.updated_at
        FROM workspace_design_systems s
        JOIN workspaces w ON w.id = s.workspace_id AND w.org_id = s.org_id
        WHERE s.workspace_id = :w AND s.org_id = :o
        """), {"w": workspace_id, "o": org_id}).mappings().first()
    if row is None:
        return None
    return _system(row)


def save_system(
    connection: Any, *, org_id: str, workspace_id: str, name: str,
    colors: dict, type: dict, spacing: dict,
) -> dict[str, Any]:
    """Save the workspace's one design system. A workspace outside the org is not_found."""
    _lock_workspace(connection, org_id, workspace_id)
    saved_name = _name(name, what="design system")
    saved_colors = _colors(colors)
    saved_type = _type_roles(type)
    saved_spacing = _spacing(spacing)
    existing = connection.execute(text(
        "SELECT id FROM workspace_design_systems WHERE workspace_id = :w AND org_id = :o"),
        {"w": workspace_id, "o": org_id}).scalar()
    payload = {
        "o": org_id,
        "w": workspace_id,
        "name": saved_name,
        "colors": json.dumps(saved_colors),
        "type": json.dumps(saved_type),
        "spacing": json.dumps(saved_spacing),
    }
    if existing:
        connection.execute(text(
            """
            UPDATE workspace_design_systems
            SET name = :name, colors = CAST(:colors AS JSONB),
                type_roles = CAST(:type AS JSONB), spacing = CAST(:spacing AS JSONB),
                updated_at = now()
            WHERE id = :id AND org_id = :o
            """), {**payload, "id": existing})
    else:
        connection.execute(text(
            """
            INSERT INTO workspace_design_systems
              (id, org_id, workspace_id, name, colors, type_roles, spacing)
            VALUES
              (:id, :o, :w, :name, CAST(:colors AS JSONB), CAST(:type AS JSONB),
               CAST(:spacing AS JSONB))
            """), {**payload, "id": make_id("wds")})
    saved = read_system(connection, org_id=org_id, workspace_id=workspace_id)
    if saved is None:
        raise _missing()
    return saved


def create_app(connection: Any, *, org_id: str, workspace_id: str, name: str) -> dict[str, Any]:
    """Create an app in the workspace. It reads the current design system and keeps that spec."""
    _lock_workspace(connection, org_id, workspace_id)
    app_name = _name(name, what="app")
    system = read_system(connection, org_id=org_id, workspace_id=workspace_id)
    if system is None:
        raise DesignSystemError(
            409, "design_system_missing",
            "Save a design system before a new app can read one.")
    app_id = make_id("wda")
    read = {
        "id": system["id"],
        "name": system["name"],
        "colors": system["colors"],
        "type": system["type"],
        "spacing": system["spacing"],
    }
    connection.execute(text(
        """
        INSERT INTO workspace_design_apps
          (id, org_id, workspace_id, name, design_system_id,
           read_name, read_colors, read_type, read_spacing)
        VALUES
          (:id, :o, :w, :name, :system,
           :read_name, CAST(:colors AS JSONB), CAST(:type AS JSONB), CAST(:spacing AS JSONB))
        """), {
            "id": app_id, "o": org_id, "w": workspace_id, "name": app_name,
            "system": system["id"], "read_name": read["name"],
            "colors": json.dumps(read["colors"]), "type": json.dumps(read["type"]),
            "spacing": json.dumps(read["spacing"]),
        })
    return {
        "id": app_id,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "name": app_name,
        "design_system": read,
    }
