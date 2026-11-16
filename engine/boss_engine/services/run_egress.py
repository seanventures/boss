"""Per-run outbound allowlist and proxy secret injection (BOSSAPP-1265).

A person replaces the hosts one run may call. The proxy refuses any other host.
The secret named ``run:<run_id>`` is added to the outbound request and is never
copied into the receipt.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .receipts import ReceiptService

_HOST = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_RUN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
_SECRET_HEADER = "x-boss-run-secret"
_SECRET_NAME_HEADER = "x-boss-run-secret-name"

Upstream = Callable[[str, str, Mapping[str, str], bytes], Mapping[str, Any]]


class RunEgressError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class HostRefused(RunEgressError):
    def __init__(self, host: str) -> None:
        super().__init__(f"host {host} is not on the run allowlist", status=403)
        self.host = host


def secret_name_for(run_id: str) -> str:
    return f"run:{run_id}"


def echo_upstream(method: str, url: str, headers: Mapping[str, str], body: bytes) -> dict:
    """No dial. The allowlist and the receipt still run."""
    return {"status": 204, "body": b""}


def set_allowlist(
    connection: Connection, *, project_id: str, run_id: str, hosts: list[str], actor: str,
) -> dict:
    person = _person(actor)
    run = _run_id(run_id)
    _tenancy(connection, project_id)
    cleaned = _hosts(hosts)
    row = connection.execute(
        text(
            """
            INSERT INTO run_outbound_allowlists
                (run_id, project_id, hosts, updated_by)
            VALUES (:run_id, :project_id, CAST(:hosts AS JSONB), :actor)
            ON CONFLICT (run_id) DO UPDATE
                SET hosts = EXCLUDED.hosts,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                WHERE run_outbound_allowlists.project_id = EXCLUDED.project_id
            RETURNING run_id, project_id, hosts, updated_by
            """
        ),
        {
            "run_id": run, "project_id": project_id, "hosts": json.dumps(cleaned),
            "actor": person,
        },
    ).mappings().first()
    if row is None:
        raise RunEgressError(f"run {run} belongs to another project", status=409)
    return _allowlist_row(row)


def get_allowlist(connection: Connection, *, project_id: str, run_id: str) -> dict:
    run = _run_id(run_id)
    row = connection.execute(
        text(
            """
            SELECT run_id, project_id, hosts, updated_by
            FROM run_outbound_allowlists
            WHERE run_id = :run_id AND project_id = :project_id
            """
        ),
        {"run_id": run, "project_id": project_id},
    ).mappings().first()
    if row is None:
        return {"run_id": run, "project_id": project_id, "hosts": [], "updated_by": None}
    return _allowlist_row(row)


def put_run_secret(
    connection: Connection, *, project_id: str, run_id: str, value: str, actor: str,
) -> dict:
    person = _person(actor)
    run = _run_id(run_id)
    _tenancy(connection, project_id)
    secret = _secret_value(value)
    name = secret_name_for(run)
    row = connection.execute(
        text(
            """
            INSERT INTO run_egress_secrets
                (run_id, project_id, secret_name, secret_value, updated_by)
            VALUES (:run_id, :project_id, :name, :value, :actor)
            ON CONFLICT (run_id) DO UPDATE
                SET secret_name = EXCLUDED.secret_name,
                    secret_value = EXCLUDED.secret_value,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                WHERE run_egress_secrets.project_id = EXCLUDED.project_id
            RETURNING run_id, project_id, secret_name
            """
        ),
        {
            "run_id": run, "project_id": project_id, "name": name,
            "value": secret, "actor": person,
        },
    ).mappings().first()
    if row is None:
        raise RunEgressError(f"run {run} belongs to another project", status=409)
    return {
        "run_id": row["run_id"],
        "project_id": row["project_id"],
        "secret_name": row["secret_name"],
        "stored": True,
    }


def proxy(
    connection: Connection, *, project_id: str, run_id: str, host: str, method: str,
    path: str, upstream: Upstream,
) -> dict:
    """Refuse a host that is not listed, then inject the run's secret and record a receipt.

    A repeat of the same request returns the existing receipt and does not call out again.
    """
    run = _run_id(run_id)
    cleaned_host = _host(host)
    cleaned_method = _method(method)
    cleaned_path = _path(path)
    org_id, workspace_id = _tenancy(connection, project_id)
    if cleaned_host not in set(get_allowlist(
        connection, project_id=project_id, run_id=run,
    )["hosts"]):
        raise HostRefused(cleaned_host)
    name, secret = _load_secret(connection, project_id=project_id, run_id=run)
    dedupe_key = f"run-egress:{project_id}:{run}:{cleaned_method}:{cleaned_host}:{cleaned_path}"
    receipts = ReceiptService()
    existing = receipts.get_by_dedupe_key(connection, org_id, dedupe_key)
    if existing and existing.get("status") == "succeeded":
        return _from_receipt(existing, reused=True)
    scope = {
        "run_id": run,
        "host": cleaned_host,
        "method": cleaned_method,
        "path": cleaned_path,
        "secret_name": name,
    }
    _refuse_if_secret_leaked(scope, secret)
    receipt, _created = receipts.create_or_reuse(
        connection, org_id, workspace_id, project_id, dedupe_key,
        "run_egress.proxy", scope, dict(scope),
    )
    url = f"https://{cleaned_host}{cleaned_path}"
    headers = {_SECRET_HEADER: secret, _SECRET_NAME_HEADER: name}
    try:
        response = upstream(cleaned_method, url, headers, b"")
    except Exception as exc:
        message = _redact(str(exc) or "upstream failed", secret)
        receipts.mark_failed(
            connection, receipt["id"], {"code": "upstream_failed", "message": message},
        )
        raise RunEgressError(message, status=502) from RuntimeError(message)
    if not isinstance(response, Mapping) or "status" not in response:
        message = "upstream returned no status"
        receipts.mark_failed(connection, receipt["id"], {"code": "upstream_failed", "message": message})
        raise RunEgressError(message, status=502)
    result = {**scope, "status": int(response["status"]), "injected": True}
    _refuse_if_secret_leaked(result, secret)
    receipts.mark_succeeded(connection, receipt["id"], url, result)
    return {
        "receipt_id": receipt["id"],
        "dedupe_key": dedupe_key,
        "status": int(response["status"]),
        "host": cleaned_host,
        "reused": False,
        "secret_name": name,
    }


def _from_receipt(receipt: Mapping[str, Any], *, reused: bool) -> dict:
    result = receipt.get("result") or {}
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, Mapping):
        result = {}
    return {
        "receipt_id": receipt["id"],
        "dedupe_key": receipt.get("dedupe_key"),
        "status": int(result.get("status") or 0),
        "host": result.get("host"),
        "reused": reused,
        "secret_name": result.get("secret_name"),
    }


def _load_secret(connection: Connection, *, project_id: str, run_id: str) -> tuple[str, str]:
    name = secret_name_for(run_id)
    row = connection.execute(
        text(
            """
            SELECT secret_name, secret_value
            FROM run_egress_secrets
            WHERE run_id = :run_id AND project_id = :project_id AND secret_name = :name
            """
        ),
        {"run_id": run_id, "project_id": project_id, "name": name},
    ).mappings().first()
    if row is None or not row["secret_value"]:
        raise RunEgressError(f"run {run_id} has no secret named {name}", status=422)
    return str(row["secret_name"]), str(row["secret_value"])


def _person(actor: str) -> str:
    name = (actor or "").strip()
    if not name.startswith("person:") or name == "person:":
        raise RunEgressError("a person edits the run allowlist", status=403)
    return name


def _run_id(run_id: str) -> str:
    run = (run_id or "").strip()
    if not _RUN.fullmatch(run):
        raise RunEgressError("run id is not valid", status=422)
    return run


def _host(host: str) -> str:
    cleaned = (host or "").strip().lower().rstrip(".")
    if not _HOST.fullmatch(cleaned):
        raise RunEgressError(f"host {host!r} is not a hostname", status=422)
    return cleaned


def _hosts(hosts: list[str]) -> list[str]:
    if not isinstance(hosts, list):
        raise RunEgressError("hosts must be a list", status=422)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in hosts:
        host = _host(str(raw))
        if host not in seen:
            seen.add(host)
            cleaned.append(host)
    return cleaned


def _method(method: str) -> str:
    cleaned = (method or "").strip().upper()
    if cleaned not in _METHODS:
        raise RunEgressError(f"method {method!r} is not allowed", status=422)
    return cleaned


def _path(path: str) -> str:
    raw = path if path else "/"
    if not isinstance(raw, str) or not raw.startswith("/") or any(ch in raw for ch in "?#\\\n\r "):
        raise RunEgressError("path must be an absolute path", status=422)
    return raw


def _secret_value(value: str) -> str:
    secret = value if isinstance(value, str) else ""
    if not secret.strip():
        raise RunEgressError("the run secret is empty", status=422)
    if len(secret) > 8192:
        raise RunEgressError("the run secret is too long", status=422)
    return secret


def _tenancy(connection: Connection, project_id: str) -> tuple[str, str]:
    row = connection.execute(
        text(
            """
            SELECT p.workspace_id, w.org_id
            FROM projects p
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE p.id = :id
            """
        ),
        {"id": project_id},
    ).mappings().first()
    if row is None:
        raise RunEgressError(f"project {project_id} was not found", status=404)
    return str(row["org_id"]), str(row["workspace_id"])


def _allowlist_row(row: Mapping[str, Any]) -> dict:
    hosts = row["hosts"]
    if isinstance(hosts, str):
        hosts = json.loads(hosts)
    return {
        "run_id": row["run_id"],
        "project_id": row["project_id"],
        "hosts": list(hosts or []),
        "updated_by": row["updated_by"],
    }


def _redact(message: str, secret: str) -> str:
    if not secret:
        return message
    return message.replace(secret, "[redacted]")


def _refuse_if_secret_leaked(payload: Mapping[str, Any], secret: str) -> None:
    if secret and secret in json.dumps(payload):
        raise RunEgressError("refusing to record a receipt that contains the run secret", status=500)
