"""PORTABILITY (BOSSAPP-837): take a project, or a whole org, from one BOSS install to another.

BOSS could export one pack, one skill or the audit log. Moving a configured project (its agents,
their skills and servers, its workflows, policies and model choices) meant redoing it by hand.

THE BUNDLE is one JSON document, versioned:

    {"format": "boss.portability-bundle", "schema_version": 1,
     "manifest": {scope, source: {install_id, boss_commit, org_id, projects}, exported_at,
                  exported_by, counts, checksum},
     "org": {"model_settings": {...}} | null,
     "projects": [{"ref": {slug, name, description}, "sections": {
         "model_settings", "routing_policy", "packs", "roles", "skills", "mcp_servers",
         "role_skills", "role_mcp", "custom_steps", "workflows", "deploy_policies",
         "tool_always_allow"}}],
     "secrets": [{"kind", "name", "scope_type", "value": null, "placeholder": true}],
     "fidelity": {"exported": {...counts}, "not_exported": [{what, count, why}]}}

SECRETS NEVER TRAVEL. A secret is exported as a TYPED EMPTY PLACEHOLDER — its kind (the
secret_refs kinds) and its name, `value: null` — and nothing that reads one (the secret store,
`secret_material`, a server's stored auth, its last four) is ever called. On top of that, every
section passes through :func:`_scrub`, which replaces any string under a secret-shaped key with a
placeholder, and :func:`assert_no_secret_values` refuses a bundle that still carries one.

IMPORT is two steps. :func:`plan_import` (the dry run) diffs the bundle against the target —
create / update / unchanged for every item — and VALIDATES it by replaying every item through the
same create services the console uses, inside a transaction it then rolls back. Nothing is ever
written with a raw INSERT. :func:`apply_import` then replays it for real, CHUNK_SIZE items per
transaction, recording the resume point (`portability_imports.chunks_done`) after each chunk, so
an apply that stops part way is carried on by applying the same import again.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

FORMAT = "boss.portability-bundle"
SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
CHUNK_SIZE = 25
PLACEHOLDER_KEY = "$secret_placeholder"

#: The order an import replays sections in: what a later item names must exist by then.
SECTION_ORDER = (
    "model_settings", "routing_policy", "packs", "roles", "role_reports_to", "skills",
    "mcp_servers", "role_skills", "role_mcp", "custom_steps", "workflows", "deploy_policies",
    "tool_always_allow",
)

_SECRET_KEY = re.compile(
    r"(^|_)(secret|secrets|password|passwd|token|api_?key|private_?key|access_?key|bearer|"
    r"authorization|client_secret|session_token)($|_)", re.IGNORECASE)
_NOT_A_VALUE = re.compile(r"_(kind|name|type|header|count|limit|budget|order|label|id_hint)$",
                          re.IGNORECASE)


class PortabilityError(ValueError):
    """A bundle this install cannot take, or an import that cannot run; the message says why."""


# ------------------------------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------------------------------ #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def checksum(bundle: dict[str, Any]) -> str:
    body = {k: v for k, v in bundle.items() if k != "manifest"}
    manifest = {k: v for k, v in (bundle.get("manifest") or {}).items() if k != "checksum"}
    return "sha256:" + hashlib.sha256(canonical({**body, "manifest": manifest}).encode()).hexdigest()


def _placeholder(kind: str, name: str, scope_type: str = "project") -> dict[str, Any]:
    return {PLACEHOLDER_KEY: {"kind": kind, "name": name, "scope_type": scope_type,
                              "value": None}}


def _scrub(value: Any, path: str, found: list[str]) -> Any:
    """Replace any string under a secret-shaped key with a typed empty placeholder."""
    if isinstance(value, dict):
        if PLACEHOLDER_KEY in value:
            return value
        out: dict[str, Any] = {}
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if (isinstance(item, str) and item.strip() and _SECRET_KEY.search(str(key))
                    and not _NOT_A_VALUE.search(str(key))):
                out[key] = _placeholder("api_token", here)
                found.append(here)
            else:
                out[key] = _scrub(item, here, found)
        return out
    if isinstance(value, list):
        return [_scrub(item, f"{path}[{i}]", found) for i, item in enumerate(value)]
    return value


def assert_no_secret_values(bundle: dict[str, Any]) -> None:
    """Refuse a bundle that carries a value under a secret-shaped key (outside placeholders)."""
    leaks: list[str] = []
    _scrub({k: v for k, v in bundle.items() if k not in ("manifest", "fidelity")}, "", leaks)
    if leaks:
        raise PortabilityError(f"the bundle carries secret-shaped values at: {', '.join(leaks[:10])}")


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _org_id(connection: Connection, project_id: str) -> Optional[str]:
    from . import org_models

    return org_models.org_of(connection, project_id)


def _install_identity(connection: Connection) -> str:
    """This install's id (resource_ledger.install_id) without creating one: an export reads."""
    env = (os.environ.get("BOSS_INSTALL_ID") or "").strip()
    if env:
        return env
    try:
        with connection.begin_nested():
            value = connection.execute(text(
                "SELECT value FROM system_state WHERE key = 'install_id'")).scalar()
    except Exception:  # noqa: BLE001 — an install without system_state has no recorded id
        return "unrecorded"
    value = _json(value, value)
    if isinstance(value, dict) and value.get("id"):
        return str(value["id"])
    return str(value) if value else "unrecorded"


def _project_ref(connection: Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(text("SELECT id, name, slug, description FROM projects WHERE id = :p"),
                             {"p": project_id}).mappings().first()
    if row is None:
        raise PortabilityError(f"no project {project_id!r}")
    return {"slug": row["slug"], "name": row["name"], "description": row["description"]}


# ------------------------------------------------------------------------------------------ #
# export
# ------------------------------------------------------------------------------------------ #

def _secret_placeholders(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    """{secret_ref_id: placeholder} — kind and name only. Reads secret_refs metadata, never a
    value: no backend, no secret_material."""
    rows = connection.execute(text(
        "SELECT id, scope_type, scope_id, ref_key, secret_kind, metadata FROM secret_refs "
        "WHERE project_id = :p ORDER BY created_at"), {"p": project_id}).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        meta = _json(r["metadata"], {}) or {}
        prefix = "BOSS_" + re.sub(r"[^A-Za-z0-9]+", "_", str(r["scope_id"] or "").upper()).strip("_") + "_"
        name = str(r["ref_key"] or "")
        name = name[len(prefix):] if name.startswith(prefix) else name
        out[str(r["id"])] = {"kind": meta.get("secret_kind_detail") or r["secret_kind"],
                             "name": name.lower(), "scope_type": r["scope_type"] or "project",
                             "value": None, "placeholder": True}
    return out


def _model_settings(connection: Connection, project_id: str) -> tuple[dict[str, Any], int]:
    from . import org_models, project_settings

    own = project_settings.get_own(connection, project_id)
    knobs = set(org_models.MODEL_KNOBS)
    model = {k: v for k, v in own.items() if k in knobs}
    return model, len(own) - len(model)


def _sections(connection: Connection, settings: Any, project_id: str,
              secrets: dict[str, dict[str, Any]],
              not_exported: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from . import agents, mcp_registry, skill_definitions, tool_policies, workflows
    from .model_router.store import load_policy_doc
    from .specpack import SpecPackService

    def skip(what: str, count: int, why: str) -> None:
        if count:
            entry = not_exported.setdefault(what, {"what": what, "count": 0, "why": why})
            entry["count"] += count

    model, other_settings = _model_settings(connection, project_id)
    skip("other project settings", other_settings,
         "they name this install's own repositories, hosts and apps; only model settings travel")
    policy_doc, _version = load_policy_doc(connection, project_id)

    packs = []
    for r in connection.execute(text(
            "SELECT pack_id, pack_version, source, yaml_blob FROM pack_installs "
            "WHERE project_id = :p ORDER BY installed_at"), {"p": project_id}).mappings():
        item = {"pack_id": r["pack_id"], "version": r["pack_version"],
                "source": r["source"] or "baked"}
        if r["source"] == "local_upload" and r["yaml_blob"]:
            item["yaml"] = r["yaml_blob"]
        packs.append(item)

    roles = []
    for r in SpecPackService(settings).list_roles_for_project(connection, project_id):
        roles.append({"role_key": r["role_key"], "display_name": r["display_name"],
                      "autonomy_level": r["autonomy_level"], "reports_to": r.get("reports_to"),
                      "spec": r.get("spec") or {}})
    skip("agent track records", len(roles),
         "trust scores and run counters are earned on the install that ran the work")

    skills = []
    for s in skill_definitions.list_skills(connection, project_id, limit=10000):
        skills.append({k: s[k] for k in ("skill_key", "label", "summary", "behaviour_kind",
                                         "knowledge", "inputs", "produces", "config",
                                         "risk_tier", "trusted", "enabled")})

    servers = []
    for s in mcp_registry.list_mcp_servers(connection, project_id):
        raw = _json(connection.execute(text("SELECT auth FROM mcp_servers WHERE id = :i"),
                                       {"i": s["id"]}).scalar(), {}) or {}
        auth: dict[str, Any] = {"type": str(raw.get("type") or "none")}
        if raw.get("header_name"):
            auth["header_name"] = raw["header_name"]
        # A bearer/header server whose token is not set (yet: an imported one) still needs one,
        # so it is named the same way as a set one.
        ref = raw.get("secret_ref_id") or (
            f"unset:{s['name']}" if auth["type"] in ("bearer", "header") else None)
        if ref:
            ph = secrets.get(str(ref)) or {"kind": "api_token", "name": f"mcp_{s['name']}_token",
                                           "scope_type": "project", "value": None,
                                           "placeholder": True}
            secrets.setdefault(str(ref), ph)
            auth["secret"] = {PLACEHOLDER_KEY: {k: ph[k] for k in ("kind", "name", "scope_type", "value")}}
        if raw.get("headers"):
            auth["header_names"] = sorted(str(k) for k in raw["headers"])
            skip("MCP static header values", len(raw["headers"]),
                 "a header value can be a credential; the names travel, the values are set again")
        servers.append({"name": s["name"], "url": s["url"], "transport": s["transport"],
                        "risk_tier": s["risk_tier"], "data_boundary": s["data_boundary"],
                        "description": s.get("description"), "enabled": s["enabled"],
                        "tool_risk_overrides": s.get("tool_risk_overrides") or {},
                        "tool_policy": s.get("tool_policy") or {}, "auth": auth})
    server_names = {s["id"]: s["name"] for s in mcp_registry.list_mcp_servers(connection, project_id)}

    role_skills = [{"role_key": r["role_key"], "skill_id": r["skill_id"]}
                   for r in skill_definitions.list_role_skills(connection, project_id)]
    role_mcp = [{"role_key": r["role_key"], "server": r["server"], "tools": r["tools"]}
                for r in agents.list_role_mcp(connection, project_id)]

    steps = []
    for r in connection.execute(text(
            "SELECT step_key, label, summary, binding_kind, skill_id, mcp_server_id, "
            "mcp_tool_name, inputs, produces, knowledge FROM workflow_custom_steps "
            "WHERE project_id = :p ORDER BY step_key"), {"p": project_id}).mappings():
        steps.append({"step_key": r["step_key"], "label": r["label"], "summary": r["summary"] or "",
                      "binding_kind": r["binding_kind"], "skill_id": r["skill_id"],
                      "server": server_names.get(r["mcp_server_id"]) if r["mcp_server_id"] else None,
                      "mcp_tool_name": r["mcp_tool_name"], "inputs": _json(r["inputs"], []),
                      "produces": _json(r["produces"], []), "knowledge": r["knowledge"] or ""})

    flows = []
    details = workflows.list_definition_details(connection, project_id,
                                                namespace=workflows.PRODUCT_NAMESPACE)
    for d in details:
        if d.get("is_catalogue"):
            continue
        versions = d.get("versions") or []
        active = next((v for v in versions if v["id"] == d.get("active_version_id")),
                      versions[0] if versions else None)
        if active is None:
            continue
        spec = copy.deepcopy(active["spec"])
        spec.pop("step_agent_rules", None)
        flows.append({"workflow_key": d["workflow_key"], "name": d["name"],
                      "description": d.get("description"), "spec": spec})
    system = connection.execute(text(
        "SELECT count(*) FROM workflow_definitions WHERE project_id = :p AND namespace = 'system'"),
        {"p": project_id}).scalar() or 0
    skip("BOSS's own workflows", int(system), "every install ships them; the target has its own")
    mappings = connection.execute(text(
        "SELECT count(*) FROM workflow_mappings WHERE project_id = :p"), {"p": project_id}).scalar()
    skip("workflow mappings", int(mappings or 0),
         "they select by this install's apps and scopes; map the imported workflows again")
    app_agents = connection.execute(text(
        "SELECT count(*) FROM project_agents WHERE project_id = :p"), {"p": project_id}).scalar()
    skip("app agent assignments", int(app_agents or 0), "apps and their deployments do not travel")

    deploy = []
    for r in connection.execute(text(
            "SELECT environment, requires_approval FROM deployment_environment_policies "
            "WHERE project_id = :p AND status = 'active' ORDER BY environment"),
            {"p": project_id}).mappings():
        deploy.append({"environment": r["environment"],
                       "requires_approval": bool(r["requires_approval"])})

    allow = []
    for r in tool_policies.list_always_allow(connection, project_id=project_id):
        allow.append({"server": server_names.get(r["server_id"]) if r["server_id"] else None,
                      "tool": r["tool"], "schema_sha256": r["schema_sha256"]})
    allow.sort(key=lambda a: (a["server"] or "", a["tool"], a["schema_sha256"] or ""))
    # de-duplicate: a second grant of the same thing is one rule
    allow = [a for i, a in enumerate(allow) if i == 0 or a != allow[i - 1]]

    return {"model_settings": model, "routing_policy": policy_doc, "packs": packs,
            "roles": roles, "skills": skills, "mcp_servers": servers,
            "role_skills": role_skills, "role_mcp": role_mcp, "custom_steps": steps,
            "workflows": flows, "deploy_policies": deploy, "tool_always_allow": allow}


def export_bundle(connection: Connection, settings: Any, *, scope: str, target_id: str,
                  actor: str) -> dict[str, Any]:
    """The bundle for one project (`scope='project'`) or every project of an org (`'org'`)."""
    from . import org_models

    if scope == "project":
        project_ids = [target_id]
        _project_ref(connection, target_id)
        org_id = _org_id(connection, target_id)
    elif scope == "org":
        org_id = target_id
        if not connection.execute(text("SELECT 1 FROM orgs WHERE id = :o"), {"o": org_id}).first():
            raise PortabilityError(f"no org {org_id!r}")
        project_ids = org_models.org_projects(connection, org_id)
    else:
        raise PortabilityError("scope must be project or org")

    not_exported: dict[str, dict[str, Any]] = {}
    secrets: dict[str, dict[str, Any]] = {}
    projects = []
    for pid in project_ids:
        project_secrets = _secret_placeholders(connection, pid)
        secrets.update(project_secrets)
        sections = _sections(connection, settings, pid, secrets, not_exported)
        projects.append({"ref": _project_ref(connection, pid), "sections": sections})

    org_part = None
    if scope == "org":
        rows = connection.execute(text(
            "SELECT key, value FROM org_settings WHERE org_id = :o ORDER BY key"),
            {"o": org_id}).mappings().all()
        prefix = getattr(org_models, "PREFIX", "")
        knobs = set(org_models.MODEL_KNOBS)
        model = {}
        for r in rows:
            key = str(r["key"])[len(prefix):] if str(r["key"]).startswith(prefix) else str(r["key"])
            if key in knobs:
                model[key] = r["value"]
        org_part = {"model_settings": model}

    scrubbed: list[str] = []
    projects = _scrub(projects, "projects", scrubbed)
    org_part = _scrub(org_part, "org", scrubbed) if org_part else None
    placeholders = sorted(
        ({k: v for k, v in s.items()} for s in secrets.values()),
        key=lambda s: (s["scope_type"], s["name"]))
    placeholders += [{"kind": "api_token", "name": path, "scope_type": "project", "value": None,
                      "placeholder": True} for path in scrubbed]
    if placeholders:
        not_exported["secret values"] = {
            "what": "secret values", "count": len(placeholders),
            "why": "secrets never leave an install; each is a typed empty placeholder to fill in"}

    counts: dict[str, int] = {}
    for p in projects:
        for name, section in p["sections"].items():
            if isinstance(section, (list, dict)):
                counts[name] = counts.get(name, 0) + len(section or [])
    bundle: dict[str, Any] = {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "manifest": {
            "scope": scope,
            "source": {"install_id": _install_identity(connection),
                       "boss_commit": os.environ.get("BOSS_SOURCE_SHA") or "unknown",
                       "org_id": org_id, "projects": [p["ref"]["slug"] for p in projects]},
            "exported_at": _now(), "exported_by": actor, "counts": counts,
        },
        "org": org_part,
        "projects": projects,
        "secrets": placeholders,
        "fidelity": {"exported": counts,
                     "not_exported": sorted(not_exported.values(), key=lambda e: e["what"])},
    }
    assert_no_secret_values(bundle)
    bundle["manifest"]["checksum"] = checksum(bundle)
    return bundle


# ------------------------------------------------------------------------------------------ #
# import: the plan
# ------------------------------------------------------------------------------------------ #

def validate_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise PortabilityError(f"not a BOSS bundle (format must be {FORMAT})")
    version = bundle.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise PortabilityError(
            f"bundle schema version {version!r} is not one this install reads "
            f"({sorted(SUPPORTED_SCHEMA_VERSIONS)}); upgrade BOSS first")
    manifest = bundle.get("manifest") or {}
    if manifest.get("checksum") and manifest["checksum"] != checksum(bundle):
        raise PortabilityError("the bundle's checksum does not match its content (edited or cut)")
    if not isinstance(bundle.get("projects"), list):
        raise PortabilityError("the bundle has no projects list")
    assert_no_secret_values(bundle)
    return manifest


def _ops_for(project: dict[str, Any]) -> list[dict[str, Any]]:
    """The import, item by item, in SECTION_ORDER. Stable for a given bundle (the resume key)."""
    s = project.get("sections") or {}
    ops: list[dict[str, Any]] = []

    def add(section: str, key: str, data: Any) -> None:
        ops.append({"op_id": f"{section}:{key}", "section": section, "key": key, "data": data})

    for k, v in sorted((s.get("model_settings") or {}).items()):
        add("model_settings", k, v)
    if s.get("routing_policy"):
        add("routing_policy", "policy", s["routing_policy"])
    for p in s.get("packs") or []:
        add("packs", p["pack_id"], p)
    for r in s.get("roles") or []:
        add("roles", r["role_key"], r)
    for r in s.get("roles") or []:
        if r.get("reports_to"):
            add("role_reports_to", r["role_key"], r["reports_to"])
    for k in s.get("skills") or []:
        add("skills", k["skill_key"], k)
    for m in s.get("mcp_servers") or []:
        add("mcp_servers", m["name"], m)
    for g in s.get("role_skills") or []:
        add("role_skills", f"{g['role_key']}/{g['skill_id']}", g)
    for g in s.get("role_mcp") or []:
        add("role_mcp", f"{g['role_key']}/{g['server']}", g)
    for c in s.get("custom_steps") or []:
        add("custom_steps", c["step_key"], c)
    for w in s.get("workflows") or []:
        add("workflows", w["workflow_key"], w)
    for d in s.get("deploy_policies") or []:
        add("deploy_policies", d["environment"], d)
    for a in s.get("tool_always_allow") or []:
        add("tool_always_allow", f"{a.get('server') or '*'}/{a['tool']}/{a.get('schema_sha256') or ''}", a)
    return ops


def _same(a: Any, b: Any) -> bool:
    return canonical(a) == canonical(b)


def _current(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """The target project as the bundle sees it, for the diff."""
    secrets: dict[str, dict[str, Any]] = {}
    return _scrub(_sections(connection, settings, project_id, secrets, {}), "", [])


def _diff_action(op: dict[str, Any], now: dict[str, Any]) -> str:
    section, key, data = op["section"], op["key"], op["data"]
    if section == "model_settings":
        cur = now["model_settings"]
        return "unchanged" if key in cur and _same(cur[key], data) else (
            "update" if key in cur else "create")
    if section == "routing_policy":
        cur = now["routing_policy"]
        return "create" if not cur else ("unchanged" if _same(cur, data) else "update")
    if section == "role_reports_to":
        role = next((r for r in now["roles"] if r["role_key"] == key), None)
        return "unchanged" if role and role.get("reports_to") == data else "update"
    index = {
        "packs": ("pack_id", now["packs"]), "roles": ("role_key", now["roles"]),
        "skills": ("skill_key", now["skills"]), "mcp_servers": ("name", now["mcp_servers"]),
        "custom_steps": ("step_key", now["custom_steps"]),
        "workflows": ("workflow_key", now["workflows"]),
        "deploy_policies": ("environment", now["deploy_policies"]),
    }
    if section in index:
        field, rows = index[section]
        match = next((r for r in rows if str(r.get(field)).lower() == str(key).lower()), None)
        if match is None:
            return "create"
        if section == "custom_steps":
            return "unchanged" if _same(match, data) else "exists"
        return "unchanged" if _same(match, data) else "update"
    if section == "role_skills":
        return "unchanged" if any(_same(r, data) for r in now["role_skills"]) else "create"
    if section == "role_mcp":
        match = next((r for r in now["role_mcp"] if r["role_key"] == data["role_key"]
                      and r["server"] == data["server"]), None)
        return "create" if match is None else ("unchanged" if _same(match, data) else "update")
    if section == "tool_always_allow":
        return "unchanged" if any(_same(r, data) for r in now["tool_always_allow"]) else "create"
    return "create"


# ------------------------------------------------------------------------------------------ #
# import: replay through the existing services
# ------------------------------------------------------------------------------------------ #

def _server_id(connection: Connection, project_id: str, name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    from .mcp_registry import find_mcp_server

    found = find_mcp_server(connection, project_id, name)
    if found is None:
        raise PortabilityError(f"no MCP server {name!r} in the target project")
    return str(found["id"])


def _apply_op(connection: Connection, settings: Any, project_id: str, op: dict[str, Any],
              actor: str, action: str) -> dict[str, Any]:
    """Replay one item through the service the console uses for it. Returns a note."""
    section, key, data = op["section"], op["key"], op["data"]
    if action == "unchanged":
        return {"result": "unchanged"}
    if section == "model_settings":
        from . import project_settings

        project_settings.set_one(connection, project_id, key, data, actor, scope="project")
    elif section == "routing_policy":
        from .model_router.store import save_policy

        save_policy(connection, project_id, data, actor=actor, settings=settings)
    elif section == "packs":
        from .specpack import SpecPackService

        svc = SpecPackService(settings)
        if data.get("yaml"):
            svc.import_pack(connection, project_id, data["yaml"], actor)
        else:
            svc.install_pack(connection, project_id, data["pack_id"], actor)
    elif section == "roles":
        from .specpack import SpecPackService

        SpecPackService(settings).register_role(
            connection, project_id, key, display_name=data.get("display_name"),
            autonomy_level=data.get("autonomy_level"), spec=data.get("spec") or {},
            reports_to_given=False, actor=actor)
    elif section == "role_reports_to":
        from .specpack import SpecPackService

        SpecPackService(settings).register_role(connection, project_id, key, reports_to=data,
                                                reports_to_given=True, actor=actor)
    elif section == "skills":
        from .skill_definitions import save_skill

        save_skill(connection, project_id=project_id, actor=actor, mode="upsert",
                   **{k: data[k] for k in ("skill_key", "label", "behaviour_kind", "summary",
                                           "knowledge", "inputs", "produces", "config",
                                           "risk_tier", "trusted", "enabled") if k in data})
    elif section == "mcp_servers":
        from . import mcp_registry

        auth = {k: v for k, v in (data.get("auth") or {}).items()
                if k in ("type", "header_name")}
        if auth.get("type") in ("bearer", "header") and not (data.get("auth") or {}).get("secret"):
            auth = {"type": "none"}
        existing = mcp_registry.find_mcp_server(connection, project_id, key)
        fields = {k: data[k] for k in ("url", "transport", "description", "risk_tier",
                                       "data_boundary", "tool_risk_overrides", "enabled")
                  if k in data}
        if existing is None:
            mcp_registry.register_mcp_server(
                connection, project_id, key, data["url"], actor=actor, settings=settings,
                **{k: v for k, v in fields.items() if k != "url"}, auth=auth)
            existing = mcp_registry.find_mcp_server(connection, project_id, key)
        else:
            mcp_registry.update_mcp_server(connection, project_id, existing["id"], actor=actor,
                                           settings=settings, **fields)
        for tool, policy in sorted((data.get("tool_policy") or {}).items()):
            mcp_registry.set_tool_policy(connection, project_id, existing["id"], tool, policy,
                                         actor=actor)
        if (data.get("auth") or {}).get("secret"):
            return {"result": "applied",
                    "needs_secret": (data["auth"]["secret"].get(PLACEHOLDER_KEY) or {}).get("name")}
    elif section == "role_skills":
        from .skill_definitions import assign_skill_to_role

        assign_skill_to_role(connection, project_id=project_id, actor=actor,
                             role_key=data["role_key"], skill_id=data["skill_id"])
    elif section == "role_mcp":
        from .agents import grant_mcp

        grant_mcp(connection, project_id, actor=actor, role_key=data["role_key"],
                  mcp_server_id=_server_id(connection, project_id, data["server"]),
                  tools=data.get("tools") or None)
    elif section == "custom_steps":
        if action == "exists":
            return {"result": "kept", "note": "a custom step with this key already exists"}
        from . import workflow_custom_steps

        workflow_custom_steps.create(
            connection, project_id=project_id, label=data["label"],
            binding_kind=data["binding_kind"], actor=actor, summary=data.get("summary") or "",
            skill_id=data.get("skill_id"),
            mcp_server_id=_server_id(connection, project_id, data.get("server")),
            mcp_tool_name=data.get("mcp_tool_name"), inputs=data.get("inputs"),
            produces=data.get("produces"), knowledge=data.get("knowledge") or "",
            step_key=data["step_key"])
    elif section == "workflows":
        from . import workflows

        found = connection.execute(text(
            "SELECT id, namespace FROM workflow_definitions WHERE project_id = :p "
            "AND workflow_key = :k"), {"p": project_id, "k": key}).mappings().first()
        if found is None:
            created = workflows.create_definition(
                connection, project_id=project_id, workflow_key=key, name=data["name"],
                description=data.get("description"), actor=actor,
                namespace=workflows.PRODUCT_NAMESPACE)
            definition_id = created["id"]
        elif found["namespace"] != workflows.PRODUCT_NAMESPACE:
            raise PortabilityError(f"{key!r} is one of BOSS's own workflows on the target")
        else:
            definition_id = found["id"]
        published = workflows.publish_version(connection, project_id=project_id,
                                              definition_id=definition_id,
                                              spec=copy.deepcopy(data["spec"]), actor=actor)
        if published.get("duplicate"):
            return {"result": "unchanged"}
    elif section == "deploy_policies":
        from .deploy_policy import set_approval_policy

        set_approval_policy(connection, project_id=project_id, environment=data["environment"],
                            requires_approval=bool(data["requires_approval"]), actor=actor,
                            note="imported")
    elif section == "tool_always_allow":
        from .tool_policies import grant_always_allow

        grant_always_allow(connection, project_id=project_id,
                           server_id=_server_id(connection, project_id, data.get("server")),
                           tool=data["tool"], schema_sha256=data.get("schema_sha256"),
                           source_decision_id=None)
    else:
        raise PortabilityError(f"unknown section {section!r}")
    return {"result": "applied"}


def _replay(connection: Connection, settings: Any, project_id: str,
            ops: Iterable[dict[str, Any]], actor: str) -> list[dict[str, Any]]:
    """Each op in its own savepoint: one that fails is named, the rest still run."""
    results = []
    for op in ops:
        try:
            with connection.begin_nested():
                note = _apply_op(connection, settings, project_id, op, actor, op["action"])
            results.append({"op_id": op["op_id"], "action": op["action"], **note})
        except Exception as exc:  # noqa: BLE001 — every service raises its own error type
            results.append({"op_id": op["op_id"], "action": op["action"], "result": "error",
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return results


def _target_projects(connection: Connection, scope: str, target_id: str,
                     bundle: dict[str, Any]) -> list[tuple[dict[str, Any], Optional[str]]]:
    """[(bundle project, target project id or None)]."""
    projects = bundle["projects"]
    if scope == "project":
        if len(projects) != 1:
            raise PortabilityError("a project import takes a bundle of exactly one project; "
                                   "import an org bundle at the org")
        _project_ref(connection, target_id)
        return [(projects[0], target_id)]
    from . import org_models

    ids = org_models.org_projects(connection, target_id)
    by_slug = {}
    for pid in ids:
        by_slug[_project_ref(connection, pid)["slug"]] = pid
    return [(p, by_slug.get(p["ref"]["slug"])) for p in projects]


def _plan(connection: Connection, settings: Any, scope: str, target_id: str,
          bundle: dict[str, Any]) -> dict[str, Any]:
    plans = []
    for project, pid in _target_projects(connection, scope, target_id, bundle):
        ops = _ops_for(project)
        if pid is None:
            plans.append({"slug": project["ref"]["slug"], "project_id": None, "ops": [],
                          "skipped": ("no project with this slug in the target org; create it, "
                                      "then import again")})
            continue
        now = _current(connection, settings, pid)
        for op in ops:
            op["action"] = _diff_action(op, now)
        plans.append({"slug": project["ref"]["slug"], "project_id": pid, "ops": ops})
    org_ops = []
    if scope == "org" and bundle.get("org"):
        from . import org_models

        for knob, value in sorted((bundle["org"].get("model_settings") or {}).items()):
            row = org_models.org_row(connection, target_id, knob)
            action = "create" if row is None else (
                "unchanged" if _same(row.get("value"), value) else "update")
            org_ops.append({"op_id": f"org_model_settings:{knob}", "section": "org_model_settings",
                            "key": knob, "data": value, "action": action})
    return {"projects": plans, "org_ops": org_ops}


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {}
    for op in [o for p in plan["projects"] for o in p["ops"]] + plan["org_ops"]:
        counts.setdefault(op["section"], {}).setdefault(op["action"], 0)
        counts[op["section"]][op["action"]] += 1
    return counts


def _chunks(plan: dict[str, Any], size: int) -> list[list[tuple[Optional[str], dict[str, Any]]]]:
    flat: list[tuple[Optional[str], dict[str, Any]]] = [(None, op) for op in plan["org_ops"]]
    for p in plan["projects"]:
        flat += [(p["project_id"], op) for op in p["ops"]]
    return [flat[i:i + size] for i in range(0, len(flat), size)] or [[]]


def _apply_org_op(connection: Connection, target_org: str, op: dict[str, Any],
                  actor: str) -> dict[str, Any]:
    from . import org_models

    if op["action"] == "unchanged":
        return {"result": "unchanged"}
    org_models.write_org(connection, target_org, op["key"], op["data"], actor)
    org_models.invalidate_org(connection, target_org)
    return {"result": "applied"}


def _run_chunk(connection: Connection, settings: Any, scope: str, target_id: str,
               chunk: list[tuple[Optional[str], dict[str, Any]]], actor: str) -> list[dict[str, Any]]:
    results = []
    for pid, op in chunk:
        if pid is None:
            try:
                with connection.begin_nested():
                    note = _apply_org_op(connection, target_id, op, actor)
                results.append({"op_id": op["op_id"], "action": op["action"], **note})
            except Exception as exc:  # noqa: BLE001
                results.append({"op_id": op["op_id"], "action": op["action"],
                                "result": "error", "error": f"{type(exc).__name__}: {exc}"[:300]})
        else:
            results += [{**r, "project_id": pid}
                        for r in _replay(connection, settings, pid, [op], actor)]
    return results


def plan_import(engine: Engine, settings: Any, *, scope: str, target_id: str,
                bundle: dict[str, Any], actor: str, chunk_size: int = CHUNK_SIZE) -> dict[str, Any]:
    """THE DRY RUN: diff + validation replay (rolled back). Records a `planned` import."""
    manifest = validate_bundle(bundle)
    with engine.connect() as connection:
        trans = connection.begin()
        try:
            plan = _plan(connection, settings, scope, target_id, bundle)
            validation: list[dict[str, Any]] = []
            for chunk in _chunks(plan, chunk_size):
                validation += _run_chunk(connection, settings, scope, target_id, chunk, actor)
        finally:
            trans.rollback()      # a dry run writes nothing but its own record, below
    errors = [r for r in validation if r["result"] == "error"]
    needs_secret = sorted({r["needs_secret"] for r in validation if r.get("needs_secret")})
    report = {
        "dry_run": True, "summary": _summary(plan), "errors": errors,
        "skipped_projects": [{"slug": p["slug"], "why": p["skipped"]}
                             for p in plan["projects"] if p.get("skipped")],
        "secrets_to_provide": bundle.get("secrets") or [],
        "needs_secret": needs_secret,
        "fidelity": {"source": bundle.get("fidelity") or {},
                     "clean": not errors},
        "source": manifest.get("source"),
    }
    import_id = "pim_" + hashlib.sha256(
        f"{scope}:{target_id}:{checksum(bundle)}:{_now()}".encode()).hexdigest()[:16]
    chunks_total = len(_chunks(plan, chunk_size))
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO portability_imports (id, scope, target_id, bundle_checksum, bundle, "
            "status, chunk_size, chunks_total, chunks_done, plan, report, created_by) VALUES "
            "(:i, :s, :t, :ck, CAST(:b AS JSONB), 'planned', :cs, :ct, 0, CAST(:p AS JSONB), "
            "CAST(:r AS JSONB), :by)"),
            {"i": import_id, "s": scope, "t": target_id, "ck": checksum(bundle),
             "b": canonical(bundle), "cs": chunk_size, "ct": chunks_total,
             "p": canonical(plan), "r": canonical(report), "by": actor})
    return {"import_id": import_id, "status": "planned", "chunks_total": chunks_total,
            "plan": plan, **report}


def apply_import(engine: Engine, settings: Any, *, import_id: str, actor: str,
                 max_chunks: Optional[int] = None,
                 on_chunk: Optional[Callable[[int], None]] = None) -> dict[str, Any]:
    """Apply a planned import, one chunk per transaction, from where it stopped.

    The plan is computed again against the target as it is NOW (so an item a previous, partly
    finished apply created is `unchanged`), but the op list and its chunks are the bundle's own
    and do not move — chunks_done stays a valid resume point. `max_chunks` stops early (a
    caller that wants to do the rest later, and the tests)."""
    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM portability_imports WHERE id = :i FOR UPDATE"),
                        {"i": import_id}).mappings().first()
        if row is None:
            raise PortabilityError(f"no import {import_id!r}; run the dry run first")
        if row["status"] == "applied":
            return {"import_id": import_id, "status": "applied",
                    "report": _json(row["report"], {})}
        c.execute(text("UPDATE portability_imports SET status = 'applying', updated_at = now() "
                       "WHERE id = :i"), {"i": import_id})
    bundle = _json(row["bundle"], {})
    validate_bundle(bundle)
    scope, target_id, size = row["scope"], row["target_id"], int(row["chunk_size"])
    done = int(row["chunks_done"])
    report = _json(row["report"], {}) or {}
    results: list[dict[str, Any]] = list(report.get("applied") or [])
    ran = 0
    with engine.connect() as probe:
        chunks = _chunks(_plan(probe, settings, scope, target_id, bundle), size)
        probe.rollback()
    for index in range(done, len(chunks)):
        if max_chunks is not None and ran >= max_chunks:
            break
        with engine.begin() as c:
            # Re-diff only this chunk's items against the target as it stands now.
            chunk = []
            fresh = _plan(c, settings, scope, target_id, bundle)
            by_id = {(p["project_id"], o["op_id"]): o for p in fresh["projects"] for o in p["ops"]}
            by_id.update({(None, o["op_id"]): o for o in fresh["org_ops"]})
            for pid, op in chunks[index]:
                chunk.append((pid, by_id.get((pid, op["op_id"]), op)))
            results += _run_chunk(c, settings, scope, target_id, chunk, actor)
            c.execute(text(
                "UPDATE portability_imports SET chunks_done = :d, report = CAST(:r AS JSONB), "
                "updated_at = now() WHERE id = :i"),
                {"d": index + 1, "i": import_id,
                 "r": canonical({**report, "applied": results})})
        ran += 1
        if on_chunk is not None:
            on_chunk(index)
    finished = done + ran >= len(chunks)
    errors = [r for r in results if r["result"] == "error"]
    status = ("failed" if errors else "applied") if finished else "applying"
    final = {**report, "dry_run": False, "applied": results, "errors": errors,
             "fidelity": {**(report.get("fidelity") or {}), "clean": not errors}}
    with engine.begin() as c:
        c.execute(text("UPDATE portability_imports SET status = :s, report = CAST(:r AS JSONB), "
                       "updated_at = now() WHERE id = :i"),
                  {"s": status, "r": canonical(final), "i": import_id})
    return {"import_id": import_id, "status": status, "chunks_total": len(chunks),
            "chunks_done": min(len(chunks), done + ran), "report": final}


def get_import(connection: Connection, import_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, scope, target_id, bundle_checksum, status, chunk_size, chunks_total, "
        "chunks_done, report, created_by, created_at, updated_at FROM portability_imports "
        "WHERE id = :i"), {"i": import_id}).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["report"] = _json(out["report"], {})
    for k in ("created_at", "updated_at"):
        out[k] = out[k].isoformat() if out[k] else None
    return out
