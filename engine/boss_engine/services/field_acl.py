"""Who can read or change each field of a builder's app (BOSSAPP-1268).

Opening the app as a user is the only read of the record. A field they cannot read
is left out of the payload entirely. A field they can read but not change comes back
with writable false, and a write of it is refused. The person who asked to look
(`actor`) does not add their own grants: open-as and a real open are the same call.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any, Optional

from sqlalchemy import text

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_USER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,80}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


class FieldAclError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class FieldWriteRefused(FieldAclError):
    """The same refusal for a hidden field and a read-only one, so a write cannot probe."""

    def __init__(self) -> None:
        super().__init__("not_writable", "You can't change that field.", 403)


def _new(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _project(value: object) -> str:
    project_id = str(value or "").strip()
    if not project_id or len(project_id) > 200:
        raise FieldAclError("bad_project", "Name the project.")
    return project_id


def _user(value: object) -> str:
    user_id = str(value or "").strip()
    if not _USER.fullmatch(user_id):
        raise FieldAclError("bad_subject", "Name a person.")
    return user_id


def _role(value: object) -> str:
    role = str(value or "").strip()
    if not _ROLE.fullmatch(role):
        raise FieldAclError("bad_subject", "Name a role.")
    return role


def _field_key(value: object) -> str:
    key = str(value or "").strip()
    if not _KEY.fullmatch(key):
        raise FieldAclError("bad_field", "A field key uses lower case.")
    return key


def _label(value: object) -> str:
    label = " ".join(str(value or "").split())
    if not label or len(label) > 80:
        raise FieldAclError("bad_field", "Name the field.")
    return label


def _name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if not name or len(name) > 120:
        raise FieldAclError("bad_name", "Name the app.")
    return name


def _scalar(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FieldAclError("bad_value", "That value is not a number.")
        return value
    if isinstance(value, str):
        if len(value) > 4000:
            raise FieldAclError("bad_value", "That value is too long.")
        return value
    raise FieldAclError("bad_value", "A field value must be plain text or a number.")


def _json_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _load_app(connection, app_id: str, project_id: Optional[str]) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT id, project_id, name, created_by FROM field_acl_apps WHERE id = :id"),
        {"id": app_id},
    ).mappings().first()
    if row is None or (project_id is not None and row["project_id"] != project_id):
        raise FieldAclError("not_found", "No such app.", 404)
    return dict(row)


def _fields(connection, app_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT field_key, label, position FROM field_acl_fields "
            "WHERE app_id = :id ORDER BY position, field_key"
        ),
        {"id": app_id},
    ).mappings().all()
    return [{"key": row["field_key"], "label": row["label"], "position": int(row["position"])}
            for row in rows]


def _grants(connection, app_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT g.field_key, g.subject_kind, g.subject_id, g.can_read, g.can_change "
            "FROM field_acl_grants g "
            "JOIN field_acl_fields f ON f.app_id = g.app_id AND f.field_key = g.field_key "
            "WHERE g.app_id = :id "
            "ORDER BY f.position, g.subject_kind, g.subject_id"
        ),
        {"id": app_id},
    ).mappings().all()
    return [{
        "field_key": row["field_key"],
        "subject_kind": row["subject_kind"],
        "subject_id": row["subject_id"],
        "can_read": bool(row["can_read"]),
        "can_change": bool(row["can_change"]),
    } for row in rows]


def _members(connection, app_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT user_id, roles FROM field_acl_members WHERE app_id = :id ORDER BY user_id"),
        {"id": app_id},
    ).mappings().all()
    return [{"user_id": row["user_id"], "roles": _json_list(row["roles"])} for row in rows]


def _roles_of(connection, app_id: str, user_id: str) -> list[str]:
    row = connection.execute(
        text("SELECT roles FROM field_acl_members WHERE app_id = :id AND user_id = :user"),
        {"id": app_id, "user": user_id},
    ).mappings().first()
    if row is None:
        return []
    return _json_list(row["roles"])


def _stored(connection, app_id: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT values FROM field_acl_records WHERE app_id = :id"),
        {"id": app_id},
    ).mappings().first()
    if row is None:
        return {}
    return _json_object(row["values"])


def _save_values(connection, app_id: str, values: dict[str, Any]) -> None:
    connection.execute(
        text(
            "INSERT INTO field_acl_records (app_id, values) VALUES (:id, CAST(:values AS jsonb)) "
            "ON CONFLICT (app_id) DO UPDATE "
            "SET values = CAST(:values AS jsonb), updated_at = now()"
        ),
        {"id": app_id, "values": json.dumps(values)},
    )


def builder_view(connection, app_id: str, *, project_id: Optional[str] = None) -> dict[str, Any]:
    """The desk: every field, grant, and stored value. This is not an open."""
    app = _load_app(connection, app_id, project_id)
    return {
        "id": app["id"],
        "project_id": app["project_id"],
        "name": app["name"],
        "created_by": app["created_by"],
        "fields": _fields(connection, app_id),
        "grants": _grants(connection, app_id),
        "members": _members(connection, app_id),
        "values": _stored(connection, app_id),
    }


def list_apps(connection, project_id: str) -> list[dict[str, Any]]:
    project = _project(project_id)
    rows = connection.execute(
        text("SELECT id FROM field_acl_apps WHERE project_id = :p ORDER BY created_at, id"),
        {"p": project},
    ).scalars().all()
    return [builder_view(connection, app_id) for app_id in rows]


def create_app(connection, *, project_id: str, name: str, created_by: str,
               fields: list) -> dict[str, Any]:
    project = _project(project_id)
    title = _name(name)
    author = _user(created_by)
    if not isinstance(fields, list) or not fields:
        raise FieldAclError("no_fields", "Add a field first.")
    if len(fields) > 40:
        raise FieldAclError("bad_field", "Too many fields.")
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in fields:
        if not isinstance(raw, dict):
            raise FieldAclError("bad_field", "Name the field.")
        key = _field_key(raw.get("key"))
        if key in seen:
            raise FieldAclError("duplicate_field", "That field is listed twice.")
        seen.add(key)
        parsed.append((key, _label(raw.get("label"))))
    app_id = _new("fa")
    connection.execute(
        text(
            "INSERT INTO field_acl_apps (id, project_id, name, created_by) "
            "VALUES (:id, :project, :name, :author)"
        ),
        {"id": app_id, "project": project, "name": title, "author": author},
    )
    for position, (key, label) in enumerate(parsed):
        connection.execute(
            text(
                "INSERT INTO field_acl_fields (id, app_id, field_key, label, position) "
                "VALUES (:id, :app, :key, :label, :position)"
            ),
            {"id": _new("ff"), "app": app_id, "key": key, "label": label, "position": position},
        )
    _save_values(connection, app_id, {})
    return builder_view(connection, app_id)


def set_member(connection, app_id: str, user_id: str, roles: list, *,
               project_id: Optional[str] = None) -> dict[str, Any]:
    _load_app(connection, app_id, project_id)
    person = _user(user_id)
    if not isinstance(roles, list):
        raise FieldAclError("bad_subject", "Roles are a list.")
    cleaned: list[str] = []
    seen: set[str] = set()
    for role in roles:
        token = _role(role)
        if token not in seen:
            seen.add(token)
            cleaned.append(token)
    connection.execute(
        text(
            "INSERT INTO field_acl_members (app_id, user_id, roles) "
            "VALUES (:app, :user, CAST(:roles AS jsonb)) "
            "ON CONFLICT (app_id, user_id) DO UPDATE SET roles = EXCLUDED.roles"
        ),
        {"app": app_id, "user": person, "roles": json.dumps(cleaned)},
    )
    return {"user_id": person, "roles": cleaned}


def _normalize(raw: object, field_key: Optional[str] = None) -> Optional[dict[str, Any]]:
    """A grant that allows nothing is dropped. Change forces read."""
    if not isinstance(raw, dict):
        raise FieldAclError("bad_grant", "A grant must name a person or a role.")
    key = field_key if field_key is not None else str(raw.get("field_key") or "").strip()
    if not _KEY.fullmatch(key):
        raise FieldAclError("bad_field", "A field key uses lower case.")
    kind = str(raw.get("subject_kind") or "").strip()
    if kind not in ("user", "role"):
        raise FieldAclError("bad_subject", "Subject kind must be user or role.")
    subject = _user(raw.get("subject_id")) if kind == "user" else _role(raw.get("subject_id"))
    can_change = bool(raw.get("can_change"))
    can_read = bool(raw.get("can_read")) or can_change
    if not can_read:
        return None
    return {
        "field_key": key,
        "subject_kind": kind,
        "subject_id": subject,
        "can_read": True,
        "can_change": can_change,
    }


def _reject_duplicates(grants: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for grant in grants:
        token = (grant["field_key"], grant["subject_kind"], grant["subject_id"])
        if token in seen:
            raise FieldAclError("duplicate_grant", "That person is listed twice.")
        seen.add(token)


def _require_known(grants: list[dict[str, Any]], known: set[str]) -> None:
    for grant in grants:
        if grant["field_key"] not in known:
            raise FieldAclError("unknown_field", "That field is not on this app.")


def _insert_grants(connection, app_id: str, grants: list[dict[str, Any]]) -> None:
    for grant in grants:
        connection.execute(
            text(
                "INSERT INTO field_acl_grants "
                "(id, app_id, field_key, subject_kind, subject_id, can_read, can_change) "
                "VALUES (:id, :app, :key, :kind, :subject, :read, :change)"
            ),
            {
                "id": _new("fg"), "app": app_id, "key": grant["field_key"],
                "kind": grant["subject_kind"], "subject": grant["subject_id"],
                "read": grant["can_read"], "change": grant["can_change"],
            },
        )


def set_grants(connection, app_id: str, grants: list, *,
               project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Replace every grant on the app. Invalid input writes nothing."""
    _load_app(connection, app_id, project_id)
    if not isinstance(grants, list):
        raise FieldAclError("bad_grant", "Grants are a list.")
    if len(grants) > 400:
        raise FieldAclError("bad_grant", "Too many grants.")
    normalized = [grant for raw in grants if (grant := _normalize(raw)) is not None]
    _reject_duplicates(normalized)
    _require_known(normalized, {field["key"] for field in _fields(connection, app_id)})
    connection.execute(text("DELETE FROM field_acl_grants WHERE app_id = :app"), {"app": app_id})
    _insert_grants(connection, app_id, normalized)
    return _grants(connection, app_id)


def set_field_grants(connection, app_id: str, field_key: str, grants: list) -> list[dict[str, Any]]:
    """Replace one field's grants. Other fields stay. Invalid input writes nothing."""
    _load_app(connection, app_id, None)
    key = _field_key(field_key)
    known = {field["key"] for field in _fields(connection, app_id)}
    if key not in known:
        raise FieldAclError("unknown_field", "That field is not on this app.")
    if not isinstance(grants, list):
        raise FieldAclError("bad_grant", "Grants are a list.")
    normalized = [grant for raw in grants if (grant := _normalize(raw, key)) is not None]
    _reject_duplicates(normalized)
    connection.execute(
        text("DELETE FROM field_acl_grants WHERE app_id = :app AND field_key = :key"),
        {"app": app_id, "key": key},
    )
    _insert_grants(connection, app_id, normalized)
    return [grant for grant in _grants(connection, app_id) if grant["field_key"] == key]


def _visible(fields: list[dict[str, Any]], grants: list[dict[str, Any]], user_id: str,
             roles: list[str]) -> list[tuple[str, str, bool]]:
    """Fields this user receives, in desk order. The bool is whether they can change it."""
    role_set = set(roles)
    change: dict[str, bool] = {}
    for grant in grants:
        if grant["subject_kind"] == "user" and grant["subject_id"] != user_id:
            continue
        if grant["subject_kind"] == "role" and grant["subject_id"] not in role_set:
            continue
        if not grant["can_read"]:
            continue
        change[grant["field_key"]] = change.get(grant["field_key"], False) or bool(grant["can_change"])
    return [(field["key"], field["label"], change[field["key"]])
            for field in fields if field["key"] in change]


def open_app(connection, app_id: str, *, as_user: str, actor: Optional[str] = None,
             project_id: Optional[str] = None) -> dict[str, Any]:
    """The app as `as_user` would receive it. `actor` is who asked, and is ignored."""
    del actor  # opening as a user never adds the opener's grants
    person = _user(as_user)
    app = _load_app(connection, app_id, project_id)
    visible = _visible(
        _fields(connection, app_id), _grants(connection, app_id), person,
        _roles_of(connection, app_id, person),
    )
    stored = _stored(connection, app_id)
    return {
        "app_id": app["id"],
        "project_id": app["project_id"],
        "name": app["name"],
        "as_user": person,
        "fields": [{"key": key, "label": label, "writable": writable} for key, label, writable in visible],
        "values": {key: stored.get(key) for key, _label, _writable in visible},
    }


def write_fields(connection, app_id: str, *, as_user: str, values: dict, actor: Optional[str] = None,
                 project_id: Optional[str] = None) -> dict[str, Any]:
    """Change stored values as `as_user`. One refused key rolls back the whole write."""
    del actor  # a builder opening the write as ada cannot use the builder's change right
    if not isinstance(values, dict) or not values:
        raise FieldAclError("empty_write", "Name a field to change.")
    person = _user(as_user)
    app = _load_app(connection, app_id, project_id)
    visible = _visible(
        _fields(connection, app_id), _grants(connection, app_id), person,
        _roles_of(connection, app_id, person),
    )
    allowed = {key: writable for key, _label, writable in visible}
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if not allowed.get(key):
            raise FieldWriteRefused()
        cleaned[str(key)] = _scalar(value)
    stored = _stored(connection, app_id)
    stored.update(cleaned)
    _save_values(connection, app_id, stored)
    return open_app(connection, app["id"], as_user=person, project_id=project_id)
