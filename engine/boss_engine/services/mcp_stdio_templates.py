"""STDIO MCP TEMPLATES (BOSSAPP-850): how BOSS launches a stdio MCP server, set by an operator.

A template is the launch recipe — argv, image, the environment NAMES it may receive, the
network it may use, its idle TTL and how instances are shared — never a secret. A registered
server points at one as ``stdio://<project_id>/<name>``; mcp_stdio_supervisor spawns it in a
sandboxed container and presents it to the registry as an ordinary streamable-HTTP server, so
review, policy, receipts and audit are unchanged.

Writing a template is running a command on BOSS's host, so the API gates it with `host:exec`
(Owner/Admin, step-up, critical, not delegable) — the strictest project permission BOSS has for
exactly that act. This module validates and stores; it never spawns anything.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .connector_catalog import validate_stdio
from .core import emit_event, make_id

STDIO_SCHEME = "stdio://"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_PROJECT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

_COLUMNS = ("id, project_id, name, description, argv, image, env_allowlist, env_defaults, "
            "env_from_auth, network, idle_ttl_seconds, reuse_key, connector_key, status, "
            "created_by, updated_by, created_at, updated_at")

#: The fields a launch depends on: an edit to any of them is a different instance.
LAUNCH_FIELDS = ("argv", "image", "env_allowlist", "env_defaults", "env_from_auth", "network",
                 "reuse_key")
EDITABLE = LAUNCH_FIELDS + ("description", "idle_ttl_seconds", "status", "connector_key")


class StdioTemplateError(ValueError):
    """Something the operator can fix. The API answers 422."""


class StdioTemplateConflict(StdioTemplateError):
    """The name is taken. 409."""


class StdioTemplateInUse(StdioTemplateError):
    """A registered server still points at it. 409."""


def parse_stdio_url(url: Any) -> Optional[tuple[str, str]]:
    """(project_id, template name) of a ``stdio://<project>/<name>`` address, else None."""
    if not isinstance(url, str) or not url.startswith(STDIO_SCHEME):
        return None
    rest = url[len(STDIO_SCHEME):].strip("/")
    project, _, name = rest.partition("/")
    if not _PROJECT.match(project) or not _NAME.match(name):
        return None
    return project, name


def stdio_url(project_id: str, name: str) -> str:
    return f"{STDIO_SCHEME}{project_id}/{name}"


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row(r: Any) -> dict[str, Any]:
    item = dict(r)
    for key, fallback in (("argv", []), ("env_allowlist", []), ("env_defaults", {})):
        item[key] = _loads(item.get(key), fallback)
    item["idle_ttl_seconds"] = int(item["idle_ttl_seconds"])
    item["created_at"] = _iso(item.get("created_at"))
    item["updated_at"] = _iso(item.get("updated_at"))
    item["url"] = stdio_url(item["project_id"], item["name"])
    item["launch_hash"] = launch_hash(item)
    return item


def launch_hash(template: dict[str, Any]) -> str:
    material = {k: template.get(k) for k in LAUNCH_FIELDS}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _normalized(fields: dict[str, Any], *, partial_of: Optional[dict[str, Any]] = None
                ) -> dict[str, Any]:
    base = dict(partial_of or {})
    for key in EDITABLE + ("name",):
        if key in fields:
            base[key] = fields[key]
    name = str(base.get("name") or "").strip()
    if not _NAME.match(name):
        raise StdioTemplateError(
            "name must be a lowercase slug (a-z, 0-9, '-', '_'), up to 63 characters")
    base["name"] = name
    base.setdefault("network", "none")
    base.setdefault("env_allowlist", [])
    base.setdefault("env_defaults", {})
    base.setdefault("idle_ttl_seconds", 600)
    base.setdefault("reuse_key", "credential")
    base.setdefault("status", "active")
    base["network"] = str(base.get("network") or "none").strip()
    base["image"] = str(base.get("image") or "").strip()
    base["env_from_auth"] = (str(base["env_from_auth"]).strip() or None) \
        if base.get("env_from_auth") else None
    problems = validate_stdio(base)
    if base["status"] not in ("active", "disabled"):
        problems.append("status must be active or disabled")
    if problems:
        raise StdioTemplateError("; ".join(problems))
    return base


def list_templates(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        f"SELECT {_COLUMNS} FROM mcp_stdio_templates WHERE project_id = :p ORDER BY name"),
        {"p": project_id}).mappings().all()
    return [_row(r) for r in rows]


def get_template(connection: Connection, project_id: str, template_id: str
                 ) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        f"SELECT {_COLUMNS} FROM mcp_stdio_templates WHERE id = :id AND project_id = :p"),
        {"id": template_id, "p": project_id}).mappings().first()
    return _row(row) if row else None


def find_template(connection: Connection, project_id: str, name: str
                  ) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        f"SELECT {_COLUMNS} FROM mcp_stdio_templates WHERE project_id = :p AND name = :n"),
        {"p": project_id, "n": name}).mappings().first()
    return _row(row) if row else None


def create_template(connection: Connection, project_id: str, fields: dict[str, Any], *,
                    actor: str) -> dict[str, Any]:
    t = _normalized(fields)
    if find_template(connection, project_id, t["name"]):
        raise StdioTemplateConflict(f"a stdio template named '{t['name']}' already exists")
    template_id = make_id("mcpstdio")
    connection.execute(text(
        "INSERT INTO mcp_stdio_templates (id, project_id, name, description, argv, image, "
        "env_allowlist, env_defaults, env_from_auth, network, idle_ttl_seconds, reuse_key, "
        "connector_key, status, created_by, updated_by) VALUES (:id, :p, :name, :description, "
        "CAST(:argv AS JSONB), :image, CAST(:allow AS JSONB), CAST(:defaults AS JSONB), "
        ":from_auth, :network, :ttl, :reuse, :connector, :status, :actor, :actor)"),
        {"id": template_id, "p": project_id, "name": t["name"],
         "description": t.get("description"), "argv": json.dumps(t["argv"]),
         "image": t["image"], "allow": json.dumps(t["env_allowlist"]),
         "defaults": json.dumps(t["env_defaults"]), "from_auth": t["env_from_auth"],
         "network": t["network"], "ttl": int(t["idle_ttl_seconds"]), "reuse": t["reuse_key"],
         "connector": t.get("connector_key"), "status": t["status"], "actor": actor})
    made = get_template(connection, project_id, template_id)
    assert made is not None
    emit_event(connection, project_id, "mcp.stdio_template.created", actor,
               "mcp_stdio_template", template_id,
               {"name": made["name"], "image": made["image"], "network": made["network"],
                "argv": made["argv"]})
    return made


def update_template(connection: Connection, project_id: str, template_id: str,
                    changes: dict[str, Any], *, actor: str) -> dict[str, Any]:
    current = get_template(connection, project_id, template_id)
    if current is None:
        raise LookupError("stdio template not found")
    if "name" in changes and changes["name"] != current["name"]:
        raise StdioTemplateError("a template's name is its address (stdio://…/name); "
                                 "create a new template instead of renaming")
    t = _normalized(changes, partial_of=current)
    connection.execute(text(
        "UPDATE mcp_stdio_templates SET description = :description, argv = CAST(:argv AS JSONB), "
        "image = :image, env_allowlist = CAST(:allow AS JSONB), "
        "env_defaults = CAST(:defaults AS JSONB), env_from_auth = :from_auth, network = :network, "
        "idle_ttl_seconds = :ttl, reuse_key = :reuse, connector_key = :connector, "
        "status = :status, updated_by = :actor, updated_at = now() "
        "WHERE id = :id AND project_id = :p"),
        {"id": template_id, "p": project_id, "description": t.get("description"),
         "argv": json.dumps(t["argv"]), "image": t["image"],
         "allow": json.dumps(t["env_allowlist"]), "defaults": json.dumps(t["env_defaults"]),
         "from_auth": t["env_from_auth"], "network": t["network"],
         "ttl": int(t["idle_ttl_seconds"]), "reuse": t["reuse_key"],
         "connector": t.get("connector_key"), "status": t["status"], "actor": actor})
    updated = get_template(connection, project_id, template_id)
    assert updated is not None
    emit_event(connection, project_id, "mcp.stdio_template.updated", actor,
               "mcp_stdio_template", template_id,
               {"name": updated["name"], "changed": sorted(k for k in EDITABLE
                                                           if current.get(k) != updated.get(k))})
    return updated


def servers_using(connection: Connection, project_id: str, name: str) -> list[str]:
    rows = connection.execute(text(
        "SELECT name FROM mcp_servers WHERE project_id = :p AND url = :u ORDER BY name"),
        {"p": project_id, "u": stdio_url(project_id, name)}).scalars().all()
    return [str(r) for r in rows]


def delete_template(connection: Connection, project_id: str, template_id: str, *,
                    actor: str) -> None:
    current = get_template(connection, project_id, template_id)
    if current is None:
        raise LookupError("stdio template not found")
    holders = servers_using(connection, project_id, current["name"])
    if holders:
        raise StdioTemplateInUse(
            f"'{current['name']}' is still the launch recipe of {', '.join(holders)}; "
            "remove or re-point those servers first")
    connection.execute(text("DELETE FROM mcp_stdio_templates WHERE id = :id AND project_id = :p"),
                       {"id": template_id, "p": project_id})
    emit_event(connection, project_id, "mcp.stdio_template.deleted", actor,
               "mcp_stdio_template", template_id, {"name": current["name"]})


def require_template_for_url(connection: Connection, project_id: str, url: str) -> None:
    """The registry's URL check for a stdio server: the address names a template of THIS
    project. Raises :class:`StdioTemplateError` with what to do."""
    parsed = parse_stdio_url(url)
    if parsed is None:
        raise StdioTemplateError(
            "a stdio server's URL is stdio://<project_id>/<template name>")
    owner, name = parsed
    if owner != project_id:
        raise StdioTemplateError("a stdio server can only use a template of its own project")
    if find_template(connection, project_id, name) is None:
        raise StdioTemplateError(
            f"no stdio template named '{name}' in this project; an Owner or Admin creates it "
            "first (Add MCP › Connectors › Create template)")
