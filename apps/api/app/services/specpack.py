from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event


class PackNotFoundError(Exception):
    pass


class RoleNotFoundError(Exception):
    pass


class AgentModuleNotFoundError(Exception):
    pass


class SpecPackService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path)
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path} did not contain an object payload")
        return payload

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
        return payload

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
        return role_key, role_spec

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
    ) -> dict[str, Any]:
        display_name = role_spec.get("display_name", role_key.replace("_", " ").title())
        autonomy_level = int(((role_spec.get("autonomy") or {}).get("level")) or 0)
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
            status = "installed"
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
                status = "updated"
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
                       installed_at, updated_at
                FROM pack_installs
                WHERE project_id = :project_id
                ORDER BY installed_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        installed_by_pack = {row["pack_id"]: dict(row) for row in installed_rows}

        items: list[dict[str, Any]] = []
        for pack in self.list_available_packs():
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
                ORDER BY installed_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        installed_by_module = {row["module_id"]: dict(row) for row in installed_rows}

        items: list[dict[str, Any]] = []
        for module in self.list_available_agent_modules():
            installed = installed_by_module.get(module["module_id"])
            items.append(
                {
                    **module,
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

    def list_roles_for_project(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT DISTINCT ON (role_key)
                       id, role_key, display_name, autonomy_level, spec, created_at,
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
                SELECT id, module_version, config, spec
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
        matches_existing = (
            existing_install is not None
            and existing_install["module_version"] == module_spec.get("version", "0.0.0")
            and (existing_install["config"] or {}) == merged_config
            and (existing_install["spec"] or {}) == module_spec
            and role_result["status"] == "present"
        )
        if not matches_existing:
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
                },
            )

        module = self.get_agent_module_for_project(connection, project_id, module_id)
        module["install_status"] = (
            "already_installed" if matches_existing else "installed"
        )
        module["role_result"] = role_result
        return module

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
                        status
                    )
                    VALUES (
                        :id, :project_id, :pack_id, :pack_version, :description,
                        CAST(:spec AS JSONB), :installed_by, :status
                    )
                    ON CONFLICT (project_id, pack_id) DO UPDATE
                    SET pack_version = EXCLUDED.pack_version,
                        description = EXCLUDED.description,
                        spec = EXCLUDED.spec,
                        installed_by = EXCLUDED.installed_by,
                        status = EXCLUDED.status,
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
