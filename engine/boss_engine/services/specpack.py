from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .community_edition import CommunityEditionService
from .core import create_artifact, emit_event, json_hash
from .receipts import ReceiptService
from .skill_scope import matching_skill_ids


class PackNotFoundError(Exception):
    pass


class RoleNotFoundError(Exception):
    pass


class AgentModuleNotFoundError(Exception):
    pass


class AgentModuleEntitlementError(Exception):
    pass


class AgentModuleRollbackError(Exception):
    pass


class SpecPackService:
    DEFAULT_AGENT_MODULE_IDS = [
        "metatron_operator_v1",
        "coo_operator_v1",
        "cao_architect_v1",
        "cto_planner_v1",
        "pm_product_planner_v1",
        "qa_gatekeeper_v1",
        "release_manager_v1",
        "program_manager_v1",
        "vp_agents_governor_v1",
        "providerops_manager_v1",
        "finops_sentinel_v1",
        "resource_janitor_v1",
        "infra_steward_v1",
        "deployment_manager_v1",
        "secrets_steward_v1",
        "model_spend_analyst_v1",
        "codex_software_engineer_v1",
    ]

    def __init__(
        self,
        settings: Settings,
        *,
        edition_service: CommunityEditionService | None = None,
    ):
        self.settings = settings
        self.edition_service = edition_service or CommunityEditionService(settings)
        self.receipts = ReceiptService()
        self._skill_ids_cache: set[str] | None = None

    def _project_scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.workspace_id, w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            return {
                "org_id": self.settings.default_org_id,
                "workspace_id": self.settings.default_workspace_id,
            }
        return {"org_id": row["org_id"], "workspace_id": row["workspace_id"]}

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path)
        # Parsed once per version of the file (yaml_files).
        from .yaml_files import load_mapping

        return load_mapping(path)

    def _pack_path(self, pack_id: str) -> Path:
        return self.settings.specpack_root / "packs" / f"{pack_id}.yaml"

    def _role_path(self, role_key: str) -> Path:
        return self.settings.specpack_root / "roles" / f"{role_key}.yaml"

    def _module_path(self, module_id: str) -> Path:
        return self.settings.specpack_root / "modules" / f"{module_id}.yaml"

    def load_pack_spec(self, pack_id: str) -> dict[str, Any]:
        try:
            payload = self._load_yaml(self._pack_path(pack_id))
        except FileNotFoundError as error:
            raise PackNotFoundError(f"Pack '{pack_id}' not found") from error
        payload.setdefault("id", pack_id)
        payload.setdefault("requires", [])
        payload.setdefault("installs", {})
        payload.setdefault("status", "draft")
        return payload

    def load_role_spec(self, role_key: str) -> dict[str, Any]:
        try:
            payload = self._load_yaml(self._role_path(role_key))
        except FileNotFoundError as error:
            raise RoleNotFoundError(f"Role '{role_key}' not found") from error
        payload.setdefault("role_id", role_key)
        payload.setdefault("display_name", role_key.replace("_", " ").title())
        self.validate_role_spec(payload, f"roles/{role_key}.yaml")
        payload["skill_reference_diagnostics"] = (
            self.skill_reference_diagnostics_for_role(payload)
        )
        return payload

    def _catalog_skill_ids(self) -> set[str]:
        if self._skill_ids_cache is not None:
            return set(self._skill_ids_cache)
        skill_ids: set[str] = set()
        for path in sorted((self.settings.specpack_root / "skills").glob("*.yaml")):
            payload = self._load_yaml(path)
            for skill in payload.get("skills") or []:
                if isinstance(skill, dict) and isinstance(skill.get("id"), str):
                    skill_ids.add(skill["id"])
        self._skill_ids_cache = skill_ids
        return set(skill_ids)

    def skill_reference_diagnostics_for_role(
        self,
        role_spec: dict[str, Any],
        *,
        runtime: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        catalog_ids = self._catalog_skill_ids()
        permissions = role_spec.get("permissions") or {}
        references: list[tuple[str, str]] = []
        for field in ("allowed_skills", "forbidden_skills"):
            references.extend(
                (field, item.strip())
                for item in permissions.get(field) or []
                if isinstance(item, str) and item.strip()
            )
        references.extend(
            ("runtime.default_skill_ids", item.strip())
            for item in (runtime or {}).get("default_skill_ids") or []
            if isinstance(item, str) and item.strip()
        )
        diagnostics: list[dict[str, Any]] = []
        for field, reference in references:
            matches = sorted(matching_skill_ids(reference, catalog_ids))
            status = (
                "unresolved"
                if not matches
                else "exact"
                if reference in catalog_ids
                else "wildcard"
                if reference.endswith(".*")
                else "legacy_alias"
            )
            if status in {"unresolved", "legacy_alias"}:
                diagnostics.append(
                    {
                        "field": field,
                        "reference": reference,
                        "status": status,
                        "matches": matches,
                    }
                )
        return diagnostics

    def skill_reference_diagnostics_for_module(
        self, module_spec: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        role_key, role_spec = self._normalize_role_spec_from_module(module_spec)
        return role_key, list(role_spec.get("skill_reference_diagnostics") or [])

    def validate_role_spec(
        self, role_spec: dict[str, Any], source: str = "RoleSpec"
    ) -> None:
        errors: list[str] = []
        for key in ("role_id", "display_name", "mission"):
            if not isinstance(role_spec.get(key), str) or not role_spec[key].strip():
                errors.append(f"{key} must be a non-empty string")

        self._require_string_list(
            role_spec, "primary_outcomes", errors, allow_empty=False
        )
        self._require_string_list(
            role_spec, "output_contracts", errors, allow_empty=False
        )

        boundaries = role_spec.get("boundaries")
        if not isinstance(boundaries, dict):
            errors.append("boundaries must be an object")
        else:
            self._require_string_list(boundaries, "must", errors, "boundaries")
            self._require_string_list(boundaries, "must_not", errors, "boundaries")

        decision_rights = role_spec.get("decision_rights")
        if not isinstance(decision_rights, dict):
            errors.append("decision_rights must be an object")
        else:
            self._require_string_list(
                decision_rights, "can_decide", errors, "decision_rights"
            )
            self._require_string_list(
                decision_rights, "must_escalate", errors, "decision_rights"
            )

        permissions = role_spec.get("permissions")
        if not isinstance(permissions, dict):
            errors.append("permissions must be an object")
        else:
            self._require_string_list(permissions, "allowed_skills", errors, "permissions")
            self._require_string_list(
                permissions, "forbidden_skills", errors, "permissions"
            )

        for key in ("cadence", "comms_contract", "eval_rubric"):
            value = role_spec.get(key)
            if not isinstance(value, dict) or not value:
                errors.append(f"{key} must be a non-empty object")

        autonomy = role_spec.get("autonomy")
        if not isinstance(autonomy, dict):
            errors.append("autonomy must be an object")
        else:
            level = autonomy.get("level")
            if not isinstance(level, int) or level < 0:
                errors.append("autonomy.level must be a non-negative integer")

        # A pack's persona is held to the same lint as one typed into the console: it reaches
        # the dock, the work prompt and every panel seat, so a pack cannot be the way round it.
        persona = role_spec.get("persona")
        if persona is not None:
            from .agent_persona import lint_persona

            for problem in lint_persona(persona)[0]:
                errors.append(f"persona.{problem['field']}: {problem['message']}")

        if errors:
            raise ValueError(f"{source} is invalid: {'; '.join(errors)}")

    def validate_pack_spec(
        self, pack_spec: dict[str, Any], source: str = "PackSpec"
    ) -> None:
        errors: list[str] = []
        for key in ("id", "version", "description"):
            val = pack_spec.get(key)
            if not isinstance(val, str) or not val.strip():
                errors.append(f"{key} must be a non-empty string")
        installs = pack_spec.get("installs")
        if not isinstance(installs, dict):
            errors.append("installs must be an object")
        else:
            for k in ("roles", "workflows", "policies", "skills", "canaries"):
                lst = installs.get(k, [])
                if not isinstance(lst, list):
                    errors.append(f"installs.{k} must be a list")
                elif any(not isinstance(x, str) or not x.strip() for x in lst):
                    errors.append(f"installs.{k} must contain only non-empty strings")
        requires = pack_spec.get("requires", [])
        if not isinstance(requires, list) or any(
            not isinstance(x, str) or not x.strip() for x in requires
        ):
            errors.append("requires must be a list of non-empty strings")
        # A TEAM (BOSSAPP-894): reporting lines and starter work, both optional. A pack without
        # them is validated exactly as before.
        from .team_templates import has_team, validate_team

        if has_team(pack_spec):
            errors.extend(validate_team(pack_spec))
        status = pack_spec.get("status")
        if status is not None and status not in ("draft", "active", "deprecated"):
            errors.append("status must be draft/active/deprecated or omitted")
        if errors:
            raise ValueError(f"{source} is invalid: {'; '.join(errors)}")

    def _require_string_list(
        self,
        payload: dict[str, Any],
        key: str,
        errors: list[str],
        namespace: str | None = None,
        *,
        allow_empty: bool = True,
    ) -> None:
        label = f"{namespace}.{key}" if namespace else key
        value = payload.get(key)
        if not isinstance(value, list):
            errors.append(f"{label} must be a list")
            return
        if not allow_empty and not value:
            errors.append(f"{label} must not be empty")
            return
        if any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"{label} must contain only non-empty strings")

    def load_agent_module_spec(self, module_id: str) -> dict[str, Any]:
        try:
            payload = self._load_yaml(self._module_path(module_id))
        except FileNotFoundError as error:
            raise AgentModuleNotFoundError(
                f"Agent module '{module_id}' not found"
            ) from error
        payload.setdefault("id", module_id)
        payload.setdefault("version", "0.0.0")
        payload.setdefault("display_name", module_id.replace("_", " ").title())
        payload.setdefault("description", "")
        payload.setdefault("publisher", "Unknown")
        payload.setdefault("category", "general")
        payload.setdefault("status", "draft")
        payload.setdefault("marketplace", {})
        payload.setdefault("prompt_bundle", {})
        payload.setdefault("runtime", {})
        payload.setdefault("role", {})
        return payload

    def _normalize_role_spec_from_module(
        self, module_spec: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        role_spec = dict(module_spec.get("role") or {})
        role_key = role_spec.get("role_key") or role_spec.get("role_id")
        if not isinstance(role_key, str) or not role_key.strip():
            raise ValueError("Agent module role.role_key must be a non-empty string")
        role_key = role_key.strip()
        role_spec["role_id"] = role_key
        role_spec["display_name"] = role_spec.get(
            "display_name", role_key.replace("_", " ").title()
        )
        role_spec.setdefault("mission", "")
        role_spec.setdefault("primary_outcomes", [])
        role_spec.setdefault("boundaries", {})
        role_spec.setdefault("permissions", {})
        role_spec.setdefault("output_contracts", [])
        role_spec.setdefault("autonomy", {"level": 0})
        # WHO THE AGENT IS TRAVELS WITH THE ROLE, NOT THE MODULE. A default agent module
        # (pm_product_planner_v1, qa_gatekeeper_v1, …) replaces its role's spec on install, and
        # modules carry no persona and no MCP grants — so Juno and Vera came out of every
        # install voiceless and holding nothing. The role YAML's persona and MCP servers are
        # kept unless the module states its own.
        shipped = self._shipped_role_identity(role_key)
        if shipped.get("persona") and "persona" not in role_spec:
            role_spec["persona"] = shipped["persona"]
        if shipped.get("team") and not str(role_spec.get("team") or "").strip():
            role_spec["team"] = shipped["team"]
        if shipped.get("mcp_servers"):
            permissions = dict(role_spec.get("permissions") or {})
            permissions.setdefault("mcp_servers", shipped["mcp_servers"])
            role_spec["permissions"] = permissions
        # AND ITS KIT LOADOUT (skills-mcp §6.2): the abilities the role names travel with the
        # role too, or a module-installed Juno would hold none of hers.
        for field in ("abilities", "recommended_abilities"):
            if shipped.get(field):
                permissions = dict(role_spec.get("permissions") or {})
                permissions.setdefault(field, list(shipped[field]))
                role_spec["permissions"] = permissions
        role_spec["agent_module"] = {
            "id": module_spec["id"],
            "version": module_spec.get("version", "0.0.0"),
            "display_name": module_spec.get("display_name", module_spec["id"]),
            "publisher": module_spec.get("publisher", "Unknown"),
            "category": module_spec.get("category", "general"),
            "description": module_spec.get("description", ""),
            "marketplace": module_spec.get("marketplace") or {},
            "prompt_bundle": module_spec.get("prompt_bundle") or {},
            "runtime": module_spec.get("runtime") or {},
        }
        self.validate_role_spec(
            role_spec,
            f"modules/{module_spec['id']}.yaml role",
        )
        role_spec["skill_reference_diagnostics"] = (
            self.skill_reference_diagnostics_for_role(
                role_spec, runtime=module_spec.get("runtime") or {}
            )
        )
        return role_key, role_spec

    def _shipped_role_identity(self, role_key: str) -> dict[str, Any]:
        """The persona and MCP servers the role's own YAML gives it, if it has one."""
        try:
            payload = self._load_yaml(self._role_path(role_key))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        permissions = payload.get("permissions") or {}
        return {"persona": payload.get("persona"), "team": payload.get("team"),
                "mcp_servers": permissions.get("mcp_servers"),
                "abilities": permissions.get("abilities"),
                "recommended_abilities": permissions.get("recommended_abilities")}

    def _upsert_role_spec(
        self,
        connection: Connection,
        project_id: str,
        role_key: str,
        role_spec: dict[str, Any],
        actor: str,
        *,
        source_module_id: str | None = None,
        source_module_version: str | None = None,
        born_from: dict[str, Any] | None = None,
        preserve_module_owned_role: bool = False,
    ) -> dict[str, Any]:
        display_name = role_spec.get("display_name", role_key.replace("_", " ").title())
        autonomy_level = int(((role_spec.get("autonomy") or {}).get("level")) or 0)
        # THE SEED IS ADDITIVE ONLY. An agent an operator deleted is not reinstalled, and one an
        # operator edited keeps its edits: this ran on every install and upgrade and rewrote any
        # bundled role whose spec differed from the pack's YAML — which is every edited one.
        # "present" because, from the pack's side, nothing needed doing.
        from . import seed_exclusions

        if seed_exclusions.is_excluded(connection, project_id, "role", role_key):
            return {"role_key": role_key, "display_name": display_name,
                    "autonomy_level": autonomy_level, "status": "present",
                    "kept_as_operator_left_it": "deleted",
                    "source_module_id": source_module_id,
                    "source_module_version": source_module_version}
        operator_edited = seed_exclusions.is_excluded(
            connection, project_id, "role_spec", role_key)
        existing_role = connection.execute(
            text(
                """
                SELECT id, display_name, autonomy_level, spec, source_module_id, source_module_version
                FROM roles
                WHERE project_id = :project_id AND role_key = :role_key
                """
            ),
            {"project_id": project_id, "role_key": role_key},
        ).mappings().first()
        role_id = existing_role["id"] if existing_role else f"role_{project_id}_{role_key}"
        status = "present"
        if not existing_role:
            connection.execute(
                text(
                    """
                    INSERT INTO roles (
                        id, project_id, role_key, display_name, spec, autonomy_level,
                        source_module_id, source_module_version
                    )
                    VALUES (
                        :id, :project_id, :role_key, :display_name, CAST(:spec AS JSONB),
                        :autonomy_level, :source_module_id, :source_module_version
                    )
                    """
                ),
                {
                    "id": role_id,
                    "project_id": project_id,
                    "role_key": role_key,
                    "display_name": display_name,
                    "spec": json.dumps(role_spec),
                    "autonomy_level": autonomy_level,
                    "source_module_id": source_module_id,
                    "source_module_version": source_module_version,
                },
            )
            emit_event(
                connection,
                project_id,
                "role.installed",
                actor,
                "role",
                role_id,
                {
                    "role_key": role_key,
                    "display_name": display_name,
                    "source_module_id": source_module_id,
                },
            )
            # BORN FROM THE PACK: an agt_ id, born_by 'spec-pack', no sponsor (its owners
            # answer for it), and its v1 seal (agent_identity; the seed batches the seal so the
            # grants that follow land in v1, not v2…v9).
            from . import agent_identity

            agent_identity.quietly(
                connection, agent_identity.birth, project_id, role_key, born_by="spec-pack",
                born_from=born_from or {"kind": "pack", "module_id": source_module_id,
                                        "module_version": source_module_version},
                actor=actor)
            status = "installed"
        elif operator_edited:
            pass
        elif preserve_module_owned_role and existing_role.get("source_module_id"):
            # The pack seeds a base role; an installed module owns its effective remit.
            # Do not reset it before the following module seed restores the same spec.
            source_module_id = existing_role["source_module_id"]
            source_module_version = existing_role.get("source_module_version")
        else:
            spec_changed = (
                existing_role["display_name"] != display_name
                or existing_role["autonomy_level"] != autonomy_level
                or (existing_role["spec"] or {}) != role_spec
                or existing_role.get("source_module_id") != source_module_id
                or existing_role.get("source_module_version") != source_module_version
            )
            if spec_changed:
                connection.execute(
                    text(
                        """
                        UPDATE roles
                        SET display_name = :display_name,
                            spec = CAST(:spec AS JSONB),
                            autonomy_level = :autonomy_level,
                            source_module_id = :source_module_id,
                            source_module_version = :source_module_version
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": role_id,
                        "display_name": display_name,
                        "spec": json.dumps(role_spec),
                        "autonomy_level": autonomy_level,
                        "source_module_id": source_module_id,
                        "source_module_version": source_module_version,
                    },
                )
                emit_event(
                    connection,
                    project_id,
                    "role.updated",
                    actor,
                    "role",
                    role_id,
                    {
                        "role_key": role_key,
                        "display_name": display_name,
                        "source_module_id": source_module_id,
                    },
                )
                # A NEW PACK VERSION IS A NEW AGENT VERSION, when it really changed the remit.
                from . import agent_identity

                agent_identity.quietly(connection, agent_identity.seal_identity, project_id,
                                       role_key, "remit", actor)
                status = "updated"
        if existing_role and not operator_edited:
            # The agent's identity carries the name people see (consent sheet, lineage,
            # approvals): it follows the role's name, or a renamed agent keeps its old one there.
            connection.execute(text(
                "UPDATE identity_principals SET display_name = :n, updated_at = now() "
                "WHERE kind = 'agent' AND id = (SELECT agent_id FROM roles WHERE id = :id) "
                "AND display_name IS DISTINCT FROM :n"), {"n": display_name, "id": role_id})
        return {
            "role_key": role_key,
            "display_name": display_name,
            "autonomy_level": autonomy_level,
            "status": status,
            "source_module_id": source_module_id,
            "source_module_version": source_module_version,
        }

    def list_available_packs(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted((self.settings.specpack_root / "packs").glob("*.yaml")):
            payload = self._load_yaml(path)
            pack_id = payload.get("id") or path.stem
            installs = payload.get("installs") or {}
            items.append(
                {
                    "pack_id": pack_id,
                    "version": payload.get("version", "0.0.0"),
                    "description": payload.get("description", ""),
                    "status": payload.get("status", "draft"),
                    "requires": payload.get("requires", []),
                    "roles": installs.get("roles", []),
                    "workflows": installs.get("workflows", []),
                    "policies": installs.get("policies", []),
                    "skills": installs.get("skills", []),
                }
            )
        return items

    def list_available_agent_modules(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted((self.settings.specpack_root / "modules").glob("*.yaml")):
            payload = self._load_yaml(path)
            module_id = payload.get("id") or path.stem
            role = payload.get("role") or {}
            runtime = payload.get("runtime") or {}
            marketplace = payload.get("marketplace") or {}
            required_entitlements = self._required_entitlements(payload)
            items.append(
                {
                    "module_id": module_id,
                    "version": payload.get("version", "0.0.0"),
                    "display_name": payload.get(
                        "display_name", module_id.replace("_", " ").title()
                    ),
                    "description": payload.get("description", ""),
                    "publisher": payload.get("publisher", "Unknown"),
                    "category": payload.get("category", "general"),
                    "status": payload.get("status", "draft"),
                    "role_key": role.get("role_key") or role.get("role_id"),
                    "job_kinds": runtime.get("job_kinds") or [],
                    "default_skill_ids": runtime.get("default_skill_ids") or [],
                    "tags": marketplace.get("tags") or [],
                    "price_model": marketplace.get("price_model"),
                    "price_usd_micros": marketplace.get("price_usd_micros"),
                    "required_entitlements": required_entitlements,
                    "config_schema": runtime.get("configuration_schema") or {},
                    "prompt_bundle": payload.get("prompt_bundle") or {},
                    "runtime": runtime,
                    "marketplace": marketplace,
                    "role": role,
                }
            )
        return items

    def list_packs_for_project(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        installed_rows = connection.execute(
            text(
                """
                SELECT pack_id, pack_version, description, spec, installed_by, status,
                       installed_at, updated_at, source, yaml_blob
                FROM pack_installs
                WHERE project_id = :project_id
                ORDER BY installed_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        installed_by_pack = {row["pack_id"]: dict(row) for row in installed_rows}

        items: list[dict[str, Any]] = []
        baked_pack_ids: set[str] = set()
        for pack in self.list_available_packs():
            baked_pack_ids.add(pack["pack_id"])
            installed = installed_by_pack.get(pack["pack_id"])
            items.append(
                {
                    **pack,
                    "installed": installed is not None,
                    "installed_by": installed["installed_by"] if installed else None,
                    "installed_at": (
                        installed["installed_at"].isoformat() if installed else None
                    ),
                    "updated_at": (
                        installed["updated_at"].isoformat() if installed else None
                    ),
                    "installed_version": (
                        installed["pack_version"] if installed else None
                    ),
                }
            )
        for pack_id, installed in installed_by_pack.items():
            if pack_id in baked_pack_ids:
                continue
            spec = installed.get("spec") or {}
            installs = spec.get("installs") or {}
            items.append(
                {
                    "pack_id": pack_id,
                    "version": installed["pack_version"]
                    or spec.get("version")
                    or "0.0.0",
                    "description": installed["description"]
                    or spec.get("description")
                    or "",
                    "status": installed["status"] or spec.get("status") or "active",
                    "requires": spec.get("requires") or [],
                    "roles": installs.get("roles") or [],
                    "workflows": installs.get("workflows") or [],
                    "policies": installs.get("policies") or [],
                    "skills": installs.get("skills") or [],
                    "installed": True,
                    "installed_by": installed["installed_by"],
                    "installed_at": (
                        installed["installed_at"].isoformat()
                        if installed.get("installed_at")
                        else None
                    ),
                    "updated_at": (
                        installed["updated_at"].isoformat()
                        if installed.get("updated_at")
                        else None
                    ),
                    "installed_version": installed["pack_version"],
                }
            )
        return items

    def list_agent_modules_for_project(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        installed_rows = connection.execute(
            text(
                """
                SELECT module_id, module_version, config, installed_by, status,
                       installed_at, updated_at
                FROM agent_module_installs
                WHERE project_id = :project_id
                  AND status = 'active'
                ORDER BY installed_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        installed_by_module = {row["module_id"]: dict(row) for row in installed_rows}

        items: list[dict[str, Any]] = []
        active_capabilities = self._project_capabilities(connection, project_id)
        for module in self.list_available_agent_modules():
            installed = installed_by_module.get(module["module_id"])
            entitlement_result = self._entitlement_result(
                module.get("required_entitlements") or [],
                active_capabilities,
            )
            items.append(
                {
                    **module,
                    "entitlement_status": entitlement_result["status"],
                    "entitlement_result": entitlement_result,
                    "installed": installed is not None,
                    "installed_by": installed["installed_by"] if installed else None,
                    "installed_at": (
                        installed["installed_at"].isoformat() if installed else None
                    ),
                    "updated_at": (
                        installed["updated_at"].isoformat() if installed else None
                    ),
                    "installed_version": (
                        installed["module_version"] if installed else None
                    ),
                    "config": installed["config"] if installed else {},
                }
            )
        return items

    def get_pack_for_project(
        self, connection: Connection, project_id: str, pack_id: str
    ) -> dict[str, Any]:
        pack = next(
            (
                item
                for item in self.list_packs_for_project(connection, project_id)
                if item["pack_id"] == pack_id
            ),
            None,
        )
        if not pack:
            raise PackNotFoundError(f"Pack '{pack_id}' not found")
        return pack

    def get_agent_module_for_project(
        self, connection: Connection, project_id: str, module_id: str
    ) -> dict[str, Any]:
        module = next(
            (
                item
                for item in self.list_agent_modules_for_project(connection, project_id)
                if item["module_id"] == module_id
            ),
            None,
        )
        if not module:
            raise AgentModuleNotFoundError(f"Agent module '{module_id}' not found")
        return module

    def register_role(
        self, connection: Connection, project_id: str, role_key: str, *,
        display_name: Optional[str] = None, autonomy_level: Optional[int] = None,
        reports_to: Optional[str] = None, spec: Optional[dict] = None,
        reports_to_given: bool = True,
        actor: str = "operator",
    ) -> dict[str, Any]:
        """Put a role on the books, or update the one already there.

        WHY THIS DID NOT EXIST
        ----------------------
        BOSS had eight role endpoints and every one of them read. Roles arrived only by
        installing a spec pack, so an agent that started doing work outside one could never be
        registered at all -- and an unregistered role has no trust score, no autonomy level and
        no violation tracking. The console had been reporting exactly that, on five agents that
        between them ran most of the PRD pipeline, with no way to act on it.

        Idempotent on (project_id, role_key): registering twice updates rather than duplicating,
        because the caller is usually a console button pressed by someone who cannot see whether
        it worked the first time.

        AN UPDATE CHANGES ONLY WHAT IT WAS GIVEN. `autonomy_level=None` keeps the current level
        and `reports_to_given=False` keeps the current manager. Both used to be written
        unconditionally, so re-registering a role to fix its display name demoted it to
        autonomy 0 and detached it from the org chart.
        """
        from . import seed_exclusions

        key = (role_key or "").strip()
        if not key:
            raise ValueError("role_key is required")
        # ONE LINT FOR EVERY WAY IN. A persona written here reaches the same prompts as one
        # saved through PATCH /v1/agents, so it is cleaned (and refused) the same way: raises
        # agents.PersonaInvalid, a 422 naming the field.
        if isinstance(spec, dict) and spec.get("persona") is not None:
            from .agents import _clean_persona

            cleaned, _warnings = _clean_persona(connection, project_id, spec["persona"])
            spec = {**spec, "persona": cleaned}

        existing = connection.execute(
            text("SELECT id FROM roles WHERE project_id = :p AND role_key = :k"),
            {"p": project_id, "k": key},
        ).mappings().first()

        # A role reporting to itself is a cycle of one. Cheap to check, and it would otherwise
        # make the org chart pick an arbitrary root and look correct while being wrong.
        if reports_to is not None and reports_to.strip() == key:
            raise ValueError("a role cannot report to itself")

        if existing:
            connection.execute(
                text(
                    """
                    UPDATE roles
                       SET display_name    = COALESCE(:name, display_name),
                           autonomy_level  = COALESCE(:autonomy, autonomy_level),
                           reports_to      = CASE WHEN :given THEN :reports_to
                                                  ELSE reports_to END,
                           spec            = COALESCE(CAST(:spec AS JSONB), spec)
                     WHERE project_id = :p AND role_key = :k
                    """
                ),
                {"p": project_id, "k": key, "name": display_name,
                 "autonomy": None if autonomy_level is None else int(autonomy_level),
                 "given": bool(reports_to_given), "reports_to": reports_to,
                 "spec": json.dumps(spec) if spec is not None else None},
            )
            from . import agent_identity

            agent_identity.quietly(connection, agent_identity.seal_identity, project_id, key,
                                   "remit", actor)
        else:
            # REGISTERING A SHIPPED ROLE INSTALLS THE SHIPPED ROLE. "Register" on an agent the
            # roster found doing work used to make an empty row: no persona, no remit, no
            # grants — a registered nobody. When BOSS ships a definition for the key, that is
            # what is registered (persona, skills, MCP holdings); anything the caller gave wins.
            shipped: dict[str, Any] = {}
            if not spec and getattr(self.settings, "specpack_root", None) is not None:
                try:
                    shipped = self.load_role_spec(key)
                except (RoleNotFoundError, ValueError):
                    shipped = {}
            if shipped:
                spec = shipped
                display_name = display_name or shipped.get("display_name")
                if autonomy_level is None:
                    autonomy_level = int(((shipped.get("autonomy") or {}).get("level")) or 0)
            connection.execute(
                text(
                    """
                    INSERT INTO roles (id, project_id, role_key, display_name, autonomy_level,
                                       reports_to, spec)
                    VALUES (:id, :p, :k, :name, :autonomy, :reports_to,
                            CAST(:spec AS JSONB))
                    """
                ),
                {"id": f"role_{uuid4().hex[:20]}", "p": project_id, "k": key,
                 "name": display_name or key.replace("_", " ").title(),
                 "autonomy": int(autonomy_level or 0), "reports_to": reports_to,
                 "spec": json.dumps(spec or {})},
            )
            # Registering a key by hand is a new decision, over any earlier delete.
            seed_exclusions.lift(connection, project_id, "role", key)
            # And a birth: the person who registered it made it and answers for it.
            from . import agent_identity

            agent_identity.quietly(connection, agent_identity.birth, project_id, key,
                                   born_by=actor,
                                   born_from={"kind": "pack"} if shipped else {"kind": "blank"},
                                   actor=actor)
            if shipped:
                # Its grants, by the seed's own additive syncs (an operator removal holds).
                from .shipped_cast import _grant

                _grant(connection, project_id, actor)
                return self.get_role_for_project(connection, project_id, key)
        # A SPEC OR NAME AN OPERATOR WROTE STAYS WRITTEN. This path recorded nothing, so editing
        # a bundled role through POST /v1/roles lasted until the next seed, which found the spec
        # differed from the pack's YAML and put the YAML back. An autonomy- or manager-only
        # update writes no spec and so records nothing; neither does registering a bare key
        # (empty spec), which should still be filled from its pack if it has one.
        if spec or (existing and display_name is not None):
            seed_exclusions.exclude(connection, project_id, "role_spec", key, actor=actor,
                                    reason="written through POST /v1/roles")
        return self.get_role_for_project(connection, project_id, key)

    def list_roles_for_project(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT DISTINCT ON (role_key)
                       id, role_key, display_name, autonomy_level, spec, created_at,
                       reports_to,
                       trust_score, successful_runs, policy_violation_count,
                       duplicate_effect_attempt_count, hidden_failure_count,
                       low_quality_output_count, scope_violation_count,
                       source_module_id, source_module_version,
                       last_reviewed_at, last_violation_at
                FROM roles
                WHERE project_id = :project_id
                ORDER BY role_key, created_at DESC, id DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [
            {
                "id": row["id"],
                "project_id": project_id,
                "role_key": row["role_key"],
                "display_name": row["display_name"],
                "autonomy_level": row["autonomy_level"],
                "reports_to": row.get("reports_to"),
                "spec": row["spec"] or {},
                "trust_score": int(row.get("trust_score") or 50),
                "successful_runs": int(row.get("successful_runs") or 0),
                "policy_violation_count": int(row.get("policy_violation_count") or 0),
                "duplicate_effect_attempt_count": int(
                    row.get("duplicate_effect_attempt_count") or 0
                ),
                "hidden_failure_count": int(row.get("hidden_failure_count") or 0),
                "low_quality_output_count": int(
                    row.get("low_quality_output_count") or 0
                ),
                "scope_violation_count": int(row.get("scope_violation_count") or 0),
                "source_module_id": row.get("source_module_id"),
                "source_module_version": row.get("source_module_version"),
                "last_reviewed_at": (
                    row["last_reviewed_at"].isoformat()
                    if row.get("last_reviewed_at")
                    else None
                ),
                "last_violation_at": (
                    row["last_violation_at"].isoformat()
                    if row.get("last_violation_at")
                    else None
                ),
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    def get_role_for_project(
        self, connection: Connection, project_id: str, role_key: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, role_key, display_name, autonomy_level, spec, created_at,
                       trust_score, successful_runs, policy_violation_count,
                       duplicate_effect_attempt_count, hidden_failure_count,
                       low_quality_output_count, scope_violation_count,
                       source_module_id, source_module_version,
                       last_reviewed_at, last_violation_at
                FROM roles
                WHERE project_id = :project_id AND role_key = :role_key
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "role_key": role_key},
        ).mappings().first()
        if not row:
            raise RoleNotFoundError(f"Role '{role_key}' not installed for project")
        return {
            "id": row["id"],
            "project_id": project_id,
            "role_key": row["role_key"],
            "display_name": row["display_name"],
            "autonomy_level": row["autonomy_level"],
            "spec": row["spec"] or {},
            "trust_score": int(row.get("trust_score") or 50),
            "successful_runs": int(row.get("successful_runs") or 0),
            "policy_violation_count": int(row.get("policy_violation_count") or 0),
            "duplicate_effect_attempt_count": int(
                row.get("duplicate_effect_attempt_count") or 0
            ),
            "hidden_failure_count": int(row.get("hidden_failure_count") or 0),
            "low_quality_output_count": int(
                row.get("low_quality_output_count") or 0
            ),
            "scope_violation_count": int(row.get("scope_violation_count") or 0),
            "source_module_id": row.get("source_module_id"),
            "source_module_version": row.get("source_module_version"),
            "last_reviewed_at": (
                row["last_reviewed_at"].isoformat()
                if row.get("last_reviewed_at")
                else None
            ),
            "last_violation_at": (
                row["last_violation_at"].isoformat()
                if row.get("last_violation_at")
                else None
            ),
            "created_at": row["created_at"].isoformat(),
        }

    def resolve_installed_agent_module(
        self, connection: Connection, project_id: str, module_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT module_version, config, spec, status
                FROM agent_module_installs
                WHERE project_id = :project_id AND module_id = :module_id
                """
            ),
            {"project_id": project_id, "module_id": module_id},
        ).mappings().first()
        if not row:
            raise AgentModuleNotFoundError(
                f"Agent module '{module_id}' is not installed for project"
            )
        if row["status"] != "active":
            raise AgentModuleNotFoundError(
                f"Agent module '{module_id}' is not active for project"
            )
        spec = dict(row["spec"] or {})
        spec["version"] = row["module_version"]
        spec["config"] = row["config"] or {}
        spec["status"] = row["status"]
        return spec

    def find_default_agent_module_for_job_kind(
        self, connection: Connection, project_id: str, job_kind: str
    ) -> dict[str, Any] | None:
        rows = connection.execute(
            text(
                """
                SELECT module_id, module_version, config, spec
                FROM agent_module_installs
                WHERE project_id = :project_id AND status = 'active'
                ORDER BY installed_at ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        for row in rows:
            spec = dict(row["spec"] or {})
            runtime = spec.get("runtime") or {}
            if job_kind in (runtime.get("job_kinds") or []):
                spec["version"] = row["module_version"]
                spec["config"] = row["config"] or {}
                return spec
        return None

    def install_agent_module(
        self,
        connection: Connection,
        project_id: str,
        module_id: str,
        actor: str,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        module_spec = self.load_agent_module_spec(module_id)
        active_capabilities = self._project_capabilities(connection, project_id)
        required_entitlements = self._required_entitlements(module_spec)
        entitlement_result = self._entitlement_result(
            required_entitlements,
            active_capabilities,
        )
        if entitlement_result["status"] == "missing_required_entitlement":
            integrity_artifact_id = self._create_agent_module_integrity_artifact(
                connection,
                project_id,
                module_spec,
                action="install_denied",
                actor=actor,
                entitlement_result=entitlement_result,
                config=config or {},
            )
            self._record_agent_module_install(
                connection,
                project_id,
                module_id,
                action="install_denied",
                actor=actor,
                status="denied",
                from_version=None,
                to_version=module_spec.get("version", "0.0.0"),
                entitlement_result=entitlement_result,
                rollback_snapshot={},
                metadata={
                    "reason": "missing_required_entitlement",
                    "module_integrity_artifact_id": integrity_artifact_id,
                },
            )
            emit_event(
                connection,
                project_id,
                "agent_module.install_denied",
                actor,
                "agent_module",
                module_id,
                entitlement_result,
            )
            missing = ", ".join(entitlement_result["missing_entitlements"])
            raise AgentModuleEntitlementError(
                f"Agent module '{module_id}' requires missing entitlement(s): {missing}."
            )
        role_key, role_spec = self._normalize_role_spec_from_module(module_spec)
        role_result = self._upsert_role_spec(
            connection,
            project_id,
            role_key,
            role_spec,
            actor,
            source_module_id=module_spec["id"],
            source_module_version=module_spec.get("version", "0.0.0"),
        )
        merged_config = config or {}

        existing_install = connection.execute(
            text(
                """
                SELECT id, module_version, config, spec, status
                FROM agent_module_installs
                WHERE project_id = :project_id AND module_id = :module_id
                """
            ),
            {"project_id": project_id, "module_id": module_id},
        ).mappings().first()
        install_id = (
            existing_install["id"]
            if existing_install
            else f"agent_module_{project_id}_{module_id}"
        )
        rollback_snapshot = (
            {
                "module_version": existing_install["module_version"],
                "config": existing_install["config"] or {},
                "spec": existing_install["spec"] or {},
            }
            if existing_install
            else {}
        )
        matches_existing = (
            existing_install is not None
            and existing_install["module_version"] == module_spec.get("version", "0.0.0")
            and (existing_install["config"] or {}) == merged_config
            and (existing_install["spec"] or {}) == module_spec
            and existing_install["status"] == module_spec.get("status", "active")
            and role_result["status"] == "present"
        )
        if not matches_existing:
            integrity_artifact_id = self._create_agent_module_integrity_artifact(
                connection,
                project_id,
                module_spec,
                action="install",
                actor=actor,
                entitlement_result=entitlement_result,
                config=merged_config,
                rollback_snapshot=rollback_snapshot,
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agent_module_installs (
                        id, project_id, module_id, module_version, role_key, display_name,
                        publisher, category, description, marketplace, runtime, config, spec,
                        installed_by, status
                    )
                    VALUES (
                        :id, :project_id, :module_id, :module_version, :role_key,
                        :display_name, :publisher, :category, :description,
                        CAST(:marketplace AS JSONB), CAST(:runtime AS JSONB),
                        CAST(:config AS JSONB), CAST(:spec AS JSONB),
                        :installed_by, :status
                    )
                    ON CONFLICT (project_id, module_id) DO UPDATE
                    SET module_version = EXCLUDED.module_version,
                        role_key = EXCLUDED.role_key,
                        display_name = EXCLUDED.display_name,
                        publisher = EXCLUDED.publisher,
                        category = EXCLUDED.category,
                        description = EXCLUDED.description,
                        marketplace = EXCLUDED.marketplace,
                        runtime = EXCLUDED.runtime,
                        config = EXCLUDED.config,
                        spec = EXCLUDED.spec,
                        installed_by = EXCLUDED.installed_by,
                        status = EXCLUDED.status,
                        updated_at = now()
                    """
                ),
                {
                    "id": install_id,
                    "project_id": project_id,
                    "module_id": module_id,
                    "module_version": module_spec.get("version", "0.0.0"),
                    "role_key": role_key,
                    "display_name": module_spec.get("display_name", module_id),
                    "publisher": module_spec.get("publisher", "Unknown"),
                    "category": module_spec.get("category", "general"),
                    "description": module_spec.get("description", ""),
                    "marketplace": json.dumps(module_spec.get("marketplace") or {}),
                    "runtime": json.dumps(module_spec.get("runtime") or {}),
                    "config": json.dumps(merged_config),
                    "spec": json.dumps(module_spec),
                    "installed_by": actor,
                    "status": module_spec.get("status", "active"),
                },
            )
            emit_event(
                connection,
                project_id,
                "agent_module.installed",
                actor,
                "agent_module",
                install_id,
                {
                    "module_id": module_id,
                    "version": module_spec.get("version", "0.0.0"),
                    "role_key": role_key,
                    "module_integrity_artifact_id": integrity_artifact_id,
                },
            )
            self._record_agent_module_install(
                connection,
                project_id,
                module_id,
                action="install",
                actor=actor,
                status="installed",
                from_version=existing_install["module_version"]
                if existing_install
                else None,
                to_version=module_spec.get("version", "0.0.0"),
                entitlement_result=entitlement_result,
                rollback_snapshot=rollback_snapshot,
                metadata={
                    "role_key": role_key,
                    "module_integrity_artifact_id": integrity_artifact_id,
                },
            )

        module = self.get_agent_module_for_project(connection, project_id, module_id)
        module["install_status"] = (
            "already_installed" if matches_existing else "installed"
        )
        module["role_result"] = role_result
        module["entitlement_result"] = entitlement_result
        return module

    def uninstall_agent_module(
        self,
        connection: Connection,
        project_id: str,
        module_id: str,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        row = self._agent_module_install_row(connection, project_id, module_id)
        if row["status"] != "active":
            module = self.get_agent_module_for_project(connection, project_id, module_id)
            module["install_status"] = "already_uninstalled"
            module["role_result"] = {"status": "unchanged"}
            module["entitlement_result"] = {"status": "not_checked"}
            return module

        rollback_snapshot = self._install_snapshot(row)
        connection.execute(
            text(
                """
                UPDATE agent_module_installs
                SET status = 'inactive', updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": row["id"]},
        )
        role_result = self._restore_role_after_module_removal(
            connection,
            project_id,
            row["role_key"],
            module_id,
            actor,
        )
        integrity_artifact_id = self._create_agent_module_integrity_artifact(
            connection,
            project_id,
            row["spec"] or {},
            action="uninstall",
            actor=actor,
            entitlement_result={"status": "not_checked"},
            config=row["config"] or {},
            rollback_snapshot=rollback_snapshot,
        )
        self._record_agent_module_install(
            connection,
            project_id,
            module_id,
            action="uninstall",
            actor=actor,
            status="uninstalled",
            from_version=row["module_version"],
            to_version=None,
            entitlement_result={"status": "not_checked"},
            rollback_snapshot=rollback_snapshot,
            metadata={
                "reason": reason,
                "role_key": row["role_key"],
                "role_result": role_result,
                "module_integrity_artifact_id": integrity_artifact_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_module.uninstalled",
            actor,
            "agent_module",
            module_id,
            {"module_id": module_id, "version": row["module_version"], "reason": reason},
        )
        module = self.get_agent_module_for_project(connection, project_id, module_id)
        module["install_status"] = "uninstalled"
        module["role_result"] = role_result
        module["entitlement_result"] = {"status": "not_checked"}
        return module

    def rollback_agent_module(
        self,
        connection: Connection,
        project_id: str,
        module_id: str,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        current_row = self._agent_module_install_row(connection, project_id, module_id)
        snapshot = self._latest_agent_module_rollback_snapshot(
            connection,
            project_id,
            module_id,
        )
        snapshot_spec = snapshot.get("spec")
        snapshot_version = snapshot.get("module_version")
        if not isinstance(snapshot_spec, dict) or not isinstance(snapshot_version, str):
            raise AgentModuleRollbackError(
                f"Agent module '{module_id}' has no usable rollback snapshot."
            )
        role_key, role_spec = self._normalize_role_spec_from_module(snapshot_spec)
        role_result = self._upsert_role_spec(
            connection,
            project_id,
            role_key,
            role_spec,
            actor,
            source_module_id=snapshot_spec["id"],
            source_module_version=snapshot_version,
        )
        current_snapshot = self._install_snapshot(current_row)
        connection.execute(
            text(
                """
                UPDATE agent_module_installs
                SET module_version = :module_version,
                    role_key = :role_key,
                    display_name = :display_name,
                    publisher = :publisher,
                    category = :category,
                    description = :description,
                    marketplace = CAST(:marketplace AS JSONB),
                    runtime = CAST(:runtime AS JSONB),
                    config = CAST(:config AS JSONB),
                    spec = CAST(:spec AS JSONB),
                    installed_by = :installed_by,
                    status = 'active',
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {
                "id": current_row["id"],
                "module_version": snapshot_version,
                "role_key": role_key,
                "display_name": snapshot_spec.get("display_name", module_id),
                "publisher": snapshot_spec.get("publisher", "Unknown"),
                "category": snapshot_spec.get("category", "general"),
                "description": snapshot_spec.get("description", ""),
                "marketplace": json.dumps(snapshot_spec.get("marketplace") or {}),
                "runtime": json.dumps(snapshot_spec.get("runtime") or {}),
                "config": json.dumps(snapshot.get("config") or {}),
                "spec": json.dumps(snapshot_spec),
                "installed_by": actor,
            },
        )
        integrity_artifact_id = self._create_agent_module_integrity_artifact(
            connection,
            project_id,
            snapshot_spec,
            action="rollback",
            actor=actor,
            entitlement_result={"status": "not_checked"},
            config=snapshot.get("config") or {},
            rollback_snapshot=current_snapshot,
        )
        self._record_agent_module_install(
            connection,
            project_id,
            module_id,
            action="rollback",
            actor=actor,
            status="rolled_back",
            from_version=current_row["module_version"],
            to_version=snapshot_version,
            entitlement_result={"status": "not_checked"},
            rollback_snapshot=current_snapshot,
            metadata={
                "reason": reason,
                "role_key": role_key,
                "module_integrity_artifact_id": integrity_artifact_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_module.rolled_back",
            actor,
            "agent_module",
            module_id,
            {
                "module_id": module_id,
                "from_version": current_row["module_version"],
                "to_version": snapshot_version,
                "reason": reason,
            },
        )
        module = self.get_agent_module_for_project(connection, project_id, module_id)
        module["install_status"] = "rolled_back"
        module["role_result"] = role_result
        module["entitlement_result"] = {"status": "not_checked"}
        return module

    def install_default_agent_modules(
        self,
        connection: Connection,
        project_id: str,
        actor: str,
    ) -> list[dict[str, Any]]:
        return [
            self.install_agent_module(connection, project_id, module_id, actor)
            for module_id in self.DEFAULT_AGENT_MODULE_IDS
        ]

    def list_agent_module_install_records(
        self,
        connection: Connection,
        project_id: str,
        module_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if module_id:
            rows = connection.execute(
                text(
                    """
                    SELECT id, project_id, module_id, action, from_version,
                           to_version, actor, status, entitlement_result,
                           rollback_snapshot, metadata, created_at
                    FROM agent_module_install_records
                    WHERE project_id = :project_id AND module_id = :module_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {"project_id": project_id, "module_id": module_id, "limit": limit},
            ).mappings().all()
        else:
            rows = connection.execute(
                text(
                    """
                    SELECT id, project_id, module_id, action, from_version,
                           to_version, actor, status, entitlement_result,
                           rollback_snapshot, metadata, created_at
                    FROM agent_module_install_records
                    WHERE project_id = :project_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {"project_id": project_id, "limit": limit},
            ).mappings().all()
        return [self._install_record_from_row(dict(row)) for row in rows]

    def _agent_module_install_row(
        self,
        connection: Connection,
        project_id: str,
        module_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, module_id, module_version, role_key,
                       display_name, publisher, category, description,
                       marketplace, runtime, config, spec, installed_by, status
                FROM agent_module_installs
                WHERE project_id = :project_id AND module_id = :module_id
                """
            ),
            {"project_id": project_id, "module_id": module_id},
        ).mappings().first()
        if not row:
            raise AgentModuleNotFoundError(
                f"Agent module '{module_id}' is not installed for project"
            )
        return dict(row)

    def _install_snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "module_version": row["module_version"],
            "config": row.get("config") or {},
            "spec": row.get("spec") or {},
            "status": row.get("status"),
        }

    def _latest_agent_module_rollback_snapshot(
        self,
        connection: Connection,
        project_id: str,
        module_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT rollback_snapshot
                FROM agent_module_install_records
                WHERE project_id = :project_id
                  AND module_id = :module_id
                  AND rollback_snapshot <> '{}'::jsonb
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "module_id": module_id},
        ).mappings().first()
        if not row:
            raise AgentModuleRollbackError(
                f"Agent module '{module_id}' has no rollback snapshot."
            )
        snapshot = row["rollback_snapshot"] or {}
        if not isinstance(snapshot, dict) or not snapshot:
            raise AgentModuleRollbackError(
                f"Agent module '{module_id}' has no rollback snapshot."
            )
        return snapshot

    def _restore_role_after_module_removal(
        self,
        connection: Connection,
        project_id: str,
        role_key: str,
        module_id: str,
        actor: str,
    ) -> dict[str, Any]:
        role = connection.execute(
            text(
                """
                SELECT id, source_module_id
                FROM roles
                WHERE project_id = :project_id AND role_key = :role_key
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "role_key": role_key},
        ).mappings().first()
        if not role or role.get("source_module_id") != module_id:
            return {"role_key": role_key, "status": "unchanged"}
        try:
            base_role_spec = self.load_role_spec(role_key)
        except RoleNotFoundError:
            connection.execute(
                text(
                    """
                    UPDATE roles
                    SET source_module_id = NULL,
                        source_module_version = NULL
                    WHERE id = :id
                    """
                ),
                {"id": role["id"]},
            )
            emit_event(
                connection,
                project_id,
                "role.source_module_cleared",
                actor,
                "role",
                role["id"],
                {"role_key": role_key, "source_module_id": module_id},
            )
            return {"role_key": role_key, "status": "source_cleared"}
        result = self._upsert_role_spec(
            connection,
            project_id,
            role_key,
            base_role_spec,
            actor,
        )
        return {**result, "status": "restored_base_role"}

    def _required_entitlements(self, module_spec: dict[str, Any]) -> list[str]:
        marketplace = module_spec.get("marketplace") or {}
        required = marketplace.get("required_entitlements")
        if required is None:
            required = marketplace.get("required_entitlement")
        if required is None:
            return []
        if isinstance(required, str):
            required_values = [required]
        elif isinstance(required, list):
            required_values = required
        else:
            required_values = []
        return sorted(
            {
                str(value).strip()
                for value in required_values
                if str(value).strip()
            }
        )

    def _project_capabilities(
        self,
        connection: Connection,
        project_id: str,
    ) -> set[str]:
        capabilities: set[str] = set()
        edition = self.edition_service.edition_status(connection, project_id)
        active_account_connect_org_id = (
            edition.get("account_connect_org") or {}
        ).get("hosted_org_id")
        if edition["status"] == "active":
            capabilities.update(edition.get("included_capabilities") or [])
        rows = connection.execute(
            text(
                """
                SELECT capability_key, capabilities, source, account_connect_org_id
                FROM license_entitlements
                WHERE project_id = :project_id
                  AND status IN ('active', 'trial')
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        for row in rows:
            if (
                row["source"] == "account_connect_hosted"
                and row["account_connect_org_id"] != active_account_connect_org_id
            ):
                continue
            capabilities.add(row["capability_key"])
            capabilities.update(row["capabilities"] or [])
        return capabilities

    def _entitlement_result(
        self,
        required_entitlements: list[str],
        active_capabilities: set[str],
    ) -> dict[str, Any]:
        missing = [
            entitlement
            for entitlement in required_entitlements
            if entitlement not in active_capabilities
        ]
        return {
            "status": "missing_required_entitlement" if missing else "allowed",
            "required_entitlements": required_entitlements,
            "missing_entitlements": missing,
            "active_capabilities": sorted(active_capabilities),
        }

    def _create_agent_module_integrity_artifact(
        self,
        connection: Connection,
        project_id: str,
        module_spec: dict[str, Any],
        *,
        action: str,
        actor: str,
        entitlement_result: dict[str, Any],
        config: dict[str, Any],
        rollback_snapshot: dict[str, Any] | None = None,
    ) -> str:
        module_id = str(module_spec.get("id") or "unknown_module")
        module_path = self._module_path(module_id)
        try:
            source_path = str(module_path.relative_to(self.settings.specpack_root))
        except ValueError:
            source_path = str(module_path)
        marketplace = module_spec.get("marketplace") or {}
        signing = module_spec.get("signing") or marketplace.get("signing") or {}
        if not isinstance(signing, dict):
            signing = {}
        signature_ref = signing.get("signature_ref") or marketplace.get("signature_ref")
        verification_mode = "declared_metadata_only" if signature_ref else "unsigned"
        content = {
            "object_type": "agent_module_integrity_packet",
            "module_id": module_id,
            "module_version": module_spec.get("version", "0.0.0"),
            "display_name": module_spec.get("display_name", module_id),
            "publisher": module_spec.get("publisher", "Unknown"),
            "category": module_spec.get("category", "general"),
            "action": action,
            "actor": actor,
            "source_path": source_path,
            "spec_hash": json_hash(module_spec),
            "config_hash": json_hash(config or {}),
            "marketplace": marketplace,
            "required_entitlements": self._required_entitlements(module_spec),
            "entitlement_result": entitlement_result,
            "signing": {
                "status": verification_mode,
                "signature_ref": signature_ref,
                "algorithm": signing.get("algorithm"),
                "public_key_ref": signing.get("public_key_ref"),
                "verified_at": None,
                "note": (
                    "No cryptographic verification was performed; BOSS records "
                    "declared signing metadata and the canonical spec hash."
                    if signature_ref
                    else "No signature metadata is declared for this local module."
                ),
            },
            "rollback_snapshot_hash": json_hash(rollback_snapshot or {}),
            "data_boundary": "internal",
        }
        return create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "agent_module_integrity_packet",
            f"Agent module integrity: {module_id} {action}",
            content,
            data_boundary="internal",
        )

    def _record_agent_module_install(
        self,
        connection: Connection,
        project_id: str,
        module_id: str,
        *,
        action: str,
        actor: str,
        status: str,
        from_version: str | None,
        to_version: str | None,
        entitlement_result: dict[str, Any],
        rollback_snapshot: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO agent_module_install_records (
                    id, project_id, module_id, action, from_version, to_version,
                    actor, status, entitlement_result, rollback_snapshot, metadata
                )
                VALUES (
                    :id, :project_id, :module_id, :action, :from_version,
                    :to_version, :actor, :status,
                    CAST(:entitlement_result AS JSONB),
                    CAST(:rollback_snapshot AS JSONB),
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": f"agent_module_install_record_{uuid4().hex}",
                "project_id": project_id,
                "module_id": module_id,
                "action": action,
                "from_version": from_version,
                "to_version": to_version,
                "actor": actor,
                "status": status,
                "entitlement_result": json.dumps(entitlement_result),
                "rollback_snapshot": json.dumps(rollback_snapshot),
                "metadata": json.dumps(metadata or {}),
            },
        )

    def _install_record_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "created_at": row["created_at"].isoformat(),
        }

    def install_pack(
        self, connection: Connection, project_id: str, pack_id: str, actor: str
    ) -> dict[str, Any]:
        pack_spec = self.load_pack_spec(pack_id)
        roles = pack_spec.get("installs", {}).get("roles", [])
        if not isinstance(roles, list):
            raise ValueError("Pack installs.roles must be a list")

        existing_pack = connection.execute(
            text(
                """
                SELECT id, pack_version, spec
                FROM pack_installs
                WHERE project_id = :project_id AND pack_id = :pack_id
                """
            ),
            {"project_id": project_id, "pack_id": pack_id},
        ).mappings().first()

        role_results: list[dict[str, Any]] = []
        changed = False
        for role_key in roles:
            if not isinstance(role_key, str) or not role_key:
                raise ValueError("Pack role entries must be non-empty strings")
            role_result = self._upsert_role_spec(
                connection,
                project_id,
                role_key,
                self.load_role_spec(role_key),
                actor,
                preserve_module_owned_role=True,
                # The Passport's "From": which pack, which version (not a module it never had).
                born_from={"kind": "pack", "pack_id": pack_id,
                           "module_version": str(pack_spec.get("version") or "0.0.0")},
            )
            if role_result["status"] != "present":
                changed = True
            role_results.append(role_result)

        pack_install_id = (
            existing_pack["id"] if existing_pack else f"pack_{project_id}_{pack_id}"
        )
        pack_spec_matches = (
            existing_pack is not None
            and existing_pack["pack_version"] == pack_spec.get("version", "0.0.0")
            and (existing_pack["spec"] or {}) == pack_spec
        )
        if existing_pack and pack_spec_matches and not changed:
            status = "already_installed"
        else:
            connection.execute(
                text(
                    """
                    INSERT INTO pack_installs (
                        id, project_id, pack_id, pack_version, description, spec, installed_by,
                        status, source, yaml_blob
                    )
                    VALUES (
                        :id, :project_id, :pack_id, :pack_version, :description,
                        CAST(:spec AS JSONB), :installed_by, :status, :source, :yaml_blob
                    )
                    ON CONFLICT (project_id, pack_id) DO UPDATE
                    SET pack_version = EXCLUDED.pack_version,
                        description = EXCLUDED.description,
                        spec = EXCLUDED.spec,
                        installed_by = EXCLUDED.installed_by,
                        status = EXCLUDED.status,
                        source = EXCLUDED.source,
                        yaml_blob = EXCLUDED.yaml_blob,
                        updated_at = now()
                    """
                ),
                {
                    "id": pack_install_id,
                    "project_id": project_id,
                    "pack_id": pack_id,
                    "pack_version": pack_spec.get("version", "0.0.0"),
                    "description": pack_spec.get("description", ""),
                    "spec": json.dumps(pack_spec),
                    "installed_by": actor,
                    "status": pack_spec.get("status", "active"),
                    "source": "baked",
                    "yaml_blob": None,
                },
            )
            emit_event(
                connection,
                project_id,
                "pack.installed",
                actor,
                "pack",
                pack_install_id,
                {
                    "pack_id": pack_id,
                    "version": pack_spec.get("version", "0.0.0"),
                    "role_count": len(role_results),
                },
            )
            status = "installed"

        pack = self.get_pack_for_project(connection, project_id, pack_id)
        pack["role_count"] = len(role_results)
        pack["role_results"] = role_results
        pack["install_status"] = status
        return pack

    def import_pack(
        self,
        connection: Connection,
        project_id: str,
        yaml_str: str,
        actor: str,
    ) -> dict[str, Any]:
        """Import local YAML pack for Community (P3 GAP-PRD-AUDIT-001).
        Validates PackSpec, no core baked clash, size<64k, stores in pack_installs
        with source=local_upload + yaml_blob, performs role installs, emits pack.imported,
        creates receipt with dedupe_key.
        """
        if len(yaml_str.encode("utf-8")) > 64 * 1024:
            raise ValueError("pack yaml exceeds 64k limit")
        try:
            payload = yaml.safe_load(yaml_str)
        except Exception as exc:
            raise ValueError(f"invalid yaml: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("pack yaml must be a mapping/object")
        pack_id = payload.get("id")
        if not isinstance(pack_id, str) or not pack_id.strip():
            raise ValueError("pack id must be a non-empty string")
        pack_id = pack_id.strip()
        self.validate_pack_spec(payload, f"import {pack_id}.yaml")
        roles = (payload.get("installs") or {}).get("roles", []) or []
        if not isinstance(roles, list):
            raise ValueError("pack installs.roles must be a list")
        for role_key in roles:
            if not isinstance(role_key, str) or not role_key.strip():
                raise ValueError("pack role entries must be non-empty strings")
            try:
                self.load_role_spec(role_key.strip())
            except (RoleNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"pack role '{role_key}' not loadable from core roles: {exc}"
                ) from exc
        baked_ids = {p["pack_id"] for p in self.list_available_packs()}
        if pack_id in baked_ids:
            raise ValueError(
                f"pack_id '{pack_id}' clashes with core baked pack; choose different id for local upload"
            )
        role_results: list[dict[str, Any]] = []
        changed = False
        for role_key in [r.strip() for r in roles if r and isinstance(r, str)]:
            role_result = self._upsert_role_spec(
                connection,
                project_id,
                role_key,
                self.load_role_spec(role_key),
                actor,
            )
            if role_result["status"] != "present":
                changed = True
            role_results.append(role_result)
        existing_pack = connection.execute(
            text(
                """
                SELECT id, pack_version, spec
                FROM pack_installs
                WHERE project_id = :project_id AND pack_id = :pack_id
                """
            ),
            {"project_id": project_id, "pack_id": pack_id},
        ).mappings().first()
        pack_install_id = (
            existing_pack["id"] if existing_pack else f"pack_{project_id}_{pack_id}"
        )
        pack_spec_matches = (
            existing_pack is not None
            and existing_pack["pack_version"] == payload.get("version", "0.0.0")
            and (existing_pack["spec"] or {}) == payload
        )
        if existing_pack and pack_spec_matches and not changed:
            status = "already_imported"
        else:
            connection.execute(
                text(
                    """
                    INSERT INTO pack_installs (
                        id, project_id, pack_id, pack_version, description, spec, installed_by,
                        status, source, yaml_blob
                    )
                    VALUES (
                        :id, :project_id, :pack_id, :pack_version, :description,
                        CAST(:spec AS JSONB), :installed_by, :status, :source, :yaml_blob
                    )
                    ON CONFLICT (project_id, pack_id) DO UPDATE
                    SET pack_version = EXCLUDED.pack_version,
                        description = EXCLUDED.description,
                        spec = EXCLUDED.spec,
                        installed_by = EXCLUDED.installed_by,
                        status = EXCLUDED.status,
                        source = EXCLUDED.source,
                        yaml_blob = EXCLUDED.yaml_blob,
                        updated_at = now()
                    """
                ),
                {
                    "id": pack_install_id,
                    "project_id": project_id,
                    "pack_id": pack_id,
                    "pack_version": payload.get("version", "0.0.0"),
                    "description": payload.get("description", ""),
                    "spec": json.dumps(payload),
                    "installed_by": actor,
                    "status": payload.get("status", "active"),
                    "source": "local_upload",
                    "yaml_blob": yaml_str,
                },
            )
            emit_event(
                connection,
                project_id,
                "pack.imported",
                actor,
                "pack",
                pack_install_id,
                {
                    "pack_id": pack_id,
                    "version": payload.get("version", "0.0.0"),
                    "role_count": len(role_results),
                    "source": "local_upload",
                },
            )
            status = "imported"
        scope = self._project_scope(connection, project_id)
        dedupe_key = f"pack.import:{project_id}:{pack_id}"
        receipt_payload = {
            "pack_id": pack_id,
            "source": "local_upload",
            "yaml_size": len(yaml_str),
            "role_count": len(role_results),
        }
        receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "pack.import",
            {"pack_id": pack_id, "project_id": project_id, "source": "local_upload"},
            receipt_payload,
            data_boundary="internal",
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            None,
            {
                "status": status,
                "pack_id": pack_id,
                "role_keys": [r["role_key"] for r in role_results],
            },
        )
        pack = self.get_pack_for_project(connection, project_id, pack_id)
        pack["role_count"] = len(role_results)
        pack["role_results"] = role_results
        pack["install_status"] = status
        return pack

    def export_pack_yaml(
        self, connection: Connection, project_id: str, pack_id: str
    ) -> str:
        """Return original YAML for export (baked fs or local_upload blob)."""
        row = connection.execute(
            text(
                """
                SELECT yaml_blob, source
                FROM pack_installs
                WHERE project_id = :project_id AND pack_id = :pack_id
                """
            ),
            {"project_id": project_id, "pack_id": pack_id},
        ).mappings().first()
        if row and row.get("yaml_blob"):
            return row["yaml_blob"]
        try:
            path = self._pack_path(pack_id)
            if path.exists():
                return path.read_text(encoding="utf-8")
        except Exception:
            pass
        baked = next(
            (p for p in self.list_available_packs() if p["pack_id"] == pack_id), None
        )
        if baked:
            try:
                return self._pack_path(pack_id).read_text(encoding="utf-8")
            except Exception:
                pass
        raise PackNotFoundError(f"Pack '{pack_id}' not found for export")
