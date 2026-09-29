"""Places where community BOSS can install apps.

The default place is the computer the community installer ran on. One hardware
agent is installed on that machine and on every other machine BOSS connects.
A row is not ready for app installs until that agent is installed. Creating or
updating a row does not open a socket, an SSH session, or a provider API.
Additional machines and provider accounts are never the default.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text

from .core import make_id


class PlaceNotFound(LookupError):
    """No place with this id exists in the given project."""


class PlaceRejected(ValueError):
    """The place change breaks a hardware-place rule."""


_PROVIDER_KEYS = frozenset({"aws", "digitalocean", "generic"})
_FORBIDDEN_LABEL_KEYS = frozenset({
    "secret",
    "secret_ref",
    "token",
    "password",
    "api_key",
    "private_key",
    "access_key",
    "client_secret",
})
_SECRET_REF_RE = re.compile(
    r"^(?:env|vault|local_vault|local_runner|platform_secret_store|infisical|database|secret)"
    r":[A-Za-z0-9_./:@+-]{1,200}$"
)
_SECRET_REF_ID_RE = re.compile(r"^(?:sr|sec|secret)_[A-Za-z0-9]{8,128}$")
_RAW_SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN |"
    r"sk-[A-Za-z0-9]|ghp_[A-Za-z0-9]|github_pat_|xox[baprs]-|glpat-)"
)

_ENSURE_LOCAL = text(
    """
    INSERT INTO hardware_places (
      id, project_id, kind, name, provider_key, status, agent_state, is_default,
      endpoint, labels, secret_ref, created_by
    ) VALUES (
      :id, :project_id, 'local', :hostname, NULL, 'pending', 'absent', true,
      NULL, CAST(:labels AS JSONB), NULL, :actor
    )
    ON CONFLICT (project_id) WHERE kind = 'local'
    DO UPDATE SET
      name = EXCLUDED.name,
      labels = hardware_places.labels || EXCLUDED.labels,
      updated_at = now()
    RETURNING *
    """
)
_INSERT = text(
    """
    INSERT INTO hardware_places (
      id, project_id, kind, name, provider_key, status, agent_state, is_default,
      endpoint, labels, secret_ref, created_by
    ) VALUES (
      :id, :project_id, :kind, :name, :provider_key, :status, 'absent', false,
      NULL, CAST(:labels AS JSONB), :secret_ref, :actor
    )
    RETURNING *
    """
)
_LIST = text(
    """
    SELECT * FROM hardware_places
    WHERE project_id = :project_id
    ORDER BY is_default DESC, created_at ASC, id ASC
    """
)
_LOCAL = text(
    """
    SELECT * FROM hardware_places
    WHERE project_id = :project_id AND kind = 'local'
    """
)
_BY_ID = text(
    """
    SELECT * FROM hardware_places
    WHERE project_id = :project_id AND id = :id
    """
)
_BY_PLACE_ID = text("SELECT * FROM hardware_places WHERE id = :id")
_ENROLL = text(
    """
    UPDATE hardware_places
    SET agent_state = 'installed',
        updated_at = now()
    WHERE project_id = :project_id AND id = :id
    RETURNING *
    """
)
_MARK_READY = text(
    """
    UPDATE hardware_places
    SET status = 'ready',
        endpoint = COALESCE(:endpoint, endpoint),
        labels = CASE
          WHEN CAST(:merge_labels AS boolean)
            THEN labels || CAST(:labels AS JSONB)
          ELSE labels
        END,
        updated_at = now()
    WHERE id = :id AND agent_state = 'installed'
    RETURNING *
    """
)
_RETIRE = text(
    """
    UPDATE hardware_places
    SET status = 'retired',
        labels = labels || CAST(:labels AS JSONB),
        updated_at = now()
    WHERE id = :id AND project_id = :project_id AND kind <> 'local'
    RETURNING *
    """
)
_DELETE = text(
    """
    DELETE FROM hardware_places
    WHERE id = :id AND project_id = :project_id AND kind <> 'local'
    """
)


def _require_text(value: Any, field: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise PlaceRejected(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_len or any(ord(ch) < 32 for ch in cleaned):
        raise PlaceRejected(f"{field} is empty or not usable")
    return cleaned


def _require_id(value: Any, field: str) -> str:
    cleaned = _require_text(value, field, max_len=200)
    if any(ch.isspace() for ch in cleaned):
        raise PlaceRejected(f"{field} is empty or not usable")
    return cleaned


def _labels_json(labels: Any) -> str:
    if labels is None:
        labels = {}
    if not isinstance(labels, dict) or any(not isinstance(key, str) for key in labels):
        raise PlaceRejected("labels must be a JSON object with string keys")
    forbidden = _FORBIDDEN_LABEL_KEYS.intersection(labels)
    if forbidden:
        raise PlaceRejected("labels must not carry secret material")
    try:
        encoded = json.dumps(labels, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PlaceRejected("labels must be JSON serializable") from exc
    if len(encoded) > 8192:
        raise PlaceRejected("labels are too large")
    return encoded


def _secret_ref(value: Any) -> str:
    cleaned = _require_text(value, "secret_ref", max_len=240)
    if any(ch.isspace() for ch in cleaned):
        raise PlaceRejected("secret_ref must be a secret reference, not a raw secret")
    if _RAW_SECRET_RE.search(cleaned):
        raise PlaceRejected("secret_ref must be a secret reference, not a raw secret")
    if _SECRET_REF_RE.fullmatch(cleaned) or _SECRET_REF_ID_RE.fullmatch(cleaned):
        return cleaned
    raise PlaceRejected("secret_ref must be a secret reference, not a raw secret")


def _provider_key(value: Any) -> str:
    cleaned = _require_text(value, "provider_key", max_len=64).lower()
    if cleaned not in _PROVIDER_KEYS:
        raise PlaceRejected("provider_key must be aws, digitalocean, or generic")
    return cleaned


def _optional_endpoint(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = _require_text(value, "endpoint", max_len=500)
    if any(ch.isspace() for ch in cleaned):
        raise PlaceRejected("endpoint is empty or not usable")
    return cleaned


def _as_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    labels = data.get("labels")
    if isinstance(labels, str):
        data["labels"] = json.loads(labels)
    elif labels is None:
        data["labels"] = {}
    return data


def _one(connection: Any, statement: Any, params: dict[str, Any]) -> dict[str, Any]:
    row = connection.execute(statement, params).mappings().first()
    if row is None:
        raise PlaceNotFound("place not found")
    return _as_dict(row)


def ensure_local_place(
    connection: Any,
    *,
    project_id: str,
    hostname: str,
    arch: str,
    os_name: str,
    actor: str,
) -> dict[str, Any]:
    """Create this project's installer machine, or refresh its hostname in place.

    The row is the default immediately. Its hardware agent starts ``absent`` and
    its status stays ``pending`` until ``enroll_place_agent`` and
    ``mark_place_ready``. A second call does not clear an agent that is already
    installed and does not move the default. It does not contact the machine or
    a provider. The project row must already exist.
    """
    project = _require_id(project_id, "project_id")
    host = _require_text(hostname, "hostname", max_len=255)
    machine_arch = _require_text(arch, "arch", max_len=64)
    machine_os = _require_text(os_name, "os_name", max_len=64)
    who = _require_text(actor, "actor", max_len=200)
    labels = _labels_json({
        "hostname": host,
        "arch": machine_arch,
        "os_name": machine_os,
    })
    return _one(connection, _ENSURE_LOCAL, {
        "id": make_id("hp"),
        "project_id": project,
        "hostname": host,
        "labels": labels,
        "actor": who,
    })


def list_places(connection: Any, project_id: str) -> list[dict[str, Any]]:
    """Return places for one project. The local installer machine is first."""
    project = _require_id(project_id, "project_id")
    rows = connection.execute(_LIST, {"project_id": project}).mappings().all()
    return [_as_dict(row) for row in rows]


def resolve_place(
    connection: Any, project_id: str, place_id: str | None
) -> dict[str, Any]:
    """Return one project place. ``place_id is None`` selects the local default."""
    project = _require_id(project_id, "project_id")
    if place_id is None:
        return _one(connection, _LOCAL, {"project_id": project})
    return _one(connection, _BY_ID, {
        "project_id": project,
        "id": _require_id(place_id, "place_id"),
    })


def register_agent_place(
    connection: Any,
    *,
    project_id: str,
    name: str,
    actor: str,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record another machine. Same hardware agent, not the default.

    ``agent_state`` stays ``absent`` and the machine stays pending until
    ``enroll_place_agent`` marks that agent installed.
    """
    return _one(connection, _INSERT, {
        "id": make_id("hp"),
        "project_id": _require_id(project_id, "project_id"),
        "kind": "agent",
        "name": _require_text(name, "name", max_len=200),
        "provider_key": None,
        "status": "pending",
        "labels": _labels_json(labels),
        "secret_ref": None,
        "actor": _require_text(actor, "actor", max_len=200),
    })


def register_provider_place(
    connection: Any,
    *,
    project_id: str,
    name: str,
    provider_key: str,
    actor: str,
    secret_ref: str,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a provider account. Pending, not default, agent absent.

    This is an account, not a machine. A machine later created from it is
    additional hardware and gets the same agent via ``enroll_place_agent``.
    Only a secret reference is stored.
    """
    return _one(connection, _INSERT, {
        "id": make_id("hp"),
        "project_id": _require_id(project_id, "project_id"),
        "kind": "provider",
        "name": _require_text(name, "name", max_len=200),
        "provider_key": _provider_key(provider_key),
        "status": "pending",
        "labels": _labels_json(labels),
        "secret_ref": _secret_ref(secret_ref),
        "actor": _require_text(actor, "actor", max_len=200),
    })


def enroll_place_agent(
    connection: Any, *, project_id: str, place_id: str | None = None
) -> dict[str, Any]:
    """Mark the one hardware agent installed. Does not change the default or readiness.

    ``place_id is None`` selects the local installer machine. The agent program
    itself is ``scripts/install/node_agent.sh``; this function only records that
    it is installed.
    """
    project = _require_id(project_id, "project_id")
    if place_id is None:
        current = resolve_place(connection, project, None)
    else:
        current = resolve_place(connection, project, place_id)
    return _one(connection, _ENROLL, {"project_id": project, "id": current["id"]})


def mark_place_ready(
    connection: Any,
    *,
    place_id: str,
    endpoint: str | None = None,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a place ready once its hardware agent is installed.

    Refuses a row whose ``agent_state`` is still ``absent``. Never changes
    which row is the default.
    """
    place = _require_id(place_id, "place_id")
    existing = connection.execute(_BY_PLACE_ID, {"id": place}).mappings().first()
    if existing is None:
        raise PlaceNotFound("place not found")
    if existing["agent_state"] != "installed":
        raise PlaceRejected("the hardware agent is not installed on this machine")
    encoded = "{}" if labels is None else _labels_json(labels)
    updated = _one(connection, _MARK_READY, {
        "id": place,
        "endpoint": _optional_endpoint(endpoint),
        "merge_labels": labels is not None,
        "labels": encoded,
    })
    if updated["is_default"] != existing["is_default"]:
        raise PlaceRejected("mark_place_ready cannot change the default place")
    return updated


def record_community_local_place(
    connection: Any,
    *,
    project_id: str,
    hostname: str,
    arch: str,
    os_name: str,
    actor: str,
) -> dict[str, Any]:
    """Record the installer machine with its hardware agent already installed.

    The community installer calls this in one transaction after
    ``scripts/install/node_agent.sh --local``, so the committed local row is
    never left without that agent. The row stays the default.
    """
    place = ensure_local_place(
        connection,
        project_id=project_id,
        hostname=hostname,
        arch=arch,
        os_name=os_name,
        actor=actor,
    )
    enrolled = enroll_place_agent(connection, project_id=project_id, place_id=None)
    if enrolled["id"] != place["id"] or not enrolled["is_default"]:
        raise PlaceRejected("local place did not remain the default")
    return mark_place_ready(connection, place_id=enrolled["id"])


def retire_place(
    connection: Any, *, project_id: str, place_id: str, actor: str
) -> dict[str, Any]:
    """Retire an optional place. The local installer place cannot be retired."""
    project = _require_id(project_id, "project_id")
    place = _require_id(place_id, "place_id")
    who = _require_text(actor, "actor", max_len=200)
    current = _one(connection, _BY_ID, {"project_id": project, "id": place})
    if current["kind"] == "local":
        raise PlaceRejected("the local installer place cannot be retired")
    return _one(connection, _RETIRE, {
        "id": place,
        "project_id": project,
        "labels": _labels_json({"retired_by": who}),
    })


def delete_place(connection: Any, *, project_id: str, place_id: str) -> None:
    """Delete an optional place. The local installer place cannot be deleted."""
    project = _require_id(project_id, "project_id")
    place = _require_id(place_id, "place_id")
    current = _one(connection, _BY_ID, {"project_id": project, "id": place})
    if current["kind"] == "local":
        raise PlaceRejected("the local installer place cannot be deleted")
    result = connection.execute(_DELETE, {"id": place, "project_id": project})
    if result.rowcount != 1:
        raise PlaceNotFound("place not found")
