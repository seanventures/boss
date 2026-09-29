"""The one community node agent, on the local machine and on extra machines.

The default place is the computer where the community installer ran. This module
does not open a socket and does not SSH. Enroll tokens are hashed before they
are stored and are never logged. `hardware_manager` owns place rules when that
module is present; this module still records `agent_state`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import secrets
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import text


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


logger = logging.getLogger(__name__)

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "install" / "node_agent.sh"
_COMMAND_MARK = "BOSS_NODE_AGENT_SCRIPT"
_FORBIDDEN_LABEL_KEYS = frozenset({
    "secret",
    "secret_ref",
    "token",
    "password",
    "api_key",
    "private_key",
    "access_key",
    "client_secret",
    "command",
})


class PlaceNotFound(LookupError):
    """No hardware place with this id exists."""


class PlaceRejected(ValueError):
    """The place change breaks a hardware-place rule."""


class EnrollTokenError(ValueError):
    """A call-home token was wrong, reused, or would have been stored."""


def _kernel() -> Any | None:
    spec = importlib.util.find_spec("boss_engine.services.hardware_manager")
    if spec is None:
        return None
    module = importlib.import_module("boss_engine.services.hardware_manager")
    missing = [
        name
        for name in ("ensure_local_place", "register_agent_place", "mark_place_ready")
        if not hasattr(module, name)
    ]
    if missing:
        raise RuntimeError(f"hardware_manager.py is missing {missing}")
    return module


def _dialect(connection: Any) -> str:
    engine = getattr(connection, "engine", None)
    if engine is None:
        return "sqlite"
    return str(engine.dialect.name)


def _json_expr(dialect: str, name: str) -> str:
    if name not in {"labels", "dependency_report"}:
        raise ValueError(name)
    if dialect == "postgresql":
        return f"CAST(:{name} AS JSONB)"
    return f":{name}"


def _now() -> str:
    return now_utc().isoformat()


def _require_text(value: Any, field: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise PlaceRejected(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_len or any(ord(ch) < 32 for ch in cleaned):
        raise PlaceRejected(f"{field} is empty or not usable")
    return cleaned


def _labels(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PlaceRejected("labels must be a JSON object with string keys")
    forbidden = _FORBIDDEN_LABEL_KEYS.intersection(value)
    if forbidden:
        raise PlaceRejected("labels must not carry secret material")
    return dict(value)


def _dump(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PlaceRejected("value must be JSON serializable") from exc
    if len(encoded) > 8192:
        raise PlaceRejected("value is too large")
    return encoded


def _load(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    return bool(value)


def _report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlaceRejected("dependency report must be an object")
    return dict(value)


def _control_plane(value: Any) -> str:
    cleaned = _require_text(value, "control_plane_url", max_len=500)
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PlaceRejected("control plane URL must be http or https")
    if parsed.username or parsed.password:
        raise PlaceRejected("control plane URL must not carry credentials")
    return cleaned.rstrip("/")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _reject_token_echo(token: str, *parts: Any) -> None:
    if not token:
        return
    blob = json.dumps(parts, sort_keys=True, default=str)
    if token in blob:
        raise EnrollTokenError("enroll token must not be stored")


def _note(message: str) -> None:
    logger.info("%s", message)


def _place(row: Any) -> dict[str, Any]:
    data = dict(row)
    labels = _load(data.get("labels"), {})
    report = _load(data.get("dependency_report"), {})
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "kind": data["kind"],
        "name": data["name"],
        "status": data["status"],
        "is_default": _flag(data["is_default"]),
        "agent_state": data["agent_state"],
        "endpoint": data.get("endpoint"),
        "hostname": data.get("hostname") or labels.get("hostname"),
        "arch": data.get("arch") or labels.get("arch"),
        "os": data.get("os_name") or labels.get("os") or labels.get("os_name"),
        "dependency_report": report,
        "labels": labels,
        "created_by": data["created_by"],
    }


def _select_one(connection: Any, statement: str, params: dict[str, Any]) -> dict[str, Any]:
    row = connection.execute(text(statement), params).mappings().first()
    if row is None:
        raise PlaceNotFound("place not found")
    return _place(row)


def _local_row(connection: Any, project_id: str) -> Any:
    return connection.execute(
        text(
            """
            SELECT * FROM hardware_places
            WHERE project_id = :project_id AND kind = 'local'
            """
        ),
        {"project_id": project_id},
    ).mappings().first()


def _insert_place(
    connection: Any,
    *,
    project_id: str,
    kind: str,
    name: str,
    status: str,
    is_default: bool,
    agent_state: str,
    actor: str,
    labels: dict[str, Any],
    hostname: str | None = None,
    arch: str | None = None,
    os_name: str | None = None,
    endpoint: str | None = None,
    dependency_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind == "local" and not is_default:
        raise PlaceRejected("the local machine is the default place")
    if kind != "local" and is_default:
        raise PlaceRejected("additional hardware is not the default")
    if agent_state not in {"absent", "installed"}:
        raise PlaceRejected("agent_state must be absent or installed")
    place_id = make_id("hp")
    dialect = _dialect(connection)
    labels_sql = _json_expr(dialect, "labels")
    report_sql = _json_expr(dialect, "dependency_report")
    connection.execute(
        text(
            f"""
            INSERT INTO hardware_places (
              id, project_id, kind, name, provider_key, status, is_default,
              agent_state, endpoint, hostname, arch, os_name, dependency_report,
              labels, secret_ref, created_by, created_at, updated_at
            ) VALUES (
              :id, :project_id, :kind, :name, NULL, :status, :is_default,
              :agent_state, :endpoint, :hostname, :arch, :os_name, {report_sql},
              {labels_sql}, NULL, :actor, :now, :now
            )
            """
        ),
        {
            "id": place_id,
            "project_id": project_id,
            "kind": kind,
            "name": name,
            "status": status,
            "is_default": is_default,
            "agent_state": agent_state,
            "endpoint": endpoint,
            "hostname": hostname,
            "arch": arch,
            "os_name": os_name,
            "dependency_report": _dump(dependency_report or {}),
            "labels": _dump(labels),
            "actor": actor,
            "now": _now(),
        },
    )
    return _select_one(connection, "SELECT * FROM hardware_places WHERE id = :id", {"id": place_id})


def _merge_labels(existing: Any, extra: dict[str, Any]) -> dict[str, Any]:
    current = _load(existing, {})
    if not isinstance(current, dict):
        current = {}
    merged = dict(current)
    merged.update(_labels(extra))
    return merged


def ensure_local_place(
    connection: Any,
    *,
    project_id: str,
    hostname: str,
    arch: str,
    os_name: str,
    actor: str,
) -> dict[str, Any]:
    """Record the installer machine. The agent stays absent until it is installed."""
    kernel = _kernel()
    project = _require_text(project_id, "project_id", max_len=200)
    host = _require_text(hostname, "hostname", max_len=255)
    machine_arch = _require_text(arch, "arch", max_len=64)
    machine_os = _require_text(os_name, "os_name", max_len=64)
    who = _require_text(actor, "actor", max_len=200)
    if kernel is not None and _dialect(connection) == "postgresql":
        kernel.ensure_local_place(
            connection,
            project_id=project,
            hostname=host,
            arch=machine_arch,
            os_name=machine_os,
            actor=who,
        )
    existing = _local_row(connection, project)
    facts = {"hostname": host, "arch": machine_arch, "os": machine_os}
    if existing is None:
        place = _insert_place(
            connection,
            project_id=project,
            kind="local",
            name=host,
            status="pending",
            is_default=True,
            agent_state="absent",
            actor=who,
            labels=facts,
            hostname=host,
            arch=machine_arch,
            os_name=machine_os,
        )
    else:
        dialect = _dialect(connection)
        labels_sql = _json_expr(dialect, "labels")
        connection.execute(
            text(
                f"""
                UPDATE hardware_places
                SET name = :name,
                    hostname = :hostname,
                    arch = :arch,
                    os_name = :os_name,
                    labels = {labels_sql},
                    updated_at = :now
                WHERE id = :id AND kind = 'local'
                """
            ),
            {
                "id": existing["id"],
                "name": host,
                "hostname": host,
                "arch": machine_arch,
                "os_name": machine_os,
                "labels": _dump(_merge_labels(existing["labels"], facts)),
                "now": _now(),
            },
        )
        place = _select_one(
            connection, "SELECT * FROM hardware_places WHERE id = :id", {"id": existing["id"]}
        )
    if not place["is_default"] or place["kind"] != "local":
        raise PlaceRejected("the local machine is the default place")
    _note("local place recorded")
    return place


def install_local_agent(
    connection: Any,
    *,
    project_id: str,
    hostname: str,
    arch: str,
    os_name: str,
    dependency_report: dict[str, Any],
    actor: str,
    endpoint: str | None = None,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark the agent installed on the default machine. No enroll token is used."""
    report = _report(dependency_report)
    _reject_token_echo("", report, labels or {})
    place = ensure_local_place(
        connection,
        project_id=project_id,
        hostname=hostname,
        arch=arch,
        os_name=os_name,
        actor=actor,
    )
    extra = _labels(labels)
    extra.update({
        "hostname": place["hostname"],
        "arch": place["arch"],
        "os": place["os"],
        "source": "node-agent",
    })
    dialect = _dialect(connection)
    connection.execute(
        text(
            f"""
            UPDATE hardware_places
            SET agent_state = 'installed',
                status = 'ready',
                is_default = :is_default,
                endpoint = COALESCE(:endpoint, endpoint),
                dependency_report = {_json_expr(dialect, "dependency_report")},
                labels = {_json_expr(dialect, "labels")},
                updated_at = :now
            WHERE id = :id AND kind = 'local'
            """
        ),
        {
            "id": place["id"],
            "is_default": True,
            "endpoint": endpoint,
            "dependency_report": _dump(report),
            "labels": _dump(_merge_labels(place["labels"], extra)),
            "now": _now(),
        },
    )
    installed = _select_one(
        connection, "SELECT * FROM hardware_places WHERE id = :id", {"id": place["id"]}
    )
    if installed["agent_state"] != "installed" or not installed["is_default"]:
        raise PlaceRejected("local agent install did not stay on the default place")
    _note("local agent installed")
    return installed


def operator_command(control_plane: str, token: str) -> str:
    script = _SCRIPT.read_text(encoding="utf-8")
    if _COMMAND_MARK in script:
        raise RuntimeError("node agent script collides with the command delimiter")
    return (
        f"BOSS_CONTROL_PLANE={shlex.quote(control_plane)} "
        f"BOSS_ENROLL_TOKEN={shlex.quote(token)} "
        f"bash -s <<'{_COMMAND_MARK}'\n"
        f"{script.rstrip()}\n"
        f"{_COMMAND_MARK}\n"
    )


def mint_agent_enroll_command(
    connection: Any,
    *,
    project_id: str,
    name: str,
    actor: str,
    control_plane_url: str,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint a one-time enroll command. Only the token hash is stored."""
    project = _require_text(project_id, "project_id", max_len=200)
    place_name = _require_text(name, "name", max_len=200)
    who = _require_text(actor, "actor", max_len=200)
    control_plane = _control_plane(control_plane_url)
    stored_labels = _labels(labels)
    kernel = _kernel()
    if kernel is not None:
        created = kernel.register_agent_place(
            connection,
            project_id=project,
            name=place_name,
            actor=who,
            labels=stored_labels,
        )
        place_id = created["id"]
        if _flag(created.get("is_default")):
            raise PlaceRejected("additional hardware is not the default")
    else:
        created = _insert_place(
            connection,
            project_id=project,
            kind="agent",
            name=place_name,
            status="pending",
            is_default=False,
            agent_state="absent",
            actor=who,
            labels=stored_labels,
        )
        place_id = created["id"]
    token = secrets.token_urlsafe(32)
    if any(ord(ch) < 32 for ch in token):
        raise EnrollTokenError("enroll token rejected")
    digest = _hash_token(token)
    _reject_token_echo(token, stored_labels, place_name, control_plane)
    connection.execute(
        text(
            """
            INSERT INTO hardware_agent_enroll_tokens (
              id, project_id, place_id, token_hash, consumed_at, created_by, created_at
            ) VALUES (
              :id, :project_id, :place_id, :token_hash, NULL, :actor, :now
            )
            """
        ),
        {
            "id": make_id("henroll"),
            "project_id": project,
            "place_id": place_id,
            "token_hash": digest,
            "actor": who,
            "now": _now(),
        },
    )
    command = operator_command(control_plane, token)
    if token not in command or "--local" in command.splitlines()[0]:
        raise EnrollTokenError("enroll token rejected")
    _note("agent enroll command minted")
    place = _select_one(
        connection, "SELECT * FROM hardware_places WHERE id = :id", {"id": place_id}
    )
    if place["is_default"] or place["agent_state"] != "absent":
        raise PlaceRejected("additional hardware is not the default")
    return {"place": place, "command": command}


def enroll_token_state(connection: Any, place_id: str) -> dict[str, Any]:
    """Return the stored hash and whether it has been used. Never the token."""
    place = _require_text(place_id, "place_id", max_len=200)
    row = connection.execute(
        text(
            """
            SELECT token_hash, consumed_at
            FROM hardware_agent_enroll_tokens
            WHERE place_id = :place_id
            ORDER BY created_at DESC
            """
        ),
        {"place_id": place},
    ).mappings().first()
    if row is None:
        raise PlaceNotFound("enroll token not found")
    return {
        "place_id": place,
        "token_hash": row["token_hash"],
        "used": row["consumed_at"] is not None,
    }


def _consume_token(connection: Any, token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip() or any(ord(ch) < 32 for ch in token):
        raise EnrollTokenError("enroll token rejected")
    digest = _hash_token(token)
    result = connection.execute(
        text(
            """
            UPDATE hardware_agent_enroll_tokens
            SET consumed_at = :now
            WHERE token_hash = :token_hash AND consumed_at IS NULL
            """
        ),
        {"token_hash": digest, "now": _now()},
    )
    if result.rowcount == 1:
        row = connection.execute(
            text(
                """
                SELECT project_id, place_id
                FROM hardware_agent_enroll_tokens
                WHERE token_hash = :token_hash
                """
            ),
            {"token_hash": digest},
        ).mappings().one()
        return {"project_id": row["project_id"], "place_id": row["place_id"]}
    existing = connection.execute(
        text(
            """
            SELECT consumed_at FROM hardware_agent_enroll_tokens
            WHERE token_hash = :token_hash
            """
        ),
        {"token_hash": digest},
    ).mappings().first()
    if existing is None:
        raise EnrollTokenError("enroll token rejected")
    raise EnrollTokenError("enroll token already used")


def accept_agent_call_home(connection: Any, report: dict[str, Any]) -> dict[str, Any]:
    """Record a node-agent report. Local mode has no token. Remote mode spends one."""
    if not isinstance(report, dict):
        raise PlaceRejected("call-home report must be an object")
    mode = report.get("mode")
    hostname = _require_text(report.get("hostname"), "hostname", max_len=255)
    arch = _require_text(report.get("arch"), "arch", max_len=64)
    os_name = _require_text(report.get("os") or report.get("os_name"), "os", max_len=64)
    dependency_report = _report(report.get("dependency_report"))
    endpoint = report.get("endpoint")
    if endpoint is not None:
        endpoint = _require_text(endpoint, "endpoint", max_len=500)
    labels = _labels(report.get("labels"))
    if mode == "local":
        if report.get("token"):
            raise EnrollTokenError("local install does not use an enroll token")
        project_id = _require_text(report.get("project_id"), "project_id", max_len=200)
        _reject_token_echo(str(report.get("token") or ""), dependency_report, labels, endpoint)
        return install_local_agent(
            connection,
            project_id=project_id,
            hostname=hostname,
            arch=arch,
            os_name=os_name,
            dependency_report=dependency_report,
            actor="node-agent",
            endpoint=endpoint,
            labels=labels,
        )
    if mode not in {None, "remote"}:
        raise PlaceRejected("call-home mode must be local or remote")
    token = report.get("token")
    if not isinstance(token, str):
        raise EnrollTokenError("enroll token rejected")
    _reject_token_echo(token, dependency_report, labels, hostname, arch, os_name, endpoint)
    consumed = _consume_token(connection, token)
    place = _select_one(
        connection,
        "SELECT * FROM hardware_places WHERE id = :id",
        {"id": consumed["place_id"]},
    )
    if place["kind"] == "local" or place["is_default"]:
        raise PlaceRejected("additional hardware is not the default")
    extra = dict(labels)
    extra.update({"hostname": hostname, "arch": arch, "os": os_name, "source": "node-agent"})
    dialect = _dialect(connection)
    kernel = _kernel()
    if kernel is not None:
        kernel.mark_place_ready(
            connection,
            place_id=place["id"],
            endpoint=endpoint,
            labels=extra,
        )
    connection.execute(
        text(
            f"""
            UPDATE hardware_places
            SET status = 'ready',
                agent_state = 'installed',
                is_default = :is_default,
                endpoint = :endpoint,
                hostname = :hostname,
                arch = :arch,
                os_name = :os_name,
                dependency_report = {_json_expr(dialect, "dependency_report")},
                labels = {_json_expr(dialect, "labels")},
                updated_at = :now
            WHERE id = :id AND kind <> 'local'
            """
        ),
        {
            "id": place["id"],
            "is_default": False,
            "endpoint": endpoint,
            "hostname": hostname,
            "arch": arch,
            "os_name": os_name,
            "dependency_report": _dump(dependency_report),
            "labels": _dump(_merge_labels(place["labels"], extra)),
            "now": _now(),
        },
    )
    installed = _select_one(
        connection, "SELECT * FROM hardware_places WHERE id = :id", {"id": place["id"]}
    )
    if installed["is_default"] or installed["agent_state"] != "installed":
        raise PlaceRejected("additional hardware is not the default")
    _note("remote agent installed")
    return installed


def get_place(connection: Any, place_id: str) -> dict[str, Any]:
    return _select_one(
        connection,
        "SELECT * FROM hardware_places WHERE id = :id",
        {"id": _require_text(place_id, "place_id", max_len=200)},
    )


def list_places(connection: Any, project_id: str) -> list[dict[str, Any]]:
    project = _require_text(project_id, "project_id", max_len=200)
    rows = connection.execute(
        text(
            """
            SELECT * FROM hardware_places
            WHERE project_id = :project_id
            ORDER BY is_default DESC, created_at ASC, id ASC
            """
        ),
        {"project_id": project},
    ).mappings().all()
    return [_place(row) for row in rows]
