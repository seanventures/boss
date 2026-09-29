"""Community hardware places.

The default place is the machine where the community installer ran. One
hardware agent (`scripts/install/node_agent.sh`) is installed on every
BOSS-connected machine, including that local one. A place is ready only when
`agent_state` is `installed`. Nothing here opens a socket, an SSH session, or
a provider API.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.core import emit_event, make_id


AGENT_INSTALL_SCRIPT = "scripts/install/node_agent.sh"
_PERMISSION = "infra_provision"
_PROVIDER_KEYS = frozenset({"aws", "digitalocean", "generic"})
_AGENT_STATES = frozenset({"absent", "installed"})
_RETIRE_ACTIONS = frozenset({"retire", "delete", "destroy", "remove"})
_FORBIDDEN_LABEL_KEYS = frozenset({
    "secret",
    "secret_ref",
    "token",
    "password",
    "api_key",
    "private_key",
    "access_key",
    "client_secret",
    "enroll_token_hash",
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
_RAW_SECRET_FIELDS = frozenset({
    "secret",
    "api_key",
    "api_token",
    "token",
    "password",
    "access_key",
    "secret_key",
    "private_key",
    "credentials",
    "aws_secret_access_key",
    "client_secret",
    "session_token",
})

# Same table the kernel declares. agent_state and the one-time enroll hash live
# in labels so this route module does not add a second migration.
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS hardware_places (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  provider_key TEXT,
  status TEXT NOT NULL,
  agent_state TEXT NOT NULL DEFAULT 'absent',
  is_default BOOLEAN NOT NULL DEFAULT false,
  endpoint TEXT,
  labels JSONB NOT NULL DEFAULT '{}'::jsonb,
  secret_ref TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT hardware_places_kind CHECK (kind IN ('local', 'agent', 'provider')),
  CONSTRAINT hardware_places_status CHECK (status IN ('ready', 'pending', 'retired')),
  CONSTRAINT hardware_places_agent_state CHECK (agent_state IN ('absent', 'installed')),
  CONSTRAINT hardware_places_ready_needs_agent CHECK (
    status <> 'ready' OR agent_state = 'installed'
  ),
  CONSTRAINT hardware_places_provider_key_value CHECK (
    provider_key IS NULL OR provider_key IN ('aws', 'digitalocean', 'generic')
  ),
  CONSTRAINT hardware_places_provider_key CHECK (
    (kind = 'provider') = (provider_key IS NOT NULL)
  ),
  CONSTRAINT hardware_places_local_is_default CHECK ((kind = 'local') = is_default),
  CONSTRAINT hardware_places_local_stays CHECK (kind <> 'local' OR status <> 'retired'),
  CONSTRAINT hardware_places_secret_ref CHECK (
    (kind = 'provider') = (secret_ref IS NOT NULL)
  )
)
"""
_CREATE_LOCAL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_hardware_places_one_local
  ON hardware_places (project_id) WHERE kind = 'local'
"""
_CREATE_PROJECT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_hardware_places_project
  ON hardware_places (project_id, kind, status)
"""

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
_LIST = text(
    """
    SELECT * FROM hardware_places
    WHERE project_id = :project_id
    ORDER BY is_default DESC, created_at ASC, id ASC
    """
)

router = APIRouter(prefix="/v1/hardware", tags=["hardware"])
_require_actor: Callable[..., None] | None = None
_require_reader: Callable[..., None] | None = None


class PlaceNotFound(LookupError):
    """No place with this id exists in the project."""


class PlaceRejected(ValueError):
    """The place change breaks a hardware-place rule."""


class EnrollRejected(Exception):
    """The enroll token is unknown or already used."""


class PlaceBody(BaseModel):
    id: str
    project_id: str
    kind: str
    name: str
    status: str
    is_default: bool
    agent_state: str
    ready: bool
    installer_machine: bool
    hostname: str | None = None
    arch: str | None = None
    os: str | None = None
    provider: str | None = None
    secret_ref: str | None = None
    endpoint: str | None = None
    labels: dict[str, Any]


class PlaceListResponse(BaseModel):
    project_id: str
    default_place_id: str
    places: list[PlaceBody]


class ResolveResponse(BaseModel):
    project_id: str
    place_id: str
    default_place_id: str
    place: PlaceBody


class LocalPlaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    hostname: str = Field(min_length=1)
    arch: str = Field(min_length=1)
    os: str = Field(min_length=1)
    agent_state: str | None = None

    @field_validator("agent_state")
    @classmethod
    def _agent_state(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _agent_state(value)


class AgentPlaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    name: str = Field(min_length=1)


class AgentPlaceResponse(BaseModel):
    place: PlaceBody
    enroll_command: str


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1)


class ProviderPlaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    secret_ref: str = Field(min_length=1)
    name: str | None = None

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        return _provider_key(value)

    @field_validator("secret_ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        return _secret_ref(value)


class ProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    title: str | None = None
    summary: str | None = None
    operations: list[dict[str, Any]] | None = None


class InstallProposal(BaseModel):
    script: str
    description: str
    applied: bool


class ChangesetProposal(BaseModel):
    id: str
    status: str
    applied_at: str | None = None
    operations: list[dict[str, Any]]


class ProposeResponse(BaseModel):
    applied: bool
    ready: bool
    place: PlaceBody
    install: InstallProposal
    changeset: ChangesetProposal


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


def _agent_state(value: Any) -> str:
    cleaned = _require_text(value, "agent_state", max_len=32).lower()
    if cleaned not in _AGENT_STATES:
        raise PlaceRejected("agent_state must be absent or installed")
    return cleaned


def _provider_key(value: Any) -> str:
    cleaned = _require_text(value, "provider", max_len=64).lower()
    if cleaned not in _PROVIDER_KEYS:
        raise PlaceRejected("provider must be aws, digitalocean, or generic")
    return cleaned


def _secret_ref(value: Any) -> str:
    cleaned = _require_text(value, "secret_ref", max_len=240)
    if any(ch.isspace() for ch in cleaned) or _RAW_SECRET_RE.search(cleaned):
        raise PlaceRejected("secret_ref must be a secret reference, not a raw secret")
    if _SECRET_REF_RE.fullmatch(cleaned) or _SECRET_REF_ID_RE.fullmatch(cleaned):
        return cleaned
    raise PlaceRejected("secret_ref must be a secret reference, not a raw secret")


def _labels_json(labels: dict[str, Any]) -> str:
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


def _ensure_table(connection: Any) -> None:
    connection.execute(text(_CREATE_TABLE))
    connection.execute(text(_CREATE_LOCAL_INDEX))
    connection.execute(text(_CREATE_PROJECT_INDEX))


def _as_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    labels = data.get("labels")
    if isinstance(labels, str):
        data["labels"] = json.loads(labels)
    elif labels is None:
        data["labels"] = {}
    else:
        data["labels"] = dict(labels)
    return data


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _stored_agent_state(row: dict[str, Any]) -> str:
    column = row.get("agent_state")
    if column in _AGENT_STATES:
        return str(column)
    raw = (row.get("labels") or {}).get("agent_state") or "absent"
    if raw not in _AGENT_STATES:
        return "absent"
    return str(raw)


def present_place(row: dict[str, Any]) -> dict[str, Any]:
    """Public place. The enroll hash is never returned."""
    data = _as_dict(row) if "labels" not in row or not isinstance(row.get("labels"), dict) else row
    if "kind" not in data:
        data = _as_dict(row)
    labels = dict(data.get("labels") or {})
    labels.pop("enroll_token_hash", None)
    agent_state = _stored_agent_state(data)
    if agent_state not in _AGENT_STATES:
        agent_state = "absent"
    labels["agent_state"] = agent_state
    status = str(data.get("status") or "pending")
    if status == "ready" and agent_state != "installed":
        status = "pending"
    kind = str(data["kind"])
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "kind": kind,
        "name": data["name"],
        "status": status,
        "is_default": bool(data["is_default"]),
        "agent_state": agent_state,
        "ready": status == "ready" and agent_state == "installed",
        "installer_machine": kind == "local",
        "hostname": labels.get("hostname"),
        "arch": labels.get("arch"),
        "os": labels.get("os_name"),
        "provider": data.get("provider_key") or data.get("provider"),
        "secret_ref": data.get("secret_ref"),
        "endpoint": data.get("endpoint"),
        "labels": labels,
    }


def _one(connection: Any, statement: Any, params: dict[str, Any]) -> dict[str, Any]:
    row = connection.execute(statement, params).mappings().first()
    if row is None:
        raise PlaceNotFound("place not found")
    return _as_dict(row)


def _insert_place(
    connection: Any,
    *,
    project_id: str,
    kind: str,
    name: str,
    status: str,
    is_default: bool,
    provider_key: str | None,
    secret_ref: str | None,
    labels: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    return _one(
        connection,
        text(
            """
            INSERT INTO hardware_places (
              id, project_id, kind, name, provider_key, status, agent_state, is_default,
              endpoint, labels, secret_ref, created_by
            ) VALUES (
              :id, :project_id, :kind, :name, :provider_key, :status, :agent_state, :is_default,
              NULL, CAST(:labels AS JSONB), :secret_ref, :actor
            )
            RETURNING *
            """
        ),
        {
            "id": make_id("hp"),
            "project_id": project_id,
            "kind": kind,
            "name": name,
            "provider_key": provider_key,
            "status": status,
            "agent_state": labels.get("agent_state") if labels.get("agent_state") in _AGENT_STATES else "absent",
            "is_default": is_default,
            "labels": _labels_json(labels),
            "secret_ref": secret_ref,
            "actor": actor,
        },
    )


def _ensure_installer_present(connection: Any, project_id: str, *, actor: str = "installer") -> dict[str, Any]:
    """Create the local installer row when missing. Does not record machine facts."""
    _ensure_table(connection)
    project = _require_id(project_id, "project_id")
    existing = connection.execute(_LOCAL, {"project_id": project}).mappings().first()
    if existing is not None:
        return _as_dict(existing)
    return _insert_place(
        connection,
        project_id=project,
        kind="local",
        name="local",
        status="pending",
        is_default=True,
        provider_key=None,
        secret_ref=None,
        labels={"agent_state": "absent", "role": "installer"},
        actor=_require_text(actor, "actor", max_len=200),
    )


def _load_services() -> Any | None:
    try:
        from boss_engine.services import hardware_manager as manager
    except ImportError:
        return None
    required = (
        "ensure_local_place",
        "list_places",
        "resolve_place",
        "register_agent_place",
        "register_provider_place",
        "mark_place_ready",
    )
    if all(callable(getattr(manager, name, None)) for name in required):
        return manager
    return None


_MANAGER = _load_services()


def ensure_local_place(
    connection: Any,
    *,
    project_id: str,
    hostname: str,
    arch: str,
    os_name: str,
    actor: str,
    agent_state: str | None = None,
) -> dict[str, Any]:
    """Record the installer machine. A second call updates the same default row."""
    if _MANAGER is not None and agent_state is None:
        return _MANAGER.ensure_local_place(
            connection,
            project_id=project_id,
            hostname=hostname,
            arch=arch,
            os_name=os_name,
            actor=actor,
        )
    project = _require_id(project_id, "project_id")
    host = _require_text(hostname, "hostname", max_len=255)
    machine_arch = _require_text(arch, "arch", max_len=64)
    machine_os = _require_text(os_name, "os_name", max_len=64)
    who = _require_text(actor, "actor", max_len=200)
    state = _agent_state(agent_state) if agent_state is not None else None
    _ensure_table(connection)
    existing = connection.execute(_LOCAL, {"project_id": project}).mappings().first()
    current = _as_dict(existing) if existing is not None else None
    kept_state = state or (_stored_agent_state(current) if current else "absent")
    previous = current["status"] if current else None
    status = "ready" if kept_state == "installed" else "pending"
    if previous == "retired":
        raise PlaceRejected("the local installer place cannot be retired")
    if kept_state != "installed":
        status = "pending"
    labels = {
        "hostname": host,
        "arch": machine_arch,
        "os_name": machine_os,
        "agent_state": kept_state,
        "role": "installer",
    }
    if current is None:
        row = _insert_place(
            connection,
            project_id=project,
            kind="local",
            name=host,
            status=status,
            is_default=True,
            provider_key=None,
            secret_ref=None,
            labels=labels,
            actor=who,
        )
    else:
        row = _one(
            connection,
            text(
                """
                UPDATE hardware_places
                SET name = :hostname,
                    status = :status,
                    agent_state = :agent_state,
                    labels = labels || CAST(:labels AS JSONB),
                    updated_at = now()
                WHERE project_id = :project_id AND kind = 'local'
                RETURNING *
                """
            ),
            {
                "hostname": host,
                "status": status,
                "agent_state": kept_state,
                "labels": _labels_json(labels),
                "project_id": project,
            },
        )
    emit_event(
        connection,
        project,
        "hardware.place.recorded",
        who,
        "hardware_place",
        row["id"],
        {"kind": "local", "agent_state": kept_state, "status": row["status"]},
    )
    return row


def list_places(connection: Any, project_id: str) -> list[dict[str, Any]]:
    """Return places for one project. The local installer machine is included."""
    project = _require_id(project_id, "project_id")
    _ensure_installer_present(connection, project)
    if _MANAGER is not None:
        rows = _MANAGER.list_places(connection, project)
        return [_as_dict(row) if not isinstance(row, dict) else _as_dict(row) for row in rows]
    rows = connection.execute(_LIST, {"project_id": project}).mappings().all()
    return [_as_dict(row) for row in rows]


def resolve_place(connection: Any, project_id: str, place_id: str | None) -> dict[str, Any]:
    """Return one place. A missing place id is the local installer machine."""
    project = _require_id(project_id, "project_id")
    if place_id is None or not str(place_id).strip():
        return _ensure_installer_present(connection, project)
    _ensure_table(connection)
    if _MANAGER is not None:
        try:
            found = _MANAGER.resolve_place(connection, project, str(place_id).strip())
        except LookupError as exc:
            raise PlaceNotFound("place not found") from exc
        return _as_dict(found) if not isinstance(found, dict) else _as_dict(found)
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
    """Record other hardware. Same agent, pending, and not the default."""
    project = _require_id(project_id, "project_id")
    who = _require_text(actor, "actor", max_len=200)
    machine = _require_text(name, "name", max_len=200)
    merged = dict(labels or {})
    merged["agent_state"] = "absent"
    _ensure_table(connection)
    _ensure_installer_present(connection, project)
    if _MANAGER is not None:
        row = _MANAGER.register_agent_place(
            connection, project_id=project, name=machine, actor=who, labels=merged,
        )
        return _as_dict(row)
    row = _insert_place(
        connection,
        project_id=project,
        kind="agent",
        name=machine,
        status="pending",
        is_default=False,
        provider_key=None,
        secret_ref=None,
        labels=merged,
        actor=who,
    )
    emit_event(
        connection,
        project,
        "hardware.place.agent_registered",
        who,
        "hardware_place",
        row["id"],
        {"kind": "agent", "agent_state": "absent", "is_default": False},
    )
    return row


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
    """Record a provider place. Stores a secret reference and does not call the provider."""
    project = _require_id(project_id, "project_id")
    who = _require_text(actor, "actor", max_len=200)
    provider = _provider_key(provider_key)
    ref = _secret_ref(secret_ref)
    machine = _require_text(name, "name", max_len=200)
    merged = dict(labels or {})
    merged.setdefault("agent_state", "absent")
    _ensure_table(connection)
    if _MANAGER is not None:
        row = _MANAGER.register_provider_place(
            connection,
            project_id=project,
            name=machine,
            provider_key=provider,
            actor=who,
            secret_ref=ref,
            labels=merged,
        )
        return _as_dict(row)
    row = _insert_place(
        connection,
        project_id=project,
        kind="provider",
        name=machine,
        status="pending",
        is_default=False,
        provider_key=provider,
        secret_ref=ref,
        labels=merged,
        actor=who,
    )
    emit_event(
        connection,
        project,
        "hardware.place.provider_registered",
        who,
        "hardware_place",
        row["id"],
        {"kind": "provider", "provider": provider, "agent_state": merged["agent_state"]},
    )
    return row


def mark_place_ready(
    connection: Any,
    *,
    place_id: str,
    endpoint: str | None = None,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a place ready only when its agent is installed. Does not change the default."""
    place = _require_id(place_id, "place_id")
    _ensure_table(connection)
    current = connection.execute(
        text("SELECT * FROM hardware_places WHERE id = :id"),
        {"id": place},
    ).mappings().first()
    if current is None:
        raise PlaceNotFound("place not found")
    row = _as_dict(current)
    if _stored_agent_state(row) != "installed":
        raise PlaceRejected("ready is only when the hardware agent is installed")
    if row["kind"] == "local" and row["status"] == "retired":
        raise PlaceRejected("the local installer place cannot be retired")
    merged = dict(row["labels"])
    if labels:
        merged.update(labels)
    merged["agent_state"] = "installed"
    updated = _one(
        connection,
        text(
            """
            UPDATE hardware_places
            SET status = 'ready',
                agent_state = 'installed',
                endpoint = COALESCE(:endpoint, endpoint),
                labels = labels || CAST(:labels AS JSONB),
                updated_at = now()
            WHERE id = :id
            RETURNING *
            """
        ),
        {
            "id": place,
            "endpoint": endpoint,
            "labels": _labels_json(merged),
        },
    )
    if updated["is_default"] != row["is_default"]:
        raise PlaceRejected("marking a place ready must not change the default")
    return updated


def issue_agent_enrollment(
    connection: Any, *, project_id: str, name: str, actor: str,
) -> dict[str, Any]:
    """Create a pending agent place and a one-time enroll token. The token is not stored."""
    row = register_agent_place(connection, project_id=project_id, name=name, actor=actor)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    stored = _one(
        connection,
        text(
            """
            UPDATE hardware_places
            SET labels = labels || CAST(:labels AS JSONB),
                updated_at = now()
            WHERE id = :id AND project_id = :project_id AND kind = 'agent'
            RETURNING *
            """
        ),
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "labels": json.dumps({"enroll_token_hash": digest}),
        },
    )
    return {"place": stored, "enroll_token": token}


def enroll_agent(connection: Any, token: str) -> dict[str, Any]:
    """Single-use call-home. Marks that other machine's agent installed."""
    cleaned = _require_text(token, "token", max_len=500)
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    _ensure_table(connection)
    row = connection.execute(
        text(
            """
            UPDATE hardware_places
            SET status = 'ready',
                agent_state = 'installed',
                labels = (labels - 'enroll_token_hash')
                    || CAST(:installed AS JSONB),
                updated_at = now()
            WHERE kind = 'agent'
              AND is_default = false
              AND labels->>'enroll_token_hash' = :digest
            RETURNING *
            """
        ),
        {
            "digest": digest,
            "installed": json.dumps({"agent_state": "installed"}),
        },
    ).mappings().first()
    if row is None:
        raise EnrollRejected("invalid enroll token")
    place = _as_dict(row)
    if place["is_default"] or place["kind"] == "local":
        raise PlaceRejected("enrollment must not change the local default")
    emit_event(
        connection,
        place["project_id"],
        "hardware.agent.enrolled",
        "node-agent",
        "hardware_place",
        place["id"],
        {"agent_state": "installed", "is_default": False},
    )
    return place


def _install_operation(place_id: str) -> dict[str, Any]:
    return {
        "action": "install",
        "script": AGENT_INSTALL_SCRIPT,
        "resource_type": "hardware_place",
        "place_id": place_id,
        "apply": False,
    }


def _proposal_operations(
    place: dict[str, Any], operations: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    proposed: list[dict[str, Any]] = []
    for operation in operations or []:
        if not isinstance(operation, dict):
            raise PlaceRejected("operations must be objects")
        action = str(operation.get("action") or "").strip().lower()
        retires = action in _RETIRE_ACTIONS or operation.get("retire") is True
        if retires and place["kind"] == "local":
            raise PlaceRejected("the local installer place cannot be retired")
        if retires:
            raise PlaceRejected("propose does not apply a retire")
        copied = dict(operation)
        copied["apply"] = False
        proposed.append(copied)
    if not any(item.get("script") == AGENT_INSTALL_SCRIPT for item in proposed):
        proposed.append(_install_operation(place["id"]))
    return proposed


def propose_place(
    connection: Any,
    *,
    project_id: str,
    place_id: str,
    actor: str,
    title: str | None = None,
    summary: str | None = None,
    operations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Propose installing the hardware agent. Does not apply the change or mark ready."""
    project = _require_id(project_id, "project_id")
    place = _require_id(place_id, "place_id")
    who = _require_text(actor, "actor", max_len=200)
    current = resolve_place(connection, project, place)
    if current["kind"] == "local" and current["status"] == "retired":
        raise PlaceRejected("the local installer place cannot be retired")
    ops = _proposal_operations(current, operations)
    changeset_id = make_id("chg")
    proposal_title = (title or "Install hardware agent").strip() or "Install hardware agent"
    proposal_summary = (
        summary
        or f"Proposal to install {AGENT_INSTALL_SCRIPT} on this machine. Not applied."
    ).strip()
    connection.execute(
        text(
            """
            INSERT INTO changesets (
              id, project_id, provider_account_id, title, summary, status,
              risk_tier, operations, expected_cost_usd_micros, cost_quote,
              reservation_id, rollback_plan, expected_receipts, policy_result,
              created_by
            ) VALUES (
              :id, :project_id, NULL, :title, :summary, 'pending_approval',
              2, CAST(:operations AS JSONB), 0, CAST(:cost_quote AS JSONB),
              NULL, CAST(:rollback_plan AS JSONB), CAST(:expected_receipts AS JSONB),
              CAST(:policy_result AS JSONB), :actor
            )
            """
        ),
        {
            "id": changeset_id,
            "project_id": project,
            "title": proposal_title,
            "summary": proposal_summary,
            "operations": json.dumps(ops),
            "cost_quote": json.dumps({"applied": False, "usd_micros": 0}),
            "rollback_plan": json.dumps([]),
            "expected_receipts": json.dumps([]),
            "policy_result": json.dumps({
                "allowed": True,
                "applied": False,
                "requires_approval": True,
                "script": AGENT_INSTALL_SCRIPT,
            }),
            "actor": who,
        },
    )
    fresh = _one(connection, _BY_ID, {"project_id": project, "id": current["id"]})
    if fresh["status"] != current["status"] or _stored_agent_state(fresh) != _stored_agent_state(current):
        raise PlaceRejected("propose must not mark a place ready or change its agent")
    emit_event(
        connection,
        project,
        "hardware.place.proposed",
        who,
        "hardware_place",
        fresh["id"],
        {
            "changeset_id": changeset_id,
            "script": AGENT_INSTALL_SCRIPT,
            "applied": False,
            "status": fresh["status"],
        },
    )
    return {"place": fresh, "changeset_id": changeset_id, "operations": ops}


def _default_place_id(places: list[dict[str, Any]]) -> str:
    for place in places:
        if place["kind"] == "local" or place["is_default"]:
            return str(place["id"])
    raise PlaceNotFound("local installer place is missing")


def _authorize(connection: Any, project_id: str, actor: str | None) -> str:
    who = str(actor or "").strip()
    if not who:
        raise HTTPException(status_code=422, detail="actor is required")
    if _require_actor is None:
        raise HTTPException(status_code=500, detail="hardware actor check is not configured")
    _require_actor(connection, project_id, who, _PERMISSION)
    return who


def _read(connection: Any, project_id: str) -> None:
    if _require_reader is not None:
        _require_reader(connection, project_id)


def _run(fn: Callable[[Any], Any]) -> Any:
    try:
        with begin(get_engine(get_settings())) as connection:
            return fn(connection)
    except HTTPException:
        raise
    except EnrollRejected as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except PlaceNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PlaceRejected as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        message = str(getattr(error, "orig", error)).lower()
        if "foreign key" in message or "projects" in message:
            raise HTTPException(status_code=404, detail="Project not found.") from error
        raise


def _reject_raw_secret_fields(fields: dict[str, Any]) -> None:
    leaked = sorted(key for key in fields if str(key).lower() in _RAW_SECRET_FIELDS)
    if leaked:
        raise HTTPException(
            status_code=422,
            detail="Raw secrets are not accepted; send secret_ref only.",
        )


@router.get("/places", response_model=PlaceListResponse)
def get_places(project_id: str = Query(min_length=1)) -> dict[str, Any]:
    def run(connection: Any) -> dict[str, Any]:
        project = _require_id(project_id, "project_id")
        _read(connection, project)
        rows = list_places(connection, project)
        places = [present_place(row) for row in rows]
        return {
            "project_id": project,
            "default_place_id": _default_place_id(places),
            "places": places,
        }

    return _run(run)


@router.post("/places/local", response_model=ResolveResponse)
def post_local_place(payload: LocalPlaceRequest) -> dict[str, Any]:
    def run(connection: Any) -> dict[str, Any]:
        who = _authorize(connection, payload.project_id, payload.actor)
        row = ensure_local_place(
            connection,
            project_id=payload.project_id,
            hostname=payload.hostname,
            arch=payload.arch,
            os_name=payload.os,
            actor=who,
            agent_state=payload.agent_state,
        )
        place = present_place(row)
        return {
            "project_id": place["project_id"],
            "place_id": place["id"],
            "default_place_id": place["id"],
            "place": place,
        }

    return _run(run)


@router.post("/places/agents", response_model=AgentPlaceResponse)
def post_agent_place(payload: AgentPlaceRequest) -> dict[str, Any]:
    def run(connection: Any) -> dict[str, Any]:
        who = _authorize(connection, payload.project_id, payload.actor)
        issued = issue_agent_enrollment(
            connection, project_id=payload.project_id, name=payload.name, actor=who,
        )
        token = str(issued["enroll_token"])
        place = present_place(issued["place"])
        if token and token in json.dumps(place, sort_keys=True):
            raise PlaceRejected("enroll token must be returned only in the command")
        command = f"{AGENT_INSTALL_SCRIPT} --enroll --token {token}"
        if command.count(token) != 1:
            raise PlaceRejected("enroll token must appear once")
        return {"place": place, "enroll_command": command}

    return _run(run)


@router.post("/agents/enroll", response_model=ResolveResponse)
def post_enroll(payload: EnrollRequest) -> dict[str, Any]:
    """Unauthenticated node call-home. The body token is the credential."""

    def run(connection: Any) -> dict[str, Any]:
        place = present_place(enroll_agent(connection, payload.token))
        return {
            "project_id": place["project_id"],
            "place_id": place["id"],
            "default_place_id": _default_place_id(
                [present_place(row) for row in list_places(connection, place["project_id"])]
            ),
            "place": place,
        }

    return _run(run)


@router.post("/places/providers", response_model=ResolveResponse)
def post_provider_place(payload: ProviderPlaceRequest) -> dict[str, Any]:
    _reject_raw_secret_fields(payload.model_dump())

    def run(connection: Any) -> dict[str, Any]:
        who = _authorize(connection, payload.project_id, payload.actor)
        row = register_provider_place(
            connection,
            project_id=payload.project_id,
            name=(payload.name or payload.provider).strip(),
            provider_key=payload.provider,
            actor=who,
            secret_ref=payload.secret_ref,
        )
        place = present_place(row)
        local = present_place(_ensure_installer_present(connection, place["project_id"]))
        return {
            "project_id": place["project_id"],
            "place_id": place["id"],
            "default_place_id": local["id"],
            "place": place,
        }

    return _run(run)


@router.post("/places/{place_id}/propose", response_model=ProposeResponse)
def post_propose(place_id: str, payload: ProposeRequest) -> dict[str, Any]:
    def run(connection: Any) -> dict[str, Any]:
        who = _authorize(connection, payload.project_id, payload.actor)
        result = propose_place(
            connection,
            project_id=payload.project_id,
            place_id=place_id,
            actor=who,
            title=payload.title,
            summary=payload.summary,
            operations=payload.operations,
        )
        place = present_place(result["place"])
        if place["ready"] and place["agent_state"] != "installed":
            raise PlaceRejected("propose must not mark a place ready")
        return {
            "applied": False,
            "ready": place["ready"],
            "place": place,
            "install": {
                "script": AGENT_INSTALL_SCRIPT,
                "description": "Install the hardware agent on the proposed machine",
                "applied": False,
            },
            "changeset": {
                "id": result["changeset_id"],
                "status": "pending_approval",
                "applied_at": None,
                "operations": result["operations"],
            },
        }

    return _run(run)


@router.get("/resolve", response_model=ResolveResponse)
def get_resolve(
    project_id: str = Query(min_length=1),
    place_id: str | None = Query(default=None),
) -> dict[str, Any]:
    def run(connection: Any) -> dict[str, Any]:
        project = _require_id(project_id, "project_id")
        _read(connection, project)
        selected = resolve_place(connection, project, place_id)
        local = _ensure_installer_present(connection, project)
        place = present_place(selected)
        return {
            "project_id": project,
            "place_id": place["id"],
            "default_place_id": local["id"],
            "place": place,
        }

    return _run(run)


def mount_hardware_routes(
    app: FastAPI,
    require_actor: Callable[..., None],
    require_reader: Callable[..., None] | None = None,
) -> None:
    """Mount community hardware routes. Caller supplies the project actor check."""
    global _require_actor, _require_reader
    _require_actor = require_actor
    _require_reader = require_reader
    app.include_router(router)
