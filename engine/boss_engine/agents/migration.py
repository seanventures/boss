"""The shipped YAMLs and every existing agent, as `boss.agent/v1` (AGENT_PLATFORM §4.1, D2).

* `role_yaml_to_doc` — a shipped role (packages/specpack/roles/*.yaml) as a v1 document. The
  YAML stays the shipped form; this is its mechanical conversion, proven for every file by
  tests/test_agent_spec.py.
* `studio_to_doc` — a Studio profile (packages/specpack/agents/*.yaml, or an operator's Studio
  agent read from agent_definitions/agent_versions) as a v1 PROFILE: `metadata.extends` names
  the role it runs as. The `deterministic_brief` execution mode is not carried: it is retired.
* `sync_project` — at boot, every role row of a project becomes (or stays) its active version,
  read from the row AND its holdings, so no grant is lost; every Studio profile with a published
  version gets its v1 profile version. Idempotent: a second boot finds every hash equal and writes
  nothing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from . import spec as S
from . import versions as V

_log = logging.getLogger(__name__)


def specpack_root() -> Path:
    from ..services.agent_persona import _specpack_root

    return Path(_specpack_root())


def _baseline() -> set[str]:
    from ..services.agent_baseline import BASELINE_SKILLS

    return set(BASELINE_SKILLS)


def role_yaml_to_doc(role: dict[str, Any]) -> dict[str, Any]:
    """A shipped role YAML as a canonical v1 document."""
    perms = role.get("permissions") or {}
    autonomy = dict(role.get("autonomy") or {})
    comms = dict(role.get("comms_contract") or {})
    doc = {
        "apiVersion": S.API_VERSION, "kind": S.KIND,
        "metadata": {"key": role["role_id"], "name": role.get("display_name"), "team": role.get("team")},
        "spec": {
            "persona": role.get("persona") or {},
            "mission": role.get("mission") or "",
            "outcomes": list(role.get("primary_outcomes") or []),
            "boundaries": dict(role.get("boundaries") or {}),
            "decision_rights": dict(role.get("decision_rights") or {}),
            "tools": {
                "skills": [s for s in perms.get("allowed_skills") or [] if s not in _baseline()],
                "forbidden": list(perms.get("forbidden_skills") or []),
                "abilities": list(perms.get("abilities") or []),
                "recommended_abilities": list(perms.get("recommended_abilities") or []),
                "mcp": [{"server": m["server"], "tools": list(m.get("tools") or [])}
                        for m in perms.get("mcp_servers") or []],
            },
            "autonomy": autonomy,
            "output_contract": {"artifacts": list(role.get("output_contracts") or [])},
            "cadence": {str(k): str(v) for k, v in (role.get("cadence") or {}).items()},
            "comms": comms,
            "evaluators": ([{"kind": "rubric", "criteria": dict(role["eval_rubric"])}]
                           if role.get("eval_rubric") else []),
        },
    }
    return S.dump_spec(S.load_spec(S._strip(doc)))  # noqa: SLF001


def studio_to_doc(key: str, name: str, description: str, owner: Optional[str],
                  version: dict[str, Any], template: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A Studio profile (YAML payload or agent_versions row) as a canonical v1 PROFILE."""
    mp = dict(version.get("model_policy") or {})
    prompts = dict(version.get("prompts") or {})
    oc = dict(version.get("output_contract") or {})
    limits = {k: v for k, v in dict(version.get("limits") or {}).items()
              if k in ("max_steps", "max_calls", "timeout_seconds", "max_retries")}
    labels = {k: str(v) for k, v in {"owner": owner, **{k: (template or {}).get(k) for k in
                                                         ("category", "readiness", "introduced_in")}}.items() if v}
    doc = {
        "apiVersion": S.API_VERSION, "kind": S.KIND,
        "metadata": {"key": key, "name": name, "description": description, "extends": version["role_key"],
                     "labels": labels},
        "spec": {
            "prompts": {k: str(prompts[k]) for k in ("system", "job") if prompts.get(k)},
            "instructions": [str(r) for r in version.get("rules") or []],
            "model": {"tier": "balanced",
                      "need": [str(c) for c in mp.get("preferred_capabilities") or []]},
            "tools": {"skills": list(version.get("requested_skills") or []),
                      "integrations": list(version.get("requested_integrations") or [])},
            "scopes": {str(k): str(v) for k, v in dict(version.get("scopes") or {}).items()},
            "limits": limits,
            "output_contract": {k: oc[k] for k in ("artifact_type", "sections", "schema") if k in oc},
            "a2a": {k: list(v) for k, v in dict(version.get("a2a_policy") or {}).items()
                    if k in ("publish", "subscribe")},
        },
    }
    return S.dump_spec(S.load_spec(S._strip(doc)))  # noqa: SLF001


def shipped_roles() -> dict[str, dict[str, Any]]:
    out = {}
    for path in sorted((specpack_root() / "roles").glob("*.yaml")):
        out[path.stem] = yaml.safe_load(path.read_text())
    return out


def shipped_profiles() -> dict[str, dict[str, Any]]:
    out = {}
    for path in sorted((specpack_root() / "agents").glob("*.yaml")):
        out[path.stem] = yaml.safe_load(path.read_text())
    return out


def convert_shipped() -> dict[str, Any]:
    """Every shipped YAML as a v1 document: {roles: {key: doc}, profiles: {key: doc}, failed: [...]}."""
    roles, profiles, failed = {}, {}, []
    for key, role in shipped_roles().items():
        try:
            roles[key] = role_yaml_to_doc(role)
        except S.SpecError as exc:
            failed.append({"file": f"roles/{key}.yaml", "errors": exc.errors})
    for key, p in shipped_profiles().items():
        try:
            profiles[key] = studio_to_doc(p["id"], p["name"], p.get("description") or "", p.get("owner"),
                                          p["version"], p.get("template"))
        except S.SpecError as exc:
            failed.append({"file": f"agents/{key}.yaml", "errors": exc.errors})
    return {"roles": roles, "profiles": profiles, "failed": failed}


def _studio_profiles(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT d.agent_key, d.name, d.description, d.owner, d.source, v.role_key, v.model_policy, "
                "v.prompts, v.rules, v.requested_skills, v.requested_integrations, v.scopes, v.limits, "
                "v.output_contract, v.a2a_policy, v.version FROM agent_definitions d "
                "JOIN agent_versions v ON v.id = d.active_version_id WHERE d.project_id = :p "
                "AND d.status <> 'retired' ORDER BY d.agent_key"), {"p": project_id}).mappings().all()
    except Exception:  # noqa: BLE001 — no Studio tables on this database
        return []
    return [dict(r) for r in rows]


def sync_project(connection: Connection, project_id: str, *, actor: str = "boss") -> dict[str, Any]:
    """Every role row of the project as its active v1 version, and every Studio profile as its v1
    profile. Counts: {agents: {versioned, unchanged, failed}, profiles: {...}}."""
    shipped = set(shipped_roles().keys())
    out: dict[str, Any] = {"agents": {"versioned": [], "unchanged": 0, "failed": []},
                           "profiles": {"versioned": [], "unchanged": 0, "failed": []}}
    keys = [str(k) for k in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p ORDER BY role_key"), {"p": project_id}).scalars()]
    for key in keys:
        try:
            with connection.begin_nested():
                had = V.active(connection, project_id, key)
                source = ("shipped" if key in shipped else "migration") if had is None else "legacy_edit"
                got = V.snapshot(connection, project_id, key, actor=actor, source=source,
                                 provenance={"from": "roles_row", **({"shipped": f"roles/{key}.yaml"}
                                                                     if key in shipped else {})})
            if got is None:
                out["agents"]["unchanged"] += 1
            else:
                out["agents"]["versioned"].append(key)
        except Exception as exc:  # noqa: BLE001 — one malformed row must not stop the rest
            out["agents"]["failed"].append({"key": key, "error": str(exc)[:300]})
    profiles = shipped_profiles()
    for p in _studio_profiles(connection, project_id):
        key = p["agent_key"]
        try:
            template = (profiles.get(key) or {}).get("template")
            doc = studio_to_doc(key, p["name"], p.get("description") or "", p.get("owner"), p, template)
            with connection.begin_nested():
                saved = V.save(connection, project_id, doc, actor=actor,
                               source="shipped" if p.get("source") == "specpack" else "migration",
                               provenance={"from": "agent_versions", "studio_version": p["version"]})
                current = V.active(connection, project_id, key, kind="profile")
                if current is not None and current["id"] == saved["id"]:
                    out["profiles"]["unchanged"] += 1
                    continue
                V.publish(connection, project_id, key, saved["version"], actor=actor, kind="profile",
                          reason="migration")
            out["profiles"]["versioned"].append(key)
        except Exception as exc:  # noqa: BLE001
            out["profiles"]["failed"].append({"key": key, "error": str(exc)[:300]})
    return out


def sync_all(engine: Engine, *, actor: str = "boss", project_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """`sync_project` for every project, one transaction each (a project another replica is
    syncing is skipped, not raced)."""
    if project_ids is None:
        with engine.connect() as connection:
            if connection.execute(text("SELECT to_regclass('agent_spec_versions')")).scalar() is None:
                return {}
            project_ids = [str(p) for p in connection.execute(text("SELECT id FROM projects ORDER BY id")).scalars()]
    results: dict[str, Any] = {}
    for project_id in project_ids:
        try:
            with engine.begin() as connection:
                if not connection.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                                          {"k": f"boss.agent_spec_sync:{project_id}"}).scalar():
                    continue
                results[project_id] = sync_project(connection, project_id, actor=actor)
        except Exception:  # noqa: BLE001
            _log.warning("agent spec sync failed for %s", project_id, exc_info=True)
    return results


__all__ = ["convert_shipped", "role_yaml_to_doc", "shipped_profiles", "shipped_roles", "studio_to_doc",
           "sync_all", "sync_project"]
