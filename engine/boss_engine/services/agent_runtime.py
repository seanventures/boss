from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .specpack import AgentModuleNotFoundError, SpecPackService


class AgentRuntimeService:
    BOSS_INVARIANTS = [
        "No external effect without a Receipt.",
        "No spendy action without a BudgetReservation.",
        "No worker execution without a lease.",
        "No provider write without a ChangeSet.",
        "No provider action without a ResourceReceipt.",
        "No silent drops: retry, deadletter, or escalate visibly.",
        "No self-modification without PR/config diff, tests, approval, canary, and rollback.",
        "Budget Governor always wins over autoscaling or routing preferences.",
        "Optimize authorized capacity; do not bypass provider limits.",
        "Important work must become a durable artifact, not only chat.",
    ]

    def __init__(self, settings: Settings):
        self.settings = settings
        self.specpack = SpecPackService(settings)

    def resolve(
        self,
        connection: Connection,
        project_id: str,
        *,
        job_kind: str,
        agent_module_id: Optional[str] = None,
        task_payload: Optional[dict[str, Any]] = None,
        required: bool = True,
    ) -> dict[str, Any]:
        module = self._resolve_module(
            connection,
            project_id,
            job_kind=job_kind,
            agent_module_id=agent_module_id,
            required=required,
        )
        if module is None:
            return {
                "project_id": project_id,
                "job_kind": job_kind,
                "matched": False,
                "agent_module": None,
                "compiled_prompt": {},
                "runtime": {},
                "role": {},
                "config": {},
            }
        role = module.get("role") or {}
        role_key = role.get("role_key") or role.get("role_id")
        #: Who the installed agent is (agent id, version), for the identity line of its voice.
        identity: dict[str, Any] = {}
        instructions: list[dict[str, Any]] = []
        if role_key:
            installed_role = connection.execute(
                text(
                    "SELECT spec, display_name, agent_id, identity_version FROM roles "
                    "WHERE project_id = :project_id AND role_key = :role_key"
                ),
                {"project_id": project_id, "role_key": role_key},
            ).mappings().one_or_none()
            if installed_role is not None:
                role = dict(installed_role["spec"] or role)
                identity = {"role_key": role_key,
                            "display_name": installed_role["display_name"],
                            "agent_id": installed_role["agent_id"],
                            "identity_version": installed_role["identity_version"]}
                from .agent_instructions import for_prompt

                instructions = for_prompt(connection, project_id, role_key, "work")
        runtime = module.get("runtime") or {}
        prompt_bundle = module.get("prompt_bundle") or {}
        job_prompt = self._job_prompt(job_kind, prompt_bundle, runtime)
        compiled_prompt = {
            "system": self._compile_system_prompt(module, role, prompt_bundle,
                                                  identity=identity, instructions=instructions),
            "job": self._compile_job_prompt(job_kind, job_prompt, task_payload or {}),
            "checklist": self._compile_checklist(role, runtime),
            "runtime_contract": self._compile_runtime_contract(role, runtime),
            "invariants": self.BOSS_INVARIANTS,
        }
        return {
            "project_id": project_id,
            "job_kind": job_kind,
            "matched": True,
            "agent_module": self._module_summary(module),
            "compiled_prompt": compiled_prompt,
            "runtime": {
                "worker_target": runtime.get("worker_target"),
                "job_kinds": runtime.get("job_kinds") or [],
                "default_skill_ids": runtime.get("default_skill_ids") or [],
                "preferred_capabilities": runtime.get("preferred_capabilities") or [],
                "configuration_schema": runtime.get("configuration_schema") or {},
            },
            "role": {
                "role_key": role.get("role_key") or role.get("role_id"),
                "display_name": role.get("display_name"),
                "mission": role.get("mission"),
                "primary_outcomes": role.get("primary_outcomes") or [],
                "boundaries": role.get("boundaries") or {},
                "decision_rights": role.get("decision_rights") or {},
                "permissions": role.get("permissions") or {},
                "output_contracts": role.get("output_contracts") or [],
                "cadence": role.get("cadence") or {},
                "comms_contract": role.get("comms_contract") or {},
                "eval_rubric": role.get("eval_rubric") or {},
                "autonomy": role.get("autonomy") or {},
            },
            "config": module.get("config") or {},
        }

    def attach_to_payload(
        self,
        connection: Connection,
        project_id: str,
        *,
        job_kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if payload.get("agent_runtime") and payload.get("agent_module"):
            return payload
        runtime_context = self.resolve(
            connection,
            project_id,
            job_kind=job_kind,
            agent_module_id=self._optional_string(payload.get("agent_module_id")),
            task_payload=payload,
            required=False,
        )
        if not runtime_context["matched"]:
            return payload
        enriched = dict(payload)
        enriched["agent_runtime"] = runtime_context
        enriched["agent_module"] = runtime_context["agent_module"]
        enriched.setdefault(
            "agent_module_id",
            runtime_context["agent_module"]["id"],
        )
        return enriched

    def _resolve_module(
        self,
        connection: Connection,
        project_id: str,
        *,
        job_kind: str,
        agent_module_id: Optional[str],
        required: bool,
    ) -> Optional[dict[str, Any]]:
        if agent_module_id:
            try:
                module = self.specpack.resolve_installed_agent_module(
                    connection,
                    project_id,
                    agent_module_id,
                )
            except AgentModuleNotFoundError:
                if required:
                    raise
                return self.specpack.find_default_agent_module_for_job_kind(
                    connection,
                    project_id,
                    job_kind,
                )
            runtime = module.get("runtime") or {}
            if job_kind not in (runtime.get("job_kinds") or []):
                if not required:
                    return self.specpack.find_default_agent_module_for_job_kind(
                        connection,
                        project_id,
                        job_kind,
                    )
                raise AgentModuleNotFoundError(
                    f"Agent module '{agent_module_id}' is not configured for job kind '{job_kind}'."
                )
            return module
        module = self.specpack.find_default_agent_module_for_job_kind(
            connection,
            project_id,
            job_kind,
        )
        if module is None and required:
            raise AgentModuleNotFoundError(
                f"No installed agent module is configured for job kind '{job_kind}'."
            )
        return module

    def _module_summary(self, module: dict[str, Any]) -> dict[str, Any]:
        role = module.get("role") or {}
        return {
            "id": module.get("id"),
            "version": module.get("version"),
            "display_name": module.get("display_name"),
            "publisher": module.get("publisher"),
            "category": module.get("category"),
            "role_key": role.get("role_key") or role.get("role_id"),
        }

    def _compile_system_prompt(
        self,
        module: dict[str, Any],
        role: dict[str, Any],
        prompt_bundle: dict[str, Any],
        *,
        identity: Optional[dict[str, Any]] = None,
        instructions: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        sections = [
            str(prompt_bundle.get("system") or "").strip(),
            f"Role: {role.get('display_name') or role.get('role_key') or module.get('display_name')}.",
            f"Mission: {role.get('mission') or 'Execute the assigned BOSS role.'}",
            self._format_list("Primary outcomes", role.get("primary_outcomes") or []),
            self._format_mapping("Boundaries", role.get("boundaries") or {}),
            self._format_mapping("Decision rights", role.get("decision_rights") or {}),
            self._format_mapping("Permissions", role.get("permissions") or {}),
            self._format_list("Output contracts", role.get("output_contracts") or []),
            self._format_mapping("Cadence", role.get("cadence") or {}),
            self._format_mapping("Communication contract", role.get("comms_contract") or {}),
            self._format_mapping("Evaluation rubric", role.get("eval_rubric") or {}),
            self._format_mapping("Autonomy", role.get("autonomy") or {}),
            self._format_list("BOSS invariants", self.BOSS_INVARIANTS),
            # THE AGENT'S OWN VOICE, the same one it has in chat and on a panel
            # (agent_persona L1 + L3): who it is, that it is an AI, and how it speaks. The
            # remit above stays the authority on what it may do.
            self._agent_voice(role, identity or {}, instructions),
        ]
        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _agent_voice(role: dict[str, Any], identity: dict[str, Any],
                     instructions: Optional[list[dict[str, Any]]] = None) -> str:
        from .agent_persona import identity_and_voice

        try:
            return identity_and_voice({**identity, "spec": role}, mode="work",
                                      instructions=instructions)
        except Exception:  # noqa: BLE001 — a work prompt must still compile
            return ""

    def _compile_job_prompt(
        self,
        job_kind: str,
        job_prompt: str,
        task_payload: dict[str, Any],
    ) -> str:
        payload_summary = {
            key: value
            for key, value in task_payload.items()
            if key
            in {
                "command",
                "title",
                "summary",
                "constraints",
                "dedupe_key",
                "data_boundary",
                "risk_tier",
            }
        }
        sections = [
            f"Job kind: {job_kind}.",
            job_prompt.strip(),
            f"Payload context: {payload_summary}" if payload_summary else "",
            "Return durable, reference-friendly output with blockers, risks, evidence, and next decisions.",
        ]
        return "\n\n".join(section for section in sections if section)

    def _compile_checklist(
        self,
        role: dict[str, Any],
        runtime: dict[str, Any],
    ) -> list[str]:
        checklist = [
            "Confirm the leased job scope and data boundary before acting.",
            "Use only allowed skills and escalate forbidden or out-of-scope actions.",
            "Create artifacts/events for material findings or outputs.",
        ]
        permissions = role.get("permissions") or {}
        allowed_skills = permissions.get("allowed_skills") or []
        forbidden_skills = permissions.get("forbidden_skills") or []
        if allowed_skills:
            checklist.append("Role allowed skills: " + ", ".join(allowed_skills))
        if forbidden_skills:
            checklist.append("Forbidden skills: " + ", ".join(forbidden_skills))
        decision_rights = role.get("decision_rights") or {}
        escalations = decision_rights.get("must_escalate") or []
        if escalations:
            checklist.append("Escalate before: " + ", ".join(escalations))
        autonomy_level = ((role.get("autonomy") or {}).get("level"))
        if isinstance(autonomy_level, int):
            checklist.append(
                f"Respect autonomy level {autonomy_level}; do not exceed role rights."
            )
        if runtime.get("default_skill_ids"):
            checklist.append(
                "Default skills: " + ", ".join(runtime.get("default_skill_ids") or [])
            )
        for contract in role.get("output_contracts") or []:
            checklist.append(f"Produce or update output contract: {contract}.")
        return checklist

    def _compile_runtime_contract(
        self,
        role: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        permissions = role.get("permissions") or {}
        decision_rights = role.get("decision_rights") or {}
        return {
            "worker_target": runtime.get("worker_target"),
            "job_kinds": runtime.get("job_kinds") or [],
            "allowed_skills": permissions.get("allowed_skills") or [],
            "forbidden_skills": permissions.get("forbidden_skills") or [],
            "must_escalate": decision_rights.get("must_escalate") or [],
            "output_contracts": role.get("output_contracts") or [],
            "cadence": role.get("cadence") or {},
            "eval_rubric": role.get("eval_rubric") or {},
            "autonomy": role.get("autonomy") or {},
            "configuration_schema": runtime.get("configuration_schema") or {},
        }

    def _job_prompt(
        self,
        job_kind: str,
        prompt_bundle: dict[str, Any],
        runtime: dict[str, Any],
    ) -> str:
        prompt_key_by_kind = runtime.get("prompt_key_by_job_kind") or {}
        prompt_key = prompt_key_by_kind.get(job_kind)
        if isinstance(prompt_key, str) and prompt_key in prompt_bundle:
            return str(prompt_bundle[prompt_key])
        candidates = [
            job_kind.replace(".", "_"),
            "planning" if "plan" in job_kind else "",
            "review_packet" if "review" in job_kind or "proposal" in job_kind else "",
            "status_query" if "status" in job_kind or "digest" in job_kind else "",
            "system",
        ]
        for candidate in candidates:
            if candidate and isinstance(prompt_bundle.get(candidate), str):
                return str(prompt_bundle[candidate])
        return ""

    def _format_list(self, label: str, values: list[Any]) -> str:
        items = [str(value).strip() for value in values if str(value).strip()]
        if not items:
            return ""
        return f"{label}:\n" + "\n".join(f"- {item}" for item in items)

    def _format_mapping(self, label: str, values: dict[str, Any]) -> str:
        if not values:
            return ""
        lines = [f"{label}:"]
        for key, value in values.items():
            if isinstance(value, list):
                lines.append(f"- {key}: " + ", ".join(str(item) for item in value))
            else:
                lines.append(f"- {key}: {value}")
        return "\n".join(lines)

    def _optional_string(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None
