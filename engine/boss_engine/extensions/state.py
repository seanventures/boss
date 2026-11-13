"""state.read / state.write: each extension's own namespaced key-value store (extension_state).

THE NAMESPACE IS BOSS'S, NOT THE EXTENSION'S. Every row is keyed (extension_id, project_id, key)
and the broker fills extension_id from the installed extension it spawned, never from anything the
child sends; a child names only a key. So one extension cannot read or overwrite another's data,
whatever key it asks for. project_id is the call's project ('' for install-wide calls, such as a
scheduled job).

Values are JSON, capped at the manifest's spec.state.maxValueBytes (at most 64 KiB, which the
table also enforces), and a namespace holds at most spec.state.maxKeys keys.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text

#: The table's own ceiling (db/schema.sql CHECK); the manifest may only set a lower cap.
HARD_VALUE_CAP = 64 * 1024
KEY = r"^[A-Za-z0-9_.:/-]{1,200}$"


class StateError(ValueError):
    pass


def _key(key: Any) -> str:
    k = str(key or "")
    if not re.fullmatch(KEY, k):
        raise StateError("a state key is 1-200 characters of letters, digits and _ . : / -")
    return k


def _scope(project_id: Optional[str]) -> str:
    return str(project_id or "")


def get(connection: Any, extension_id: str, project_id: Optional[str], key: str) -> tuple[bool, Any]:
    row = connection.execute(text(
        "SELECT value FROM extension_state WHERE extension_id = :e AND project_id = :p AND key = :k"),
        {"e": extension_id, "p": _scope(project_id), "k": _key(key)}).first()
    if row is None:
        return False, None
    value = row[0]
    return True, json.loads(value) if isinstance(value, str) else value


def put(connection: Any, extension_id: str, project_id: Optional[str], key: str, value: Any, *,
        max_value_bytes: int = 16 * 1024, max_keys: int = 1000) -> int:
    k = _key(key)
    encoded = json.dumps(value, separators=(",", ":"), default=str)
    size = len(encoded.encode("utf-8"))
    cap = min(int(max_value_bytes), HARD_VALUE_CAP)
    if size > cap:
        raise StateError(f"a state value is at most {cap} bytes; this one is {size}")
    scope = _scope(project_id)
    exists = connection.execute(text(
        "SELECT 1 FROM extension_state WHERE extension_id = :e AND project_id = :p AND key = :k"),
        {"e": extension_id, "p": scope, "k": k}).first()
    if exists is None:
        count = connection.execute(text(
            "SELECT count(*) FROM extension_state WHERE extension_id = :e AND project_id = :p"),
            {"e": extension_id, "p": scope}).scalar() or 0
        if int(count) >= int(max_keys):
            raise StateError(f"this extension's store is full ({max_keys} keys)")
    connection.execute(text(
        "INSERT INTO extension_state (extension_id, project_id, key, value, bytes, updated_at) "
        "VALUES (:e, :p, :k, CAST(:v AS JSONB), :b, now()) "
        "ON CONFLICT (extension_id, project_id, key) DO UPDATE SET value = EXCLUDED.value, "
        "bytes = EXCLUDED.bytes, updated_at = now()"),
        {"e": extension_id, "p": scope, "k": k, "v": encoded, "b": size})
    return size


def delete(connection: Any, extension_id: str, project_id: Optional[str], key: str) -> bool:
    res = connection.execute(text(
        "DELETE FROM extension_state WHERE extension_id = :e AND project_id = :p AND key = :k"),
        {"e": extension_id, "p": _scope(project_id), "k": _key(key)})
    return bool(res.rowcount)


def list_keys(connection: Any, extension_id: str, project_id: Optional[str], prefix: str = "",
              limit: int = 1000) -> list[str]:
    rows = connection.execute(text(
        "SELECT key FROM extension_state WHERE extension_id = :e AND project_id = :p "
        "AND key LIKE :pre ESCAPE '\\' ORDER BY key LIMIT :n"),
        {"e": extension_id, "p": _scope(project_id),
         "pre": str(prefix or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
         "n": int(limit)}).all()
    return [r[0] for r in rows]


def usage(connection: Any, extension_id: str) -> dict[str, int]:
    row = connection.execute(text(
        "SELECT count(*), COALESCE(sum(bytes), 0) FROM extension_state WHERE extension_id = :e"),
        {"e": extension_id}).first()
    return {"keys": int(row[0]), "bytes": int(row[1])}
