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

    def get_pack_for_project(
        self, connection: Connection, project_id: str, pack_id: str
    ) -> dict[str, Any]:
        pack = next(
            (item for item in self.list_packs_for_project(connection, project_id) if item["pack_id"] == pack_id),
            None,
        )
        if not pack:
            raise PackNotFoundError(f"Pack '{pack_id}' not found")
        return pack

    def list_roles_for_project(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, role_key, display_name, autonomy_level, spec, created_at
                FROM roles
                WHERE project_id = :project_id
                ORDER BY role_key
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
                SELECT id, role_key, display_name, autonomy_level, spec, created_at
                FROM roles
                WHERE project_id = :project_id AND role_key = :role_key
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
            role_spec = self.load_role_spec(role_key)
            display_name = role_spec.get("display_name", role_key.replace("_", " ").title())
            autonomy_level = int(
                ((role_spec.get("autonomy") or {}).get("level")) or 0
            )
            existing_role = connection.execute(
                text(
                    """
                    SELECT id, display_name, autonomy_level, spec
                    FROM roles
                    WHERE project_id = :project_id AND role_key = :role_key
                    """
                ),
                {"project_id": project_id, "role_key": role_key},
            ).mappings().first()
            role_id = (
                existing_role["id"]
                if existing_role
                else f"role_{project_id}_{role_key}"
            )
            role_status = "present"
            if not existing_role:
                connection.execute(
                    text(
                        """
                        INSERT INTO roles (
                            id, project_id, role_key, display_name, spec, autonomy_level
                        )
                        VALUES (
                            :id, :project_id, :role_key, :display_name, CAST(:spec AS JSONB),
                            :autonomy_level
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
                    },
                )
                emit_event(
                    connection,
                    project_id,
                    "role.installed",
                    actor,
                    "role",
                    role_id,
                    {"role_key": role_key, "display_name": display_name},
                )
                changed = True
                role_status = "installed"
            else:
                spec_changed = (
                    existing_role["display_name"] != display_name
                    or existing_role["autonomy_level"] != autonomy_level
                    or (existing_role["spec"] or {}) != role_spec
                )
                if spec_changed:
                    connection.execute(
                        text(
                            """
                            UPDATE roles
                            SET display_name = :display_name,
                                spec = CAST(:spec AS JSONB),
                                autonomy_level = :autonomy_level
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": role_id,
                            "display_name": display_name,
                            "spec": json.dumps(role_spec),
                            "autonomy_level": autonomy_level,
                        },
                    )
                    emit_event(
                        connection,
                        project_id,
                        "role.updated",
                        actor,
                        "role",
                        role_id,
                        {"role_key": role_key, "display_name": display_name},
                    )
                    changed = True
                    role_status = "updated"
            role_results.append(
                {
                    "role_key": role_key,
                    "display_name": display_name,
                    "autonomy_level": autonomy_level,
                    "status": role_status,
                }
            )

        pack_install_id = (
            existing_pack["id"]
            if existing_pack
            else f"pack_{project_id}_{pack_id}"
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
