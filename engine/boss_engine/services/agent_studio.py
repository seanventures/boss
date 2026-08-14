from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, json_hash, make_id, normalize_data_boundary
from .entitlements import EntitlementBrokerService
from .jobs import enqueue_job
from .skill_scope import matching_skill_ids
from .work_orchestration import WorkOrchestrationService


class AgentDefinitionNotFoundError(ValueError):
    pass


class AgentVersionNotFoundError(ValueError):
    pass


class AgentVersionStateError(ValueError):
    pass


class PanelDefinitionNotFoundError(ValueError):
    pass


class PanelVersionNotFoundError(ValueError):
    pass


class PanelVersionStateError(ValueError):
    pass


class AgentStudioSchemaMissingError(RuntimeError):
    """Studio tables are not applied on this runtime — fail closed, never 500."""

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        detail = "Agent Studio schema is not applied: " + ", ".join(self.missing)
        super().__init__(detail)


class AgentStudioService:
    """Versioned agent/panel registry and deterministic execution planner.

    This service owns configuration and durable run state. It deliberately does
    not grant capabilities: every run compiles the intersection of the pinned
    AgentVersion, installed project skills, RoleSpec permissions, data boundary,
    and current project state.
    """

    AGENT_EXECUTION_MODES = {"deterministic_brief", "live_model"}
    CATALOG_SMOKE_MAX_AGENTS = 100
    MODEL_SELECTION_MODES = {"router", "pinned", "local"}
    PANEL_TOPOLOGIES = {
        "panel",
        "producer_reviewer",
        "bounded_debate",
        "fan_out",
        "sequential_handoff",
    }
    CONSOLIDATION_KINDS = {
        "attributed_synthesis",
        "evidence_ranked",
        "weighted_recommendation",
        "majority_with_minority_report",
        "producer_reviewer",
        "hold_for_evidence",
    }
    CATALOG_TABLES = (
        "agent_definitions",
        "agent_versions",
        "panel_definitions",
        "panel_versions",
    )

    def __init__(self, settings: Settings):
        self.settings = settings

    def _catalog_tables_ready(
        self, connection: Connection
    ) -> tuple[bool, list[str]]:
        missing: list[str] = []
        for table in self.CATALOG_TABLES:
            present = connection.execute(
                text("SELECT to_regclass(:name)"), {"name": table}
            ).scalar()
            if present is None:
                missing.append(table)
        return (not missing, missing)

    # ------------------------------------------------------------------
    # Seed catalog
    # ------------------------------------------------------------------
    def seed_catalog(
        self,
        connection: Connection,
        project_id: str,
        actor: str = "bootstrap",
    ) -> dict[str, list[dict[str, Any]]]:
        ready, missing = self._catalog_tables_ready(connection)
        if not ready:
            raise AgentStudioSchemaMissingError(missing)
        agents: list[dict[str, Any]] = []
        panels: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for path in sorted((self.settings.specpack_root / "agents").glob("*.yaml")):
            payload = self._load_yaml(path)
            try:
                agents.append(
                    self._seed_agent(connection, project_id, payload, actor, path)
                )
            except ValueError as error:
                skipped.append({"path": str(path.name), "error": str(error)})
        for path in sorted((self.settings.specpack_root / "panels").glob("*.yaml")):
            payload = self._load_yaml(path)
            try:
                panels.append(
                    self._seed_panel(connection, project_id, payload, actor, path)
                )
            except ValueError as error:
                skipped.append({"path": str(path.name), "error": str(error)})
        return {"agents": agents, "panels": panels, "skipped": skipped}

    def _seed_agent(
        self,
        connection: Connection,
        project_id: str,
        payload: dict[str, Any],
        actor: str,
        source: Path,
    ) -> dict[str, Any]:
        agent_key = self._required_text(payload.get("id"), f"{source}: id")
        definition = self._find_agent_by_key(connection, project_id, agent_key)
        if definition is None:
            definition = self.create_agent(
                connection,
                project_id=project_id,
                agent_key=agent_key,
                name=self._required_text(payload.get("name"), f"{source}: name"),
                description=str(payload.get("description") or ""),
                owner=str(payload.get("owner") or "vp_agents"),
                actor=actor,
                source="specpack",
            )
        version_payload = dict(payload.get("version") or {})
        version_payload["provenance"] = {
            **dict(version_payload.get("provenance") or {}),
            "source": "specpack",
            "path": str(source.relative_to(self.settings.specpack_root)),
            "trusted": True,
        }
        version_payload["requested_skills"] = self._seed_strip_unusable_skills(
            connection, project_id, version_payload
        )
        version = self.create_agent_version(
            connection,
            definition["id"],
            project_id=project_id,
            actor=actor,
            **version_payload,
        )
        if version["status"] != "published":
            validation = self.validate_agent_version(
                connection, project_id, version["id"], actor=actor
            )
            if not validation["valid"]:
                raise ValueError(
                    f"{source} failed AgentVersion validation: "
                    + "; ".join(validation["errors"])
                )
            version = self.publish_agent_version(
                connection, project_id, version["id"], actor=actor
            )
        return {
            "agent_definition_id": definition["id"],
            "agent_version_id": version["id"],
            "agent_key": agent_key,
            "duplicate": bool(version.get("duplicate")),
        }

    def _seed_panel(
        self,
        connection: Connection,
        project_id: str,
        payload: dict[str, Any],
        actor: str,
        source: Path,
    ) -> dict[str, Any]:
        panel_key = self._required_text(payload.get("id"), f"{source}: id")
        definition = self._find_panel_by_key(connection, project_id, panel_key)
        if definition is None:
            definition = self.create_panel(
                connection,
                project_id=project_id,
                panel_key=panel_key,
                name=self._required_text(payload.get("name"), f"{source}: name"),
                description=str(payload.get("description") or ""),
                purpose=self._required_text(payload.get("purpose"), f"{source}: purpose"),
                owner=str(payload.get("owner") or "vp_agents"),
                actor=actor,
                source="specpack",
            )
        version_payload = dict(payload.get("version") or {})
        members: list[dict[str, Any]] = []
        for member in version_payload.pop("members", []) or []:
            agent_key = self._required_text(
                member.get("agent_key"), f"{source}: member.agent_key"
            )
            agent = self._find_agent_by_key(connection, project_id, agent_key)
            if not agent or not agent.get("active_version_id"):
                raise ValueError(f"{source}: seeded agent '{agent_key}' is not published")
            members.append(
                {
                    **member,
                    "agent_version_id": agent["active_version_id"],
                }
            )
        version = self.create_panel_version(
            connection,
            definition["id"],
            project_id=project_id,
            members=members,
            actor=actor,
            **version_payload,
        )
        if version["status"] != "published":
            validation = self.validate_panel_version(
                connection, project_id, version["id"], actor=actor
            )
            if not validation["valid"]:
                raise ValueError(
                    f"{source} failed PanelVersion validation: "
                    + "; ".join(validation["errors"])
                )
            version = self.publish_panel_version(
                connection, project_id, version["id"], actor=actor
            )
        return {
            "panel_definition_id": definition["id"],
            "panel_version_id": version["id"],
            "panel_key": panel_key,
            "duplicate": bool(version.get("duplicate")),
        }

    # ------------------------------------------------------------------
    # Agent registry
    # ------------------------------------------------------------------
    def create_agent(
        self,
        connection: Connection,
        *,
        project_id: str,
        agent_key: str,
        name: str,
        description: str,
        owner: str,
        actor: str,
        source: str = "operator",
    ) -> dict[str, Any]:
        agent_key = self._key(agent_key, "agent_key")
        if self._find_agent_by_key(connection, project_id, agent_key):
            raise ValueError(f"Agent key '{agent_key}' already exists.")
        agent_id = make_id("agent")
        connection.execute(
            text(
                """
                INSERT INTO agent_definitions (
                    id, project_id, agent_key, name, description, owner,
                    source, created_by
                )
                VALUES (
                    :id, :project_id, :agent_key, :name, :description, :owner,
                    :source, :created_by
                )
                """
            ),
            {
                "id": agent_id,
                "project_id": project_id,
                "agent_key": agent_key,
                "name": self._required_text(name, "name"),
                "description": description.strip(),
                "owner": self._required_text(owner, "owner"),
                "source": source,
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.agent_created",
            actor,
            "agent_definition",
            agent_id,
            {"agent_key": agent_key, "source": source},
        )
        return self.get_agent(connection, project_id, agent_id)

    def create_agent_version(
        self,
        connection: Connection,
        agent_definition_id: str,
        *,
        project_id: str,
        role_key: str,
        model_policy: Optional[dict[str, Any]] = None,
        prompts: Optional[dict[str, Any]] = None,
        rules: Optional[list[Any]] = None,
        requested_skills: Optional[list[str]] = None,
        requested_integrations: Optional[list[str]] = None,
        scopes: Optional[dict[str, Any]] = None,
        limits: Optional[dict[str, Any]] = None,
        output_contract: Optional[dict[str, Any]] = None,
        a2a_policy: Optional[dict[str, Any]] = None,
        provenance: Optional[dict[str, Any]] = None,
        agent_module_id: Optional[str] = None,
        actor: str,
    ) -> dict[str, Any]:
        self.get_agent(connection, project_id, agent_definition_id)
        content = self._normalize_agent_version_content(
            role_key=role_key,
            agent_module_id=agent_module_id,
            model_policy=model_policy,
            prompts=prompts,
            rules=rules,
            requested_skills=requested_skills,
            requested_integrations=requested_integrations,
            scopes=scopes,
            limits=limits,
            output_contract=output_contract,
            a2a_policy=a2a_policy,
            provenance=provenance,
        )
        content_hash = json_hash(content)
        duplicate = connection.execute(
            text(
                """
                SELECT id
                FROM agent_versions
                WHERE project_id = :project_id
                  AND agent_definition_id = :definition_id
                  AND content_hash = :content_hash
                """
            ),
            {
                "project_id": project_id,
                "definition_id": agent_definition_id,
                "content_hash": content_hash,
            },
        ).scalar_one_or_none()
        if duplicate:
            result = self.get_agent_version(connection, project_id, duplicate)
            result["duplicate"] = True
            return result
        version = int(
            connection.execute(
                text(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM agent_versions "
                    "WHERE agent_definition_id = :definition_id"
                ),
                {"definition_id": agent_definition_id},
            ).scalar_one()
        )
        version_id = make_id("agentv")
        connection.execute(
            text(
                """
                INSERT INTO agent_versions (
                    id, project_id, agent_definition_id, version, role_key,
                    agent_module_id, model_policy, prompts, rules,
                    requested_skills, requested_integrations, scopes, limits,
                    output_contract, a2a_policy, provenance, content_hash,
                    created_by
                )
                VALUES (
                    :id, :project_id, :definition_id, :version, :role_key,
                    :agent_module_id, CAST(:model_policy AS JSONB),
                    CAST(:prompts AS JSONB), CAST(:rules AS JSONB),
                    CAST(:requested_skills AS JSONB),
                    CAST(:requested_integrations AS JSONB),
                    CAST(:scopes AS JSONB), CAST(:limits AS JSONB),
                    CAST(:output_contract AS JSONB), CAST(:a2a_policy AS JSONB),
                    CAST(:provenance AS JSONB), :content_hash, :created_by
                )
                """
            ),
            {
                "id": version_id,
                "project_id": project_id,
                "definition_id": agent_definition_id,
                "version": version,
                "content_hash": content_hash,
                "created_by": actor,
                **self._json_params(content),
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.agent_version_created",
            actor,
            "agent_version",
            version_id,
            {
                "agent_definition_id": agent_definition_id,
                "version": version,
                "content_hash": content_hash,
            },
        )
        return self.get_agent_version(connection, project_id, version_id)

    def validate_agent_version(
        self,
        connection: Connection,
        project_id: str,
        version_id: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        version = self.get_agent_version(connection, project_id, version_id)
        errors: list[str] = []
        warnings: list[str] = []
        role = connection.execute(
            text(
                """
                SELECT id, spec
                FROM roles
                WHERE project_id = :project_id AND role_key = :role_key
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"project_id": project_id, "role_key": version["role_key"]},
        ).mappings().first()
        if role is None:
            errors.append(f"RoleSpec '{version['role_key']}' is not installed.")

        model_policy = version["model_policy"]
        execution_mode = str(model_policy.get("execution_mode") or "")
        selection_mode = str(model_policy.get("selection_mode") or "")
        if execution_mode not in self.AGENT_EXECUTION_MODES:
            errors.append(
                "model_policy.execution_mode must be deterministic_brief or live_model."
            )
        if selection_mode not in self.MODEL_SELECTION_MODES:
            errors.append("model_policy.selection_mode must be router, pinned, or local.")
        if execution_mode == "live_model" and not model_policy.get(
            "preferred_capabilities"
        ):
            errors.append(
                "live_model versions require model_policy.preferred_capabilities."
            )

        prompts = version["prompts"]
        if not str(prompts.get("system") or "").strip():
            errors.append("prompts.system must not be blank.")
        if not str(prompts.get("job") or "").strip():
            errors.append("prompts.job must not be blank.")
        if not version["rules"]:
            errors.append("rules must contain at least one deterministic rule.")

        limits = version["limits"]
        self._bounded_int(errors, limits, "max_steps", 1, 50)
        self._bounded_int(errors, limits, "max_calls", 1, 100)
        self._bounded_int(errors, limits, "timeout_seconds", 5, 3600)
        self._bounded_int(errors, limits, "max_retries", 0, 10)
        if not version["output_contract"].get("artifact_type"):
            errors.append("output_contract.artifact_type is required.")

        try:
            normalize_data_boundary(version["scopes"].get("data_boundary"))
        except ValueError as exc:
            errors.append(str(exc))

        project_skills = self._project_skill_ids(connection, project_id)
        missing_skills = sorted(
            skill
            for skill in version["requested_skills"]
            if not matching_skill_ids(skill, project_skills)
        )
        if missing_skills:
            errors.append(
                "Requested skills are not installed: " + ", ".join(missing_skills)
            )

        if role is not None:
            allowed = self._role_allowed_skill_ids(role["spec"] or {}, project_skills)
            outside_role = sorted(
                skill
                for requested in version["requested_skills"]
                for skill in matching_skill_ids(requested, project_skills)
                if skill not in allowed
            )
            if outside_role:
                errors.append(
                    "Requested skills exceed RoleSpec permissions: "
                    + ", ".join(sorted(set(outside_role)))
                )

        a2a = version["a2a_policy"]
        if not isinstance(a2a.get("publish"), list) or not isinstance(
            a2a.get("subscribe"), list
        ):
            errors.append("a2a_policy.publish and a2a_policy.subscribe must be lists.")
        if execution_mode == "deterministic_brief":
            warnings.append(
                "This version is side-effect-free and does not call a live model."
            )

        validation = {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "checked_role_id": role["id"] if role else None,
            "installed_skill_count": len(project_skills),
        }
        connection.execute(
            text(
                """
                UPDATE agent_versions
                SET validation = CAST(:validation AS JSONB)
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {
                "id": version_id,
                "project_id": project_id,
                "validation": json.dumps(validation),
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.agent_version_validated",
            actor,
            "agent_version",
            version_id,
            validation,
        )
        return validation

    def publish_agent_version(
        self,
        connection: Connection,
        project_id: str,
        version_id: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        version = self.get_agent_version(connection, project_id, version_id)
        if version["status"] == "retired":
            raise AgentVersionStateError("A retired AgentVersion cannot be published.")
        validation = self.validate_agent_version(
            connection, project_id, version_id, actor=actor
        )
        if not validation["valid"]:
            raise AgentVersionStateError(
                "AgentVersion validation failed: " + "; ".join(validation["errors"])
            )
        params = {
            "id": version_id,
            "definition_id": version["agent_definition_id"],
            "project_id": project_id,
        }
        connection.execute(
            text(
                "UPDATE agent_versions SET status='published', "
                "published_at=COALESCE(published_at, now()) "
                "WHERE id=:id AND project_id=:project_id"
            ),
            params,
        )
        connection.execute(
            text(
                "UPDATE agent_definitions SET status='active', "
                "active_version_id=:id, updated_at=now() "
                "WHERE id=:definition_id AND project_id=:project_id"
            ),
            params,
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.agent_version_published",
            actor,
            "agent_version",
            version_id,
            {
                "agent_definition_id": version["agent_definition_id"],
                "version": version["version"],
            },
        )
        return self.get_agent_version(connection, project_id, version_id)

    def list_agents(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT d.id
                FROM agent_definitions d
                WHERE d.project_id = :project_id
                ORDER BY d.name, d.agent_key
                """
            ),
            {"project_id": project_id},
        ).scalars().all()
        return [self.get_agent(connection, project_id, row) for row in rows]

    def run_catalog_smoke(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        objective: str = "Prove this published agent can compile its bounded authority envelope.",
    ) -> dict[str, Any]:
        """Exercise every catalog entry with its deterministic AgentVersion test.

        This deliberately runs no model, tool, provider, or job. It validates and
        compiles every active immutable version, records one durable AgentTestRun per
        agent, and makes missing/invalid catalog entries visible instead of skipping
        them. Live model execution is a separate budgeted workflow.
        """
        agents = self.list_agents(connection, project_id)
        if len(agents) > self.CATALOG_SMOKE_MAX_AGENTS:
            raise ValueError(
                "catalog smoke is bounded to "
                f"{self.CATALOG_SMOKE_MAX_AGENTS} agents; split this catalog before running it."
            )
        suite_id = make_id("agentcatalog")
        items: list[dict[str, Any]] = []
        for agent in agents:
            version = agent.get("active_version") or {}
            version_id = version.get("id")
            if not version_id:
                items.append(
                    {
                        "agent_id": agent["id"],
                        "agent_key": agent["agent_key"],
                        "agent_name": agent["name"],
                        "status": "blocked",
                        "reason": "no_published_active_version",
                    }
                )
                continue
            test = self.run_agent_test(
                connection,
                project_id=project_id,
                agent_version_id=version_id,
                scenario={
                    "kind": "agent_catalog_smoke",
                    "suite_id": suite_id,
                    "objective": objective[:2000],
                    "agent_key": agent["agent_key"],
                },
                assertions=None,
                actor=actor,
            )
            items.append(
                {
                    "agent_id": agent["id"],
                    "agent_key": agent["agent_key"],
                    "agent_name": agent["name"],
                    "agent_version_id": version_id,
                    "test_id": test["id"],
                    "status": test["status"],
                    "summary": (test.get("result") or {}).get("summary"),
                }
            )
        passed = sum(item["status"] == "passed" for item in items)
        blocked = sum(item["status"] == "blocked" for item in items)
        failed = len(items) - passed - blocked
        status = "passed" if items and passed == len(items) else "failed"
        result = {
            "suite_id": suite_id,
            "status": status,
            "agent_count": len(items),
            "passed_count": passed,
            "failed_count": failed,
            "blocked_count": blocked,
            "objective": objective[:2000],
            "items": items,
            "execution_mode": "deterministic_compile_only",
            "model_calls": 0,
            "tool_calls": 0,
            "external_effects": 0,
        }
        emit_event(
            connection,
            project_id,
            "agent_studio.catalog_smoke_completed",
            actor,
            "agent_catalog_smoke",
            suite_id,
            {
                "status": status,
                "agent_count": len(items),
                "passed_count": passed,
                "failed_count": failed,
                "blocked_count": blocked,
            },
        )
        return result

    def get_agent(
        self, connection: Connection, project_id: str, agent_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT d.id, d.project_id, d.agent_key, d.name, d.description,
                       d.owner, d.status, d.active_version_id, d.source,
                       d.created_by, d.created_at, d.updated_at,
                       COALESCE((
                         SELECT count(*) FROM agent_versions v
                         WHERE v.agent_definition_id = d.id
                       ), 0) AS version_count,
                       COALESCE((
                         SELECT count(*) FROM agent_studio_runs r
                         JOIN agent_versions v ON v.id = r.agent_version_id
                         WHERE v.agent_definition_id = d.id
                           AND r.status IN ('queued','leased','running','waiting_decision')
                       ), 0) AS active_run_count
                FROM agent_definitions d
                WHERE d.id = :id AND d.project_id = :project_id
                """
            ),
            {"id": agent_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise AgentDefinitionNotFoundError("Agent definition not found.")
        result = self._serialize(dict(row))
        result["version_count"] = int(row["version_count"])
        result["active_run_count"] = int(row["active_run_count"])
        if row["active_version_id"]:
            result["active_version"] = self.get_agent_version(
                connection, project_id, row["active_version_id"]
            )
        else:
            result["active_version"] = None
        return result

    def get_agent_version(
        self, connection: Connection, project_id: str, version_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT v.*, d.agent_key, d.name AS agent_name
                FROM agent_versions v
                JOIN agent_definitions d ON d.id = v.agent_definition_id
                WHERE v.id = :id AND v.project_id = :project_id
                """
            ),
            {"id": version_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise AgentVersionNotFoundError("Agent version not found.")
        return self._serialize(dict(row))

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------
    def start_agent_run(
        self,
        connection: Connection,
        *,
        project_id: str,
        agent_version_id: str,
        input_data: dict[str, Any],
        actor: str,
        dedupe_key: Optional[str],
        task_id: Optional[str] = None,
        workflow_run_id: Optional[str] = None,
        panel_run_id: Optional[str] = None,
        member_key: Optional[str] = None,
        budget_reservation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if dedupe_key:
            existing = connection.execute(
                text(
                    """
                    SELECT id FROM agent_studio_runs
                    WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                    """
                ),
                {"project_id": project_id, "dedupe_key": dedupe_key},
            ).scalar_one_or_none()
            if existing:
                result = self.get_agent_run(connection, project_id, existing)
                result["duplicate"] = True
                return result
        version = self.get_agent_version(connection, project_id, agent_version_id)
        if version["status"] != "published":
            raise AgentVersionStateError("Only published AgentVersions can run.")
        compiled = self._compile_agent_context(connection, project_id, version)
        run_id = make_id("ast_run")
        execution_mode = version["model_policy"].get("execution_mode")
        blocked_reason = None
        if compiled["denied_skills"]:
            blocked_reason = "effective_capability_intersection_denied"
        elif execution_mode == "live_model" and not budget_reservation_id:
            blocked_reason = "live_model_requires_budget_reservation"
        elif execution_mode == "live_model":
            blocked_reason = "live_model_runtime_not_enabled_for_this_release"

        status = "blocked" if blocked_reason else "queued"
        connection.execute(
            text(
                """
                INSERT INTO agent_studio_runs (
                    id, project_id, agent_version_id, panel_run_id, member_key,
                    task_id, workflow_run_id, status, input, compiled_context,
                    budget_reservation_id, dedupe_key, error, created_by
                )
                VALUES (
                    :id, :project_id, :agent_version_id, :panel_run_id, :member_key,
                    :task_id, :workflow_run_id, :status, CAST(:input AS JSONB),
                    CAST(:compiled AS JSONB), :budget_reservation_id, :dedupe_key,
                    CAST(:error AS JSONB), :created_by
                )
                """
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "agent_version_id": agent_version_id,
                "panel_run_id": panel_run_id,
                "member_key": member_key,
                "task_id": task_id,
                "workflow_run_id": workflow_run_id,
                "status": status,
                "input": json.dumps(input_data),
                "compiled": json.dumps(compiled),
                "budget_reservation_id": budget_reservation_id,
                "dedupe_key": dedupe_key,
                "error": json.dumps(
                    {"reason": blocked_reason} if blocked_reason else {}
                ),
                "created_by": actor,
            },
        )
        if blocked_reason:
            emit_event(
                connection,
                project_id,
                "agent_studio.agent_run_blocked",
                actor,
                "agent_studio_run",
                run_id,
                {"reason": blocked_reason, "agent_version_id": agent_version_id},
            )
            return self.get_agent_run(connection, project_id, run_id)

        objective = str(
            input_data.get("objective")
            or input_data.get("prompt")
            or version["agent_name"]
        )
        priority_inputs = input_data.get("priority_inputs")
        work_orchestration = WorkOrchestrationService(self.settings)
        tracked = work_orchestration.ensure_tracked_work(
            connection,
            project_id=project_id,
            subject_type="agent_studio_run",
            subject_id=run_id,
            title=f"{version['agent_name']}: {objective[:160]}",
            description=objective,
            workload_class=f"agent:{version['role_key']}",
            actor=actor,
            priority_inputs=(
                priority_inputs if isinstance(priority_inputs, dict) else None
            ),
        )
        work_item = tracked["work_item"]
        tracker_binding = tracked["tracker_binding"]
        allocation = tracked["allocation"]
        tracker_sync_job = work_orchestration.enqueue_tracker_sync(
            connection,
            project_id=project_id,
            binding_id=tracker_binding["id"],
            actor=actor,
        )
        payload = {
            "agent_studio_run_id": run_id,
            "agent_version_id": agent_version_id,
            "panel_run_id": panel_run_id,
            "member_key": member_key,
            "summary": objective,
            "context": input_data,
            "evidence_refs": input_data.get("evidence_refs") or [],
            "agent_module": {
                "id": version.get("agent_module_id") or f"studio:{version['agent_key']}",
                "version": str(version["version"]),
                "display_name": version["agent_name"],
                "role_key": version["role_key"],
            },
            "agent_runtime": compiled["agent_runtime"],
            "data_boundary": compiled["data_boundary"],
            "requires_reservation": False,
            "work_item_id": work_item["id"],
            "tracker_binding_id": tracker_binding["id"],
            "allocation_id": allocation["id"],
        }
        queued_job = enqueue_job(
            connection,
            project_id,
            "agent.studio.execute",
            payload,
            task_id=task_id,
            priority=max(0, 100 - round(float(work_item["effective_score"]))),
            priority_score=float(work_item["effective_score"]),
            workload_class=work_item["workload_class"],
            resource_units=1,
            max_attempts=int(version["limits"].get("max_retries") or 0) + 1,
            data_boundary=compiled["data_boundary"],
            dedupe_key=(
                f"agent-studio:{dedupe_key}"
                if dedupe_key
                else f"agent-studio-run:{run_id}"
            ),
            work_item_id=work_item["id"],
            tracker_binding_id=tracker_binding["id"],
            allocation_id=allocation["id"],
        )
        job_id = queued_job["id"]
        connection.execute(
            text(
                "UPDATE agent_studio_runs SET job_id = :job_id WHERE id = :id"
            ),
            {"job_id": job_id, "id": run_id},
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.agent_run_queued",
            actor,
            "agent_studio_run",
            run_id,
            {
                "agent_version_id": agent_version_id,
                "job_id": job_id,
                "execution_mode": execution_mode,
                "panel_run_id": panel_run_id,
                "work_item_id": work_item["id"],
                "tracker_binding_id": tracker_binding["id"],
                "allocation_id": allocation["id"],
                "tracker_sync_job_id": tracker_sync_job["job_id"],
            },
        )
        return self.get_agent_run(connection, project_id, run_id)

    def get_agent_run(
        self, connection: Connection, project_id: str, run_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT r.*, v.version, v.role_key, v.agent_definition_id,
                       d.agent_key, d.name AS agent_name,
                       j.status AS job_status, j.attempt AS job_attempt,
                       j.max_attempts AS job_max_attempts
                FROM agent_studio_runs r
                JOIN agent_versions v ON v.id = r.agent_version_id
                JOIN agent_definitions d ON d.id = v.agent_definition_id
                LEFT JOIN jobs j ON j.id = r.job_id
                WHERE r.id = :id AND r.project_id = :project_id
                """
            ),
            {"id": run_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise AgentVersionNotFoundError("Agent Studio run not found.")
        return self._serialize(dict(row))

    def list_agent_runs(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        ids = connection.execute(
            text(
                """
                SELECT id FROM agent_studio_runs
                WHERE project_id = :project_id
                ORDER BY created_at DESC LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).scalars().all()
        return [self.get_agent_run(connection, project_id, run_id) for run_id in ids]

    def run_agent_test(
        self,
        connection: Connection,
        *,
        project_id: str,
        agent_version_id: str,
        scenario: dict[str, Any],
        assertions: Optional[list[dict[str, Any]]],
        actor: str,
    ) -> dict[str, Any]:
        """Compile and evaluate an AgentVersion without model or tool effects.

        This is intentionally deterministic: it proves authority, boundary,
        prompt, rule, and output-contract properties before a version can be
        entrusted to a live, budgeted runtime.
        """
        version = self.get_agent_version(
            connection, project_id, agent_version_id
        )
        validation = self.validate_agent_version(
            connection, project_id, agent_version_id, actor=actor
        )
        compiled = (
            self._compile_agent_context(connection, project_id, version)
            if validation["valid"]
            else {}
        )
        requested_assertions = assertions or [
            {"kind": "version_valid"},
            {"kind": "no_denied_skills"},
            {"kind": "has_output_contract"},
            {"kind": "bounded_runtime"},
            {"kind": "no_raw_secret_markers"},
        ]
        checks: list[dict[str, Any]] = []
        serialized = json.dumps(
            {
                "scenario": scenario,
                "prompts": version["prompts"],
                "compiled_context": compiled,
            },
            sort_keys=True,
        ).lower()
        for assertion in requested_assertions:
            kind = str(assertion.get("kind") or "").strip()
            passed = False
            detail: dict[str, Any] = {}
            if kind == "version_valid":
                passed = validation["valid"]
                detail = {"errors": validation["errors"]}
            elif kind == "no_denied_skills":
                denied = compiled.get("denied_skills") or []
                passed = not denied
                detail = {"denied_skills": denied}
            elif kind == "has_output_contract":
                artifact_type = version["output_contract"].get("artifact_type")
                passed = bool(artifact_type)
                detail = {"artifact_type": artifact_type}
            elif kind == "bounded_runtime":
                limits = version["limits"]
                passed = all(
                    int(limits.get(key) or 0) > 0
                    for key in ("max_steps", "max_calls", "timeout_seconds")
                )
                detail = {"limits": limits}
            elif kind == "data_boundary_at_most":
                order = {
                    "public": 0,
                    "internal": 1,
                    "confidential": 2,
                    "restricted": 3,
                    "local-only": 4,
                }
                actual = compiled.get("data_boundary")
                expected = normalize_data_boundary(assertion.get("value"))
                passed = order.get(str(actual), 99) <= order[expected]
                detail = {"actual": actual, "maximum": expected}
            elif kind == "requires_skill":
                expected = str(assertion.get("value") or "")
                passed = expected in (compiled.get("effective_skills") or [])
                detail = {
                    "required_skill": expected,
                    "effective_skills": compiled.get("effective_skills") or [],
                }
            elif kind == "no_raw_secret_markers":
                markers = [
                    marker
                    for marker in (
                        "-----begin private key-----",
                        "aws_secret_access_key=",
                        "x-api-key:",
                        "authorization: bearer ",
                    )
                    if marker in serialized
                ]
                passed = not markers
                detail = {"detected_markers": markers}
            else:
                detail = {"error": f"Unsupported assertion kind '{kind}'."}
            checks.append(
                {
                    "kind": kind,
                    "passed": passed,
                    "detail": detail,
                }
            )
        status = "passed" if all(check["passed"] for check in checks) else "failed"
        test_id = make_id("agenttest")
        result = {
            "status": status,
            "checks": checks,
            "summary": (
                f"{sum(check['passed'] for check in checks)}/{len(checks)} "
                "deterministic AgentVersion checks passed."
            ),
            "model_calls": 0,
            "tool_calls": 0,
            "external_effects": 0,
        }
        connection.execute(
            text(
                """
                INSERT INTO agent_test_runs (
                    id, project_id, agent_version_id, scenario,
                    compiled_context, assertions, evidence, status, result,
                    created_by, completed_at
                )
                VALUES (
                    :id, :project_id, :agent_version_id,
                    CAST(:scenario AS JSONB), CAST(:compiled AS JSONB),
                    CAST(:assertions AS JSONB), CAST(:evidence AS JSONB),
                    :status, CAST(:result AS JSONB), :created_by, now()
                )
                """
            ),
            {
                "id": test_id,
                "project_id": project_id,
                "agent_version_id": agent_version_id,
                "scenario": json.dumps(scenario),
                "compiled": json.dumps(compiled),
                "assertions": json.dumps(requested_assertions),
                "evidence": json.dumps(
                    [
                        {
                            "kind": "deterministic_compile",
                            "agent_version_id": agent_version_id,
                            "content_hash": version["content_hash"],
                        }
                    ]
                ),
                "status": status,
                "result": json.dumps(result),
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.test_completed",
            actor,
            "agent_test_run",
            test_id,
            {
                "agent_version_id": agent_version_id,
                "status": status,
                "passed": sum(check["passed"] for check in checks),
                "total": len(checks),
            },
        )
        return self.get_agent_test(connection, project_id, test_id)

    def get_agent_test(
        self, connection: Connection, project_id: str, test_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT t.*, v.version, v.role_key, d.agent_key, d.name AS agent_name
                FROM agent_test_runs t
                JOIN agent_versions v ON v.id=t.agent_version_id
                JOIN agent_definitions d ON d.id=v.agent_definition_id
                WHERE t.id=:id AND t.project_id=:project_id
                """
            ),
            {"id": test_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise AgentVersionNotFoundError("Agent test run not found.")
        return self._serialize(dict(row))

    def list_agent_tests(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        ids = connection.execute(
            text(
                "SELECT id FROM agent_test_runs WHERE project_id=:project_id "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"project_id": project_id, "limit": max(1, min(500, limit))},
        ).scalars().all()
        return [self.get_agent_test(connection, project_id, item) for item in ids]

    # ------------------------------------------------------------------
    # Panel registry and runtime
    # ------------------------------------------------------------------
    def create_panel(
        self,
        connection: Connection,
        *,
        project_id: str,
        panel_key: str,
        name: str,
        description: str,
        purpose: str,
        owner: str,
        actor: str,
        source: str = "operator",
    ) -> dict[str, Any]:
        panel_key = self._key(panel_key, "panel_key")
        if self._find_panel_by_key(connection, project_id, panel_key):
            raise ValueError(f"Panel key '{panel_key}' already exists.")
        panel_id = make_id("panel")
        connection.execute(
            text(
                """
                INSERT INTO panel_definitions (
                    id, project_id, panel_key, name, description, purpose,
                    owner, source, created_by
                )
                VALUES (
                    :id, :project_id, :panel_key, :name, :description, :purpose,
                    :owner, :source, :created_by
                )
                """
            ),
            {
                "id": panel_id,
                "project_id": project_id,
                "panel_key": panel_key,
                "name": self._required_text(name, "name"),
                "description": description.strip(),
                "purpose": self._required_text(purpose, "purpose"),
                "owner": self._required_text(owner, "owner"),
                "source": source,
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.panel_created",
            actor,
            "panel_definition",
            panel_id,
            {"panel_key": panel_key, "source": source},
        )
        return self.get_panel(connection, project_id, panel_id)

    def create_panel_version(
        self,
        connection: Connection,
        panel_definition_id: str,
        *,
        project_id: str,
        members: list[dict[str, Any]],
        topology: str = "panel",
        context_policy: Optional[dict[str, Any]] = None,
        independence_policy: Optional[dict[str, Any]] = None,
        quorum_policy: Optional[dict[str, Any]] = None,
        bounds: Optional[dict[str, Any]] = None,
        evidence_policy: Optional[dict[str, Any]] = None,
        consolidation_policy: Optional[dict[str, Any]] = None,
        dissent_policy: Optional[dict[str, Any]] = None,
        escalation_policy: Optional[dict[str, Any]] = None,
        output_contract: Optional[dict[str, Any]] = None,
        actor: str,
    ) -> dict[str, Any]:
        self.get_panel(connection, project_id, panel_definition_id)
        normalized_members = [
            {
                "member_key": self._key(member.get("member_key"), "member_key"),
                "agent_version_id": self._required_text(
                    member.get("agent_version_id"), "agent_version_id"
                ),
                "weight": float(member.get("weight") or 1.0),
                "required": bool(member.get("required", True)),
                "perspective": str(member.get("perspective") or ""),
                "private_context": dict(member.get("private_context") or {}),
            }
            for member in members
        ]
        normalized_independence = dict(
            independence_policy
            or {"independent_first_pass": True, "shared_chain_of_thought": False}
        )
        if "independent_first_pass" not in normalized_independence:
            normalized_independence["independent_first_pass"] = bool(
                normalized_independence.get("initial_reviews_independent", True)
            )
        normalized_bounds = dict(
            bounds
            or {
                "max_rounds": 1,
                "timeout_seconds": 300,
                "max_calls": max(1, len(normalized_members)),
            }
        )
        if "max_calls" not in normalized_bounds:
            normalized_bounds["max_calls"] = int(
                normalized_bounds.get("max_total_calls")
                or max(1, len(normalized_members))
            )
        normalized_dissent = dict(
            dissent_policy
            or {"preserve": True, "minority_report_required": True}
        )
        if "preserve" not in normalized_dissent:
            normalized_dissent["preserve"] = bool(
                normalized_dissent.get("preserve_all", True)
            )
        content = {
            "members": normalized_members,
            "topology": str(topology).strip(),
            "context_policy": dict(context_policy or {"mode": "shared_input_private_work"}),
            "independence_policy": normalized_independence,
            "quorum_policy": dict(
                quorum_policy or {"min_members": max(2, len(normalized_members))}
            ),
            "bounds": normalized_bounds,
            "evidence_policy": dict(
                evidence_policy or {"minimum_evidence_refs": 0}
            ),
            "consolidation_policy": dict(
                consolidation_policy or {"kind": "attributed_synthesis"}
            ),
            "dissent_policy": normalized_dissent,
            "escalation_policy": dict(
                escalation_policy
                or {"target": "chief_operator", "default_action": "hold"}
            ),
            "output_contract": dict(
                output_contract
                or {"artifact_type": "panel_disposition", "schema": "PanelDisposition"}
            ),
        }
        content_hash = json_hash(content)
        duplicate = connection.execute(
            text(
                """
                SELECT id FROM panel_versions
                WHERE project_id = :project_id
                  AND panel_definition_id = :definition_id
                  AND content_hash = :content_hash
                """
            ),
            {
                "project_id": project_id,
                "definition_id": panel_definition_id,
                "content_hash": content_hash,
            },
        ).scalar_one_or_none()
        if duplicate:
            result = self.get_panel_version(connection, project_id, duplicate)
            result["duplicate"] = True
            return result
        version = int(
            connection.execute(
                text(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM panel_versions "
                    "WHERE panel_definition_id = :definition_id"
                ),
                {"definition_id": panel_definition_id},
            ).scalar_one()
        )
        version_id = make_id("panelv")
        connection.execute(
            text(
                """
                INSERT INTO panel_versions (
                    id, project_id, panel_definition_id, version, topology,
                    context_policy, independence_policy, quorum_policy, bounds,
                    evidence_policy, consolidation_policy, dissent_policy,
                    escalation_policy, output_contract, content_hash, created_by
                )
                VALUES (
                    :id, :project_id, :definition_id, :version, :topology,
                    CAST(:context_policy AS JSONB),
                    CAST(:independence_policy AS JSONB),
                    CAST(:quorum_policy AS JSONB), CAST(:bounds AS JSONB),
                    CAST(:evidence_policy AS JSONB),
                    CAST(:consolidation_policy AS JSONB),
                    CAST(:dissent_policy AS JSONB),
                    CAST(:escalation_policy AS JSONB),
                    CAST(:output_contract AS JSONB), :content_hash, :created_by
                )
                """
            ),
            {
                "id": version_id,
                "project_id": project_id,
                "definition_id": panel_definition_id,
                "version": version,
                "content_hash": content_hash,
                "created_by": actor,
                **{
                    key: json.dumps(value) if isinstance(value, dict) else value
                    for key, value in content.items()
                    if key != "members"
                },
            },
        )
        for member in normalized_members:
            connection.execute(
                text(
                    """
                    INSERT INTO panel_version_members (
                        id, project_id, panel_version_id, member_key,
                        agent_version_id, weight, required, perspective,
                        private_context
                    )
                    VALUES (
                        :id, :project_id, :panel_version_id, :member_key,
                        :agent_version_id, :weight, :required, :perspective,
                        CAST(:private_context AS JSONB)
                    )
                    """
                ),
                {
                    "id": make_id("panelm"),
                    "project_id": project_id,
                    "panel_version_id": version_id,
                    **member,
                    "private_context": json.dumps(member["private_context"]),
                },
            )
        emit_event(
            connection,
            project_id,
            "agent_studio.panel_version_created",
            actor,
            "panel_version",
            version_id,
            {
                "panel_definition_id": panel_definition_id,
                "version": version,
                "member_count": len(normalized_members),
                "content_hash": content_hash,
            },
        )
        return self.get_panel_version(connection, project_id, version_id)

    def validate_panel_version(
        self,
        connection: Connection,
        project_id: str,
        version_id: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        version = self.get_panel_version(connection, project_id, version_id)
        errors: list[str] = []
        warnings: list[str] = []
        members = version["members"]
        if len(members) < 2:
            errors.append("A PanelVersion requires at least two members.")
        if len({m["member_key"] for m in members}) != len(members):
            errors.append("Panel member keys must be unique.")
        if version["topology"] not in self.PANEL_TOPOLOGIES:
            errors.append("Unsupported panel topology.")
        if (
            version["consolidation_policy"].get("kind")
            not in self.CONSOLIDATION_KINDS
        ):
            errors.append("Unsupported consolidation policy.")
        self._bounded_int(errors, version["bounds"], "max_rounds", 1, 10)
        self._bounded_int(errors, version["bounds"], "timeout_seconds", 5, 3600)
        self._bounded_int(errors, version["bounds"], "max_calls", 1, 500)
        quorum = int(version["quorum_policy"].get("min_members") or 0)
        required_count = sum(1 for member in members if member["required"])
        if quorum < 1 or quorum > len(members):
            errors.append("quorum_policy.min_members must fit the member count.")
        if required_count and quorum < 1:
            errors.append("A panel with required members needs a positive quorum.")
        for member in members:
            try:
                agent_version = self.get_agent_version(
                    connection, project_id, member["agent_version_id"]
                )
                if agent_version["status"] != "published":
                    errors.append(
                        f"Member {member['member_key']} does not pin a published AgentVersion."
                    )
            except AgentVersionNotFoundError:
                errors.append(
                    f"Member {member['member_key']} references an unknown AgentVersion."
                )
        if version["dissent_policy"].get("preserve") is not True:
            errors.append("dissent_policy.preserve must be true.")
        if not version["independence_policy"].get("independent_first_pass"):
            warnings.append("The panel does not require an independent first pass.")
        validation = {"valid": not errors, "errors": errors, "warnings": warnings}
        connection.execute(
            text(
                "UPDATE panel_versions SET validation=CAST(:v AS JSONB) "
                "WHERE id=:id AND project_id=:project_id"
            ),
            {
                "v": json.dumps(validation),
                "id": version_id,
                "project_id": project_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.panel_version_validated",
            actor,
            "panel_version",
            version_id,
            validation,
        )
        return validation

    def publish_panel_version(
        self,
        connection: Connection,
        project_id: str,
        version_id: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        version = self.get_panel_version(connection, project_id, version_id)
        if version["status"] == "retired":
            raise PanelVersionStateError("A retired PanelVersion cannot be published.")
        validation = self.validate_panel_version(
            connection, project_id, version_id, actor=actor
        )
        if not validation["valid"]:
            raise PanelVersionStateError(
                "PanelVersion validation failed: " + "; ".join(validation["errors"])
            )
        params = {
            "id": version_id,
            "definition_id": version["panel_definition_id"],
            "project_id": project_id,
        }
        connection.execute(
            text(
                "UPDATE panel_versions SET status='published', "
                "published_at=COALESCE(published_at, now()) "
                "WHERE id=:id AND project_id=:project_id"
            ),
            params,
        )
        connection.execute(
            text(
                "UPDATE panel_definitions SET status='active', "
                "active_version_id=:id, updated_at=now() "
                "WHERE id=:definition_id AND project_id=:project_id"
            ),
            params,
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.panel_version_published",
            actor,
            "panel_version",
            version_id,
            {
                "panel_definition_id": version["panel_definition_id"],
                "version": version["version"],
            },
        )
        return self.get_panel_version(connection, project_id, version_id)

    def list_panels(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        ids = connection.execute(
            text(
                "SELECT id FROM panel_definitions WHERE project_id=:project_id "
                "ORDER BY name, panel_key"
            ),
            {"project_id": project_id},
        ).scalars().all()
        return [self.get_panel(connection, project_id, panel_id) for panel_id in ids]

    def get_panel(
        self, connection: Connection, project_id: str, panel_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT d.*, COALESCE((
                  SELECT count(*) FROM panel_versions v
                  WHERE v.panel_definition_id=d.id
                ),0) AS version_count,
                COALESCE((
                  SELECT count(*) FROM panel_runs r
                  JOIN panel_versions v ON v.id=r.panel_version_id
                  WHERE v.panel_definition_id=d.id
                    AND r.status IN ('queued','resolving','running','waiting_member','waiting_decision')
                ),0) AS active_run_count
                FROM panel_definitions d
                WHERE d.id=:id AND d.project_id=:project_id
                """
            ),
            {"id": panel_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise PanelDefinitionNotFoundError("Panel definition not found.")
        result = self._serialize(dict(row))
        if row["active_version_id"]:
            result["active_version"] = self.get_panel_version(
                connection, project_id, row["active_version_id"]
            )
        else:
            result["active_version"] = None
        return result

    def get_panel_version(
        self, connection: Connection, project_id: str, version_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT v.*, d.panel_key, d.name AS panel_name, d.purpose
                FROM panel_versions v
                JOIN panel_definitions d ON d.id=v.panel_definition_id
                WHERE v.id=:id AND v.project_id=:project_id
                """
            ),
            {"id": version_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise PanelVersionNotFoundError("Panel version not found.")
        result = self._serialize(dict(row))
        members = connection.execute(
            text(
                """
                SELECT m.*, v.version AS agent_version, v.role_key,
                       d.agent_key, d.name AS agent_name
                FROM panel_version_members m
                JOIN agent_versions v ON v.id=m.agent_version_id
                JOIN agent_definitions d ON d.id=v.agent_definition_id
                WHERE m.panel_version_id=:version_id
                ORDER BY m.created_at, m.member_key
                """
            ),
            {"version_id": version_id},
        ).mappings().all()
        result["members"] = [self._serialize(dict(member)) for member in members]
        return result

    def start_panel_run(
        self,
        connection: Connection,
        *,
        project_id: str,
        panel_version_id: str,
        input_data: dict[str, Any],
        context: Optional[dict[str, Any]],
        actor: str,
        dedupe_key: Optional[str],
        task_id: Optional[str] = None,
        workflow_run_id: Optional[str] = None,
        budget_reservations: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        if dedupe_key:
            existing = connection.execute(
                text(
                    "SELECT id FROM panel_runs "
                    "WHERE project_id=:project_id AND dedupe_key=:dedupe_key"
                ),
                {"project_id": project_id, "dedupe_key": dedupe_key},
            ).scalar_one_or_none()
            if existing:
                result = self.get_panel_run(connection, project_id, existing)
                result["duplicate"] = True
                return result
        version = self.get_panel_version(connection, project_id, panel_version_id)
        if version["status"] != "published":
            raise PanelVersionStateError("Only published PanelVersions can run.")
        validation = self.validate_panel_version(
            connection, project_id, panel_version_id, actor=actor
        )
        if not validation["valid"]:
            raise PanelVersionStateError(
                "PanelVersion validation failed: " + "; ".join(validation["errors"])
            )
        run_id = make_id("panelrun")
        connection.execute(
            text(
                """
                INSERT INTO panel_runs (
                    id, project_id, panel_version_id, task_id, workflow_run_id,
                    status, input, context, policy_snapshot, dedupe_key, created_by
                )
                VALUES (
                    :id, :project_id, :panel_version_id, :task_id, :workflow_run_id,
                    'resolving', CAST(:input AS JSONB), CAST(:context AS JSONB),
                    CAST(:policy_snapshot AS JSONB), :dedupe_key, :created_by
                )
                """
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "panel_version_id": panel_version_id,
                "task_id": task_id,
                "workflow_run_id": workflow_run_id,
                "input": json.dumps(input_data),
                "context": json.dumps(context or {}),
                "policy_snapshot": json.dumps(
                    {
                        "panel_version_id": panel_version_id,
                        "consolidation_policy": version["consolidation_policy"],
                        "evidence_policy": version["evidence_policy"],
                        "dissent_policy": version["dissent_policy"],
                    }
                ),
                "dedupe_key": dedupe_key,
                "created_by": actor,
            },
        )
        member_runs: list[dict[str, Any]] = []
        reservations = budget_reservations or {}
        for member in version["members"]:
            member_runs.append(
                self.start_agent_run(
                    connection,
                    project_id=project_id,
                    agent_version_id=member["agent_version_id"],
                    input_data={
                        **input_data,
                        "panel_context": context or {},
                        "panel_member_key": member["member_key"],
                        "perspective": member.get("perspective"),
                        "evidence_refs": input_data.get("evidence_refs") or [],
                    },
                    actor=actor,
                    dedupe_key=(
                        f"{dedupe_key}:{member['member_key']}"
                        if dedupe_key
                        else f"{run_id}:{member['member_key']}"
                    ),
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                    panel_run_id=run_id,
                    member_key=member["member_key"],
                    budget_reservation_id=reservations.get(member["member_key"]),
                )
            )
        blocked = [run for run in member_runs if run["status"] == "blocked"]
        next_status = "blocked" if blocked else "running"
        connection.execute(
            text(
                """
                UPDATE panel_runs
                SET status=:status,
                    blocked_reason=:blocked_reason,
                    updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": run_id,
                "status": next_status,
                "blocked_reason": (
                    "one_or_more_members_blocked" if blocked else None
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.panel_run_started",
            actor,
            "panel_run",
            run_id,
            {
                "panel_version_id": panel_version_id,
                "member_run_ids": [run["id"] for run in member_runs],
                "status": next_status,
            },
        )
        return self.get_panel_run(connection, project_id, run_id)

    def get_panel_run(
        self, connection: Connection, project_id: str, run_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT r.*, v.version, v.panel_definition_id,
                       d.panel_key, d.name AS panel_name, d.purpose
                FROM panel_runs r
                JOIN panel_versions v ON v.id=r.panel_version_id
                JOIN panel_definitions d ON d.id=v.panel_definition_id
                WHERE r.id=:id AND r.project_id=:project_id
                """
            ),
            {"id": run_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise PanelVersionNotFoundError("Panel run not found.")
        result = self._serialize(dict(row))
        member_ids = connection.execute(
            text(
                "SELECT id FROM agent_studio_runs WHERE panel_run_id=:run_id "
                "ORDER BY created_at, member_key"
            ),
            {"run_id": run_id},
        ).scalars().all()
        result["member_runs"] = [
            self.get_agent_run(connection, project_id, member_id)
            for member_id in member_ids
        ]
        findings = connection.execute(
            text(
                "SELECT * FROM panel_findings WHERE panel_run_id=:run_id "
                "ORDER BY created_at, member_key"
            ),
            {"run_id": run_id},
        ).mappings().all()
        dissent = connection.execute(
            text(
                "SELECT * FROM panel_dissent WHERE panel_run_id=:run_id "
                "ORDER BY created_at, member_key"
            ),
            {"run_id": run_id},
        ).mappings().all()
        result["findings"] = [self._serialize(dict(item)) for item in findings]
        result["dissent"] = [self._serialize(dict(item)) for item in dissent]
        return result

    def list_panel_runs(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        ids = connection.execute(
            text(
                "SELECT id FROM panel_runs WHERE project_id=:project_id "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"project_id": project_id, "limit": limit},
        ).scalars().all()
        return [self.get_panel_run(connection, project_id, run_id) for run_id in ids]

    # ------------------------------------------------------------------
    # Job lifecycle integration
    # ------------------------------------------------------------------
    def record_job_success(
        self,
        connection: Connection,
        *,
        project_id: str,
        job_id: str,
        result: dict[str, Any],
        artifact_id: Optional[str],
        actor: str,
    ) -> None:
        run = connection.execute(
            text(
                """
                SELECT id, panel_run_id, member_key
                FROM agent_studio_runs
                WHERE project_id=:project_id AND job_id=:job_id
                FOR UPDATE
                """
            ),
            {"project_id": project_id, "job_id": job_id},
        ).mappings().first()
        if run is None:
            return
        evidence = list(result.get("evidence_refs") or [])
        if artifact_id:
            evidence.append(artifact_id)
        connection.execute(
            text(
                """
                UPDATE agent_studio_runs
                SET status='completed', output=CAST(:output AS JSONB),
                    evidence=CAST(:evidence AS JSONB), error=NULL,
                    completed_at=now(), updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": run["id"],
                "output": json.dumps(result),
                "evidence": json.dumps(evidence),
            },
        )
        if run["panel_run_id"]:
            connection.execute(
                text(
                    """
                    INSERT INTO panel_findings (
                        id, project_id, panel_run_id, agent_run_id, member_key,
                        finding, recommendation, evidence_refs, confidence
                    )
                    VALUES (
                        :id, :project_id, :panel_run_id, :agent_run_id, :member_key,
                        CAST(:finding AS JSONB), CAST(:recommendation AS JSONB),
                        CAST(:evidence_refs AS JSONB), :confidence
                    )
                    """
                ),
                {
                    "id": make_id("finding"),
                    "project_id": project_id,
                    "panel_run_id": run["panel_run_id"],
                    "agent_run_id": run["id"],
                    "member_key": run["member_key"] or "member",
                    "finding": json.dumps(
                        {
                            "summary": result.get("summary"),
                            "items": result.get("findings") or [],
                            "work_product": result.get("work_product") or {},
                        }
                    ),
                    "recommendation": json.dumps(
                        {"items": result.get("recommendations") or []}
                    ),
                    "evidence_refs": json.dumps(evidence),
                    "confidence": float(result.get("confidence") or 0.5),
                },
            )
            for decision in result.get("required_decisions") or []:
                connection.execute(
                    text(
                        """
                        INSERT INTO panel_dissent (
                            id, project_id, panel_run_id, agent_run_id, member_key,
                            dissent_kind, statement, evidence_refs
                        )
                        VALUES (
                            :id, :project_id, :panel_run_id, :agent_run_id,
                            :member_key, 'governance_hold', :statement,
                            CAST(:evidence_refs AS JSONB)
                        )
                        """
                    ),
                    {
                        "id": make_id("dissent"),
                        "project_id": project_id,
                        "panel_run_id": run["panel_run_id"],
                        "agent_run_id": run["id"],
                        "member_key": run["member_key"] or "member",
                        "statement": json.dumps(decision, sort_keys=True),
                        "evidence_refs": json.dumps(evidence),
                    },
                )
            self._consolidate_panel(connection, project_id, run["panel_run_id"], actor)
        emit_event(
            connection,
            project_id,
            "agent_studio.agent_run_completed",
            actor,
            "agent_studio_run",
            run["id"],
            {"job_id": job_id, "artifact_id": artifact_id},
        )

    def record_job_failure(
        self,
        connection: Connection,
        *,
        project_id: str,
        job_id: str,
        error: dict[str, Any],
        terminal: bool,
        actor: str,
    ) -> None:
        run = connection.execute(
            text(
                "SELECT id, panel_run_id FROM agent_studio_runs "
                "WHERE project_id=:project_id AND job_id=:job_id"
            ),
            {"project_id": project_id, "job_id": job_id},
        ).mappings().first()
        if run is None:
            return
        status = "deadlettered" if terminal else "queued"
        connection.execute(
            text(
                "UPDATE agent_studio_runs SET status=:status, "
                "error=CAST(:error AS JSONB), updated_at=now() WHERE id=:id"
            ),
            {"status": status, "error": json.dumps(error), "id": run["id"]},
        )
        if run["panel_run_id"]:
            self._consolidate_panel(connection, project_id, run["panel_run_id"], actor)
        emit_event(
            connection,
            project_id,
            (
                "agent_studio.agent_run_deadlettered"
                if terminal
                else "agent_studio.agent_run_retrying"
            ),
            actor,
            "agent_studio_run",
            run["id"],
            {"job_id": job_id, "error": error},
        )

    def _consolidate_panel(
        self,
        connection: Connection,
        project_id: str,
        panel_run_id: str,
        actor: str,
    ) -> None:
        run = connection.execute(
            text(
                """
                SELECT r.status, r.panel_version_id, v.quorum_policy,
                       v.evidence_policy, v.consolidation_policy, v.dissent_policy
                FROM panel_runs r
                JOIN panel_versions v ON v.id=r.panel_version_id
                WHERE r.id=:id AND r.project_id=:project_id
                FOR UPDATE
                """
            ),
            {"id": panel_run_id, "project_id": project_id},
        ).mappings().first()
        if run is None or run["status"] in {"completed", "cancelled"}:
            return
        members = connection.execute(
            text(
                """
                SELECT r.id, r.member_key, r.status, r.output, r.evidence,
                       m.weight, m.required
                FROM agent_studio_runs r
                JOIN panel_version_members m
                  ON m.panel_version_id=:panel_version_id
                 AND m.member_key=r.member_key
                WHERE r.panel_run_id=:panel_run_id
                ORDER BY r.created_at, r.member_key
                """
            ),
            {
                "panel_version_id": run["panel_version_id"],
                "panel_run_id": panel_run_id,
            },
        ).mappings().all()
        active = [
            member
            for member in members
            if member["status"] in {
                "queued",
                "leased",
                "running",
                "waiting_decision",
            }
        ]
        if active:
            connection.execute(
                text(
                    "UPDATE panel_runs SET status='waiting_member', updated_at=now() "
                    "WHERE id=:id"
                ),
                {"id": panel_run_id},
            )
            return
        completed = [member for member in members if member["status"] == "completed"]
        quorum = int((run["quorum_policy"] or {}).get("min_members") or len(members))
        missing_required = [
            member["member_key"]
            for member in members
            if member["required"] and member["status"] != "completed"
        ]
        if missing_required:
            connection.execute(
                text(
                    """
                    UPDATE panel_runs
                    SET status='failed', blocked_reason='required_panel_member_failed',
                        error=CAST(:error AS JSONB), updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": panel_run_id,
                    "error": json.dumps(
                        {"missing_required_members": missing_required}
                    ),
                },
            )
            return
        if len(completed) < quorum:
            connection.execute(
                text(
                    """
                    UPDATE panel_runs
                    SET status='failed', blocked_reason='panel_quorum_not_met',
                        error=CAST(:error AS JSONB), updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": panel_run_id,
                    "error": json.dumps(
                        {
                            "completed": len(completed),
                            "quorum": quorum,
                            "terminal_members": len(members),
                        }
                    ),
                },
            )
            return
        findings = connection.execute(
            text(
                "SELECT member_key, finding, recommendation, evidence_refs, confidence "
                "FROM panel_findings WHERE panel_run_id=:id ORDER BY created_at, member_key"
            ),
            {"id": panel_run_id},
        ).mappings().all()
        dissent = connection.execute(
            text(
                "SELECT member_key, dissent_kind, statement, evidence_refs, status "
                "FROM panel_dissent WHERE panel_run_id=:id ORDER BY created_at, member_key"
            ),
            {"id": panel_run_id},
        ).mappings().all()
        evidence_count = sum(len(item["evidence_refs"] or []) for item in findings)
        minimum_evidence = int(
            (run["evidence_policy"] or {}).get("minimum_evidence_refs") or 0
        )
        consolidation = {
            "kind": (run["consolidation_policy"] or {}).get(
                "kind", "attributed_synthesis"
            ),
            "quorum": {"required": quorum, "completed": len(completed)},
            "attributed_findings": [self._serialize(dict(item)) for item in findings],
            "dissent": [self._serialize(dict(item)) for item in dissent],
            "evidence_count": evidence_count,
            "consensus_is_not_proof": True,
        }
        if evidence_count < minimum_evidence:
            connection.execute(
                text(
                    """
                    UPDATE panel_runs
                    SET status='waiting_decision',
                        blocked_reason='panel_evidence_gate_not_met',
                        consolidation=CAST(:consolidation AS JSONB),
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {"id": panel_run_id, "consolidation": json.dumps(consolidation)},
            )
            emit_event(
                connection,
                project_id,
                "agent_studio.panel_waiting_for_evidence",
                actor,
                "panel_run",
                panel_run_id,
                {
                    "minimum_evidence_refs": minimum_evidence,
                    "actual_evidence_refs": evidence_count,
                },
            )
            return
        blocking_dissent_members = {
            str(key).removeprefix("block_on_").removesuffix("_dissent")
            for key, enabled in (run["dissent_policy"] or {}).items()
            if key.startswith("block_on_")
            and key.endswith("_dissent")
            and bool(enabled)
        }
        blocking_dissent = [
            item
            for item in dissent
            if item["status"] == "open"
            and item["member_key"] in blocking_dissent_members
        ]
        if blocking_dissent:
            consolidation["blocking_dissent"] = [
                self._serialize(dict(item)) for item in blocking_dissent
            ]
            connection.execute(
                text(
                    """
                    UPDATE panel_runs
                    SET status='waiting_decision',
                        blocked_reason='panel_blocking_dissent',
                        consolidation=CAST(:consolidation AS JSONB),
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {"id": panel_run_id, "consolidation": json.dumps(consolidation)},
            )
            emit_event(
                connection,
                project_id,
                "agent_studio.panel_waiting_on_dissent",
                actor,
                "panel_run",
                panel_run_id,
                {
                    "members": sorted(
                        {item["member_key"] for item in blocking_dissent}
                    )
                },
            )
            return
        output = {
            "disposition": "completed",
            "summary": (
                f"{len(completed)} panel members completed under "
                f"{consolidation['kind']}."
            ),
            "finding_count": len(findings),
            "dissent_count": len(dissent),
            "evidence_count": evidence_count,
            "governance_note": (
                "This panel output preserves attributed findings and does not "
                "replace evidence, approval, policy, budget or ownership gates."
            ),
        }
        connection.execute(
            text(
                """
                UPDATE panel_runs
                SET status='completed', consolidation=CAST(:consolidation AS JSONB),
                    output=CAST(:output AS JSONB), blocked_reason=NULL,
                    completed_at=now(), updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": panel_run_id,
                "consolidation": json.dumps(consolidation),
                "output": json.dumps(output),
            },
        )
        emit_event(
            connection,
            project_id,
            "agent_studio.panel_run_completed",
            actor,
            "panel_run",
            panel_run_id,
            output,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _compile_agent_context(
        self,
        connection: Connection,
        project_id: str,
        version: dict[str, Any],
    ) -> dict[str, Any]:
        role = connection.execute(
            text(
                "SELECT id, display_name, spec FROM roles "
                "WHERE project_id=:project_id AND role_key=:role_key "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"project_id": project_id, "role_key": version["role_key"]},
        ).mappings().first()
        if role is None:
            raise AgentVersionStateError(
                f"RoleSpec '{version['role_key']}' is no longer installed."
            )
        project_skills = self._project_skill_ids(connection, project_id)
        enabled_skills = self._enabled_project_skill_ids(connection, project_id)
        role_allowed = self._role_allowed_skill_ids(role["spec"] or {}, project_skills)
        requested: set[str] = set()
        for reference in version["requested_skills"]:
            requested.update(matching_skill_ids(reference, project_skills))
        effective = sorted(requested & enabled_skills & role_allowed)
        denied = sorted(requested - set(effective))
        data_boundary = normalize_data_boundary(
            version["scopes"].get("data_boundary")
        )
        prompts = version["prompts"]
        compiled_system = "\n\n".join(
            part
            for part in [
                str(prompts.get("system") or "").strip(),
                "Deterministic rules:\n- "
                + "\n- ".join(str(rule) for rule in version["rules"]),
                "Output contract:\n" + json.dumps(version["output_contract"], sort_keys=True),
                "BOSS invariants: no unreceipted effects; no spend without a "
                "reservation; no work without a lease; no silent drops.",
            ]
            if part
        )
        return {
            "agent_version_id": version["id"],
            "role_id": role["id"],
            "role_key": version["role_key"],
            "data_boundary": data_boundary,
            "requested_skills": sorted(requested),
            "effective_skills": effective,
            "denied_skills": denied,
            "model_policy": version["model_policy"],
            "limits": version["limits"],
            "a2a_policy": version["a2a_policy"],
            "agent_runtime": {
                "project_id": project_id,
                "job_kind": "agent.studio.execute",
                "matched": True,
                "compiled_prompt": {
                    "system": compiled_system,
                    "job": str(prompts.get("job") or ""),
                    "checklist": [
                        str(rule) for rule in version["rules"]
                    ],
                    "runtime_contract": version["output_contract"],
                    "invariants": [
                        "No external effect without a Receipt.",
                        "No spendy action without a BudgetReservation.",
                        "No worker execution without a lease.",
                        "No silent drops.",
                    ],
                },
                "runtime": {
                    "worker_target": "boss-worker",
                    "job_kinds": ["agent.studio.execute"],
                    "default_skill_ids": effective,
                    "preferred_capabilities": version["model_policy"].get(
                        "preferred_capabilities"
                    )
                    or [],
                    "configuration_schema": {},
                },
                "role": {
                    "role_key": version["role_key"],
                    "display_name": role["display_name"],
                    **dict(role["spec"] or {}),
                },
                "config": {
                    "agent_version_id": version["id"],
                    "model_policy": version["model_policy"],
                    "scopes": version["scopes"],
                    "limits": version["limits"],
                    "a2a_policy": version["a2a_policy"],
                },
            },
        }

    def _normalize_agent_version_content(self, **payload: Any) -> dict[str, Any]:
        model_policy = dict(payload.get("model_policy") or {})
        model_policy.setdefault("execution_mode", "deterministic_brief")
        model_policy.setdefault("selection_mode", "router")
        model_policy.setdefault("preferred_capabilities", [])
        prompts = dict(payload.get("prompts") or {})
        prompts.setdefault("developer", "")
        prompts.setdefault("operator", "")
        scopes = dict(payload.get("scopes") or {})
        scopes["data_boundary"] = normalize_data_boundary(
            scopes.get("data_boundary")
        )
        scopes.setdefault("project", "current")
        limits = dict(payload.get("limits") or {})
        limits.setdefault("max_steps", 8)
        limits.setdefault("max_calls", 8)
        limits.setdefault("timeout_seconds", 300)
        limits.setdefault("max_retries", 2)
        a2a_policy = dict(payload.get("a2a_policy") or {})
        a2a_policy.setdefault("publish", [])
        a2a_policy.setdefault("subscribe", [])
        return {
            "role_key": self._key(payload.get("role_key"), "role_key"),
            "agent_module_id": (
                str(payload["agent_module_id"]).strip()
                if payload.get("agent_module_id")
                else None
            ),
            "model_policy": model_policy,
            "prompts": prompts,
            "rules": list(payload.get("rules") or []),
            "requested_skills": sorted(
                {
                    str(item).strip()
                    for item in payload.get("requested_skills") or []
                    if str(item).strip()
                }
            ),
            "requested_integrations": sorted(
                {
                    str(item).strip()
                    for item in payload.get("requested_integrations") or []
                    if str(item).strip()
                }
            ),
            "scopes": scopes,
            "limits": limits,
            "output_contract": dict(payload.get("output_contract") or {}),
            "a2a_policy": a2a_policy,
            "provenance": dict(payload.get("provenance") or {}),
        }

    def _json_params(self, content: dict[str, Any]) -> dict[str, Any]:
        return {
            key: json.dumps(value) if isinstance(value, (dict, list)) else value
            for key, value in content.items()
        }

    def _seed_strip_unusable_skills(
        self,
        connection: Connection,
        project_id: str,
        version_payload: dict[str, Any],
    ) -> list[str]:
        """Drop requested skills that are not installed or not on the RoleSpec.

        Community seed must not fail the whole catalog because one pack file
        names a skill the live RoleSpec has not been refreshed to include.
        """
        requested = [
            str(item)
            for item in (version_payload.get("requested_skills") or [])
            if str(item).strip()
        ]
        if not requested:
            return []
        catalog = self._project_skill_ids(connection, project_id)
        role_key = str((version_payload.get("role_key") or "")).strip()
        allowed: Optional[set[str]] = None
        if role_key:
            row = connection.execute(
                text(
                    "SELECT spec FROM roles WHERE project_id=:p AND role_key=:r "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"p": project_id, "r": role_key},
            ).mappings().first()
            if row and row.get("spec"):
                allowed = self._role_allowed_skill_ids(row["spec"] or {}, catalog)
        keep: list[str] = []
        for skill in requested:
            matches = matching_skill_ids(skill, catalog)
            if not matches:
                continue
            if allowed is not None and not any(m in allowed for m in matches):
                continue
            keep.append(skill)
        return keep

    def _project_skill_ids(
        self, connection: Connection, project_id: str
    ) -> set[str]:
        return {
            str(item["id"])
            for item in EntitlementBrokerService(self.settings).list_skills(
                project_id, connection=connection
            )
        }

    def _enabled_project_skill_ids(
        self, connection: Connection, project_id: str
    ) -> set[str]:
        catalog = self._project_skill_ids(connection, project_id)
        disabled = set(
            connection.execute(
                text(
                    "SELECT skill_id FROM project_skills "
                    "WHERE project_id=:project_id AND enabled=false"
                ),
                {"project_id": project_id},
            ).scalars().all()
        )
        return catalog - disabled

    def _role_allowed_skill_ids(
        self, role_spec: dict[str, Any], catalog: set[str]
    ) -> set[str]:
        allowed: set[str] = set()
        for reference in (role_spec.get("permissions") or {}).get(
            "allowed_skills"
        ) or []:
            allowed.update(matching_skill_ids(str(reference), catalog))
        return allowed

    def _find_agent_by_key(
        self, connection: Connection, project_id: str, agent_key: str
    ) -> Optional[dict[str, Any]]:
        agent_id = connection.execute(
            text(
                "SELECT id FROM agent_definitions "
                "WHERE project_id=:project_id AND agent_key=:agent_key"
            ),
            {"project_id": project_id, "agent_key": agent_key},
        ).scalar_one_or_none()
        return (
            self.get_agent(connection, project_id, agent_id) if agent_id else None
        )

    def _find_panel_by_key(
        self, connection: Connection, project_id: str, panel_key: str
    ) -> Optional[dict[str, Any]]:
        panel_id = connection.execute(
            text(
                "SELECT id FROM panel_definitions "
                "WHERE project_id=:project_id AND panel_key=:panel_key"
            ),
            {"project_id": project_id, "panel_key": panel_key},
        ).scalar_one_or_none()
        return (
            self.get_panel(connection, project_id, panel_id) if panel_id else None
        )

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a mapping.")
        return payload

    def _bounded_int(
        self,
        errors: list[str],
        payload: dict[str, Any],
        key: str,
        minimum: int,
        maximum: int,
    ) -> None:
        try:
            value = int(payload.get(key))
        except (TypeError, ValueError):
            errors.append(f"{key} must be an integer.")
            return
        if value < minimum or value > maximum:
            errors.append(f"{key} must be between {minimum} and {maximum}.")

    def _required_text(self, value: Any, field: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError(f"{field} must not be blank.")
        return cleaned

    def _key(self, value: Any, field: str) -> str:
        cleaned = self._required_text(value, field).lower().replace("-", "_")
        if not all(char.isalnum() or char == "_" for char in cleaned):
            raise ValueError(f"{field} must contain only letters, numbers, and underscores.")
        return cleaned

    def _serialize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._serialize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._serialize(item) for item in value]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value
