from __future__ import annotations

import logging
import threading
from typing import Any

from .config import WorkerSettings
from .control_plane import ControlPlaneClient, LeasedJob


LOGGER = logging.getLogger("boss.worker")


class RetryableJobError(RuntimeError):
    """A transient effect failure that should use the durable job backoff path."""


AGENT_WORK_PRODUCT_STRATEGIES: dict[str, dict[str, Any]] = {
    "agent.metatron.status_brief": {
        "contract": "StatusBrief",
        "role_logic": "Summarize current state, stale data, decisions, risks, and next CEO actions.",
        "sections": [
            "freshness_and_scope",
            "what_changed",
            "top_risks",
            "decisions_needed",
            "next_actions",
        ],
        "evidence": ["task", "decision", "event", "budget", "queue"],
        "safety_gates": ["do_not_invent_state", "cite_references", "escalate_missing_data"],
        "required_decision_when": ["high_risk_action", "budget_change", "merge_or_deploy"],
    },
    "agent.coo.ops_digest": {
        "contract": "OpsDigest",
        "role_logic": "Inspect queue, leases, retries, deadletters, decisions, and visible incidents.",
        "sections": [
            "queue_state",
            "lease_health",
            "deadletters",
            "blockers",
            "recommended_remediation",
        ],
        "evidence": ["job", "deadletter", "decision", "event", "health_snapshot"],
        "safety_gates": ["no_silent_drops", "do_not_override_budget", "requeue_only_safe_jobs"],
        "required_decision_when": ["break_glass", "budget_change", "policy_override"],
    },
    "agent.cao.architecture_review": {
        "contract": "InvariantReport",
        "role_logic": "Review architecture changes against BOSS safety invariants and simplicity.",
        "sections": [
            "affected_invariants",
            "canonical_objects",
            "policy_and_budget_gates",
            "failure_modes",
            "simplification_options",
        ],
        "evidence": ["artifact", "schema", "policy", "decision", "event"],
        "safety_gates": ["preserve_receipts", "preserve_leases", "preserve_budget_gates"],
        "required_decision_when": ["schema_breaking_change", "safety_invariant_change"],
    },
    "agent.cto.implementation_plan": {
        "contract": "ImplementationPlan",
        "role_logic": "Convert approved scope into small slices with test, risk, and rollback gates.",
        "sections": [
            "implementation_slices",
            "affected_surfaces",
            "test_plan",
            "risk_hotspots",
            "sequencing_and_rollback",
        ],
        "evidence": ["task", "artifact", "test_report", "pull_request"],
        "safety_gates": ["small_reviewable_slices", "tests_required", "approval_for_scope_growth"],
        "required_decision_when": ["merge", "deploy", "budget_increase", "scope_expansion"],
    },
    "agent.pm.scope_brief": {
        "contract": "FeatureBrief",
        "role_logic": "Turn ambiguous product intent into scope, non-goals, acceptance criteria, and risks.",
        "sections": [
            "problem_statement",
            "in_scope",
            "non_goals",
            "acceptance_criteria",
            "open_questions",
        ],
        "evidence": ["prd", "task", "decision", "user_story"],
        "safety_gates": ["explicit_non_goals", "approval_for_scope_expansion"],
        "required_decision_when": ["material_scope_tradeoff", "high_risk_requirement"],
    },
    "agent.qa.validation_report": {
        "contract": "ValidationReport",
        "role_logic": "Validate acceptance criteria, test evidence, regressions, and release risk.",
        "sections": [
            "acceptance_coverage",
            "tests_run",
            "missing_tests",
            "regression_risks",
            "qa_verdict",
        ],
        "evidence": ["test_report", "artifact", "pull_request", "ci_check"],
        "safety_gates": ["block_without_evidence", "name_test_gaps"],
        "required_decision_when": ["waive_failed_tests", "accept_known_gap"],
    },
    "agent.release_manager.release_packet": {
        "contract": "ReleasePacket",
        "role_logic": "Prepare release evidence, rollout stages, rollback plan, and approval cards.",
        "sections": [
            "release_summary",
            "validation_evidence",
            "rollout_stages",
            "rollback_plan",
            "approval_requirements",
        ],
        "evidence": ["pull_request", "test_report", "decision", "receipt", "deployment_run"],
        "safety_gates": ["rollback_required", "approval_required_for_deploy"],
        "required_decision_when": ["staging_deploy", "production_deploy", "failed_check_waiver"],
    },
    "agent.program_manager.delivery_plan": {
        "contract": "DependencyMap",
        "role_logic": "Track milestones, owners, dependencies, blockers, and follow-up work.",
        "sections": [
            "milestones",
            "dependencies",
            "owners_and_dates",
            "blockers",
            "meeting_or_decision_needs",
        ],
        "evidence": ["task", "meeting", "decision", "artifact"],
        "safety_gates": ["owners_required", "durable_followups_required"],
        "required_decision_when": ["priority_change", "blocked_dependency", "cross_role_conflict"],
    },
    "agent.vp_agents.role_review": {
        "contract": "RoleReview",
        "role_logic": "Review role quality, drift signals, eval evidence, trust, and autonomy changes.",
        "sections": [
            "rolespec_quality",
            "drift_signals",
            "eval_evidence",
            "trust_and_autonomy",
            "recommended_changes",
        ],
        "evidence": ["role", "eval", "decision", "artifact", "event"],
        "safety_gates": ["no_silent_autonomy_upgrade", "evidence_required"],
        "required_decision_when": ["autonomy_upgrade", "high_risk_skill_install"],
    },
    "agent.providerops_manager.providerops_brief": {
        "contract": "ProviderOpsBrief",
        "role_logic": "Review provider connections, resource inventory, ChangeSets, and provider risk.",
        "sections": [
            "provider_health",
            "permission_gaps",
            "unmanaged_resources",
            "open_changesets",
            "risk_escalations",
        ],
        "evidence": [
            "provider_account",
            "external_resource",
            "changeset",
            "resource_receipt",
            "cost_event",
        ],
        "safety_gates": ["changeset_required", "resource_receipt_required", "no_raw_secrets"],
        "required_decision_when": ["provider_write", "destructive_action", "production_dns"],
    },
    "agent.finops_sentinel.cost_brief": {
        "contract": "BudgetPressureBrief",
        "role_logic": "Inspect spend, confidence labels, anomalies, commitments, and budget pressure.",
        "sections": [
            "spend_posture",
            "confidence_labels",
            "anomalies",
            "commitments",
            "budget_safe_actions",
        ],
        "evidence": ["cost_event", "budget_reservation", "commitment", "provider_account"],
        "safety_gates": ["never_present_estimate_as_exact", "budget_governor_wins"],
        "required_decision_when": ["budget_increase", "spend_freeze_override"],
    },
    "agent.infra_steward.infra_review": {
        "contract": "InfraReadinessReview",
        "role_logic": "Evaluate provider boundaries, local/cloud placement, worker pools, and rollback posture.",
        "sections": [
            "resource_readiness",
            "provider_boundaries",
            "local_cloud_placement",
            "worker_pool_implications",
            "cost_and_reliability_tradeoffs",
        ],
        "evidence": ["changeset", "external_resource", "worker_pool", "provider_health"],
        "safety_gates": ["no_direct_provider_write", "prefer_local_first", "rollback_ready"],
        "required_decision_when": ["provisioning", "destructive_change", "production_boundary_change"],
    },
    "agent.deployment_manager.deployment_review": {
        "contract": "DeploymentPlan",
        "role_logic": "Assess deployment target readiness, evidence gaps, health checks, and rollback points.",
        "sections": [
            "target_readiness",
            "release_evidence_gaps",
            "health_check_plan",
            "rollback_points",
            "receipt_expectations",
        ],
        "evidence": ["deployment_run", "deployment_target", "changeset", "resource_receipt"],
        "safety_gates": ["deployment_changeset_required", "health_check_required"],
        "required_decision_when": ["staging_deploy", "production_deploy", "rollback_execution"],
    },
    "agent.resource_janitor.cleanup_brief": {
        "contract": "CleanupChangeSetProposal",
        "role_logic": "Find orphaned resources and waste, estimate savings, and propose cleanup ChangeSets.",
        "sections": [
            "orphan_candidates",
            "ttl_findings",
            "estimated_savings",
            "cleanup_changeset_candidates",
            "rollback_or_recovery_notes",
        ],
        "evidence": ["external_resource", "drift_report", "cost_event", "changeset"],
        "safety_gates": ["no_auto_delete_production", "approval_for_destructive_cleanup"],
        "required_decision_when": ["destructive_cleanup", "production_impacting_cleanup"],
    },
    "agent.secrets_steward.secret_boundary_review": {
        "contract": "SecretBoundaryReview",
        "role_logic": "Review SecretRefs, provider scopes, rotation posture, and local-runner secret boundaries.",
        "sections": [
            "secretref_inventory",
            "over_permission_risks",
            "rotation_needs",
            "local_runner_boundaries",
            "least_privilege_recommendations",
        ],
        "evidence": ["secret_ref", "provider_account", "capability", "policy"],
        "safety_gates": ["never_expose_raw_secret", "secretref_only"],
        "required_decision_when": ["break_glass_secret_access", "provider_scope_expansion"],
    },
    "agent.model_spend_analyst.model_spend_brief": {
        "contract": "ModelSpendBrief",
        "role_logic": "Review model capacity, route decisions, cooldowns, cost receipts, and policy limits.",
        "sections": [
            "capacity_posture",
            "route_decision_quality",
            "cooldowns_and_limits",
            "cost_receipt_linkage",
            "authorized_optimization",
        ],
        "evidence": ["routing_decision", "capacity_receipt", "cost_receipt", "budget_policy"],
        "safety_gates": ["do_not_bypass_limits", "no_paid_api_without_budget"],
        "required_decision_when": ["paid_fallback", "subscription_policy_conflict"],
    },
}


def capabilities() -> list[str]:
    return [
        "agent.cao.architecture_review",
        "agent.coo.ops_digest",
        "agent.cto.implementation_plan",
        "agent.deployment_manager.deployment_review",
        "agent.finops_sentinel.cost_brief",
        "agent.infra_steward.infra_review",
        "agent.metatron.status_brief",
        "agent.model_spend_analyst.model_spend_brief",
        "agent.pm.scope_brief",
        "agent.program_manager.delivery_plan",
        "agent.providerops_manager.providerops_brief",
        "agent.qa.validation_report",
        "agent.release_manager.release_packet",
        "agent.resource_janitor.cleanup_brief",
        "agent.secrets_steward.secret_boundary_review",
        "agent.vp_agents.role_review",
        "enterprise.marketplace.validate",
        "providerops.sync.readonly",
        "plan.command",
        "plan.generate",
        "openai.plan",
        "meeting.step",
        "code.task",
        "docs.update",
        "agent.resume",
        "software_factory.propose_pr",
        "github.pull_request.merge",
        "github.deployment.create",
        "infra.droplet.create",
        "infra.droplet.delete",
        "comms.outbox.deliver",
        "webhook.outbound.deliver",
        "github.triage",
        "github.pr_review",
        "skill.execute",
    ]


def build_agent_brief_result(job: LeasedJob) -> dict[str, Any]:
    agent_runtime = job.payload.get("agent_runtime") or {}
    agent_module = job.payload.get("agent_module") or {}
    role = agent_runtime.get("role") if isinstance(agent_runtime.get("role"), dict) else {}
    compiled_prompt = (
        agent_runtime.get("compiled_prompt")
        if isinstance(agent_runtime.get("compiled_prompt"), dict)
        else {}
    )
    role_name = role.get("display_name") or agent_module.get("display_name") or job.kind
    prompt_job = str(compiled_prompt.get("job") or "").strip()
    payload_summary = job.payload.get("summary") or job.payload.get("command") or job.kind
    strategy = AGENT_WORK_PRODUCT_STRATEGIES.get(job.kind) or {
        "contract": "AgentBrief",
        "role_logic": "Produce a bounded role brief from supplied context.",
        "sections": ["summary", "findings", "recommendations", "required_decisions"],
        "evidence": ["task", "artifact", "event"],
        "safety_gates": ["respect_role_boundaries", "escalate_missing_data"],
        "required_decision_when": ["scope_expansion", "policy_override"],
    }
    grounding_context = _agent_grounding_context(job)
    grounding_status = "grounded_in_payload_context" if grounding_context else "needs_control_plane_context"
    findings = [
        f"{role_name} executed {strategy['contract']} logic for {job.kind}.",
        str(strategy["role_logic"]),
        f"Grounding status: {grounding_status}.",
    ]
    recommendations = [
        f"Populate sections: {', '.join(strategy['sections'])}.",
        "Attach required evidence references before using this brief for approval.",
        "Escalate any action matching decision triggers: "
        + ", ".join(strategy["required_decision_when"]),
    ]
    required_decisions = _agent_required_decisions(job, strategy)
    return {
        "summary": f"{role_name}: {payload_summary}",
        "strategy_id": job.kind,
        "grounding_status": grounding_status,
        "agent_module": agent_module,
        "agent_runtime": {
            "job_kind": agent_runtime.get("job_kind"),
            "matched": agent_runtime.get("matched"),
            "role_key": role.get("role_key"),
            "checklist": compiled_prompt.get("checklist") or [],
        },
        "work_product": {
            "contract": strategy["contract"],
            "role_logic": strategy["role_logic"],
            "sections": strategy["sections"],
            "evidence_requirements": strategy["evidence"],
            "safety_gates": strategy["safety_gates"],
            "decision_triggers": strategy["required_decision_when"],
            "grounding_context": grounding_context,
        },
        "findings": findings,
        "recommendations": recommendations,
        "required_decisions": required_decisions,
        "evidence_refs": job.payload.get("evidence_refs") or [],
        "prompt_excerpt": prompt_job[:1200],
    }


def _agent_grounding_context(job: LeasedJob) -> dict[str, Any]:
    context = job.payload.get("context")
    if isinstance(context, dict):
        return context
    grounded_keys = {
        "task_id",
        "artifact_ids",
        "decision_ids",
        "receipt_ids",
        "resource_ids",
        "changeset_ids",
        "routing_decision_ids",
        "cost_event_ids",
        "evidence_refs",
    }
    return {key: job.payload[key] for key in grounded_keys if key in job.payload}


def _agent_required_decisions(
    job: LeasedJob, strategy: dict[str, Any]
) -> list[dict[str, Any]]:
    required: list[dict[str, Any]] = []
    risk_tier = job.payload.get("risk_tier")
    if isinstance(risk_tier, int) and risk_tier >= 2:
        required.append(
            {
                "reason": "risk_tier_requires_approval",
                "risk_tier": risk_tier,
                "default_action": "hold",
            }
        )
    requested_actions = job.payload.get("requested_actions") or job.payload.get("actions")
    if not isinstance(requested_actions, list):
        requested_actions = []
    triggers = [str(item) for item in strategy.get("required_decision_when") or []]
    for action in requested_actions:
        action_text = str(action)
        if any(trigger.replace("_", " ") in action_text.replace("_", " ") for trigger in triggers):
            required.append(
                {
                    "reason": "strategy_trigger_requires_decision",
                    "triggered_by": action_text,
                    "default_action": "hold",
                }
            )
    return required


def build_plan_result(job: LeasedJob) -> dict[str, Any]:
    command = str(job.payload.get("command") or "Plan the requested task.")
    constraints = job.payload.get("constraints") or {}
    deadline = constraints.get("deadline")
    agent_runtime = job.payload.get("agent_runtime") or {}
    agent_module = job.payload.get("agent_module") or {}
    module_name = (
        agent_module.get("display_name")
        if isinstance(agent_module.get("display_name"), str)
        else None
    )
    prompt_bundle = (
        agent_module.get("prompt_bundle")
        if isinstance(agent_module.get("prompt_bundle"), dict)
        else {}
    )
    compiled_prompt = (
        agent_runtime.get("compiled_prompt")
        if isinstance(agent_runtime.get("compiled_prompt"), dict)
        else {}
    )
    planning_guidance = (
        compiled_prompt.get("job")
        if isinstance(compiled_prompt.get("job"), str)
        else prompt_bundle.get("planning")
    )

    steps = [
        "Clarify task scope and acceptance criteria.",
        "Break the work into small implementation slices.",
        "Run validation and capture receipts, artifacts, and decisions.",
    ]
    risks = [
        "Missing credentials or budget gates can block downstream execution.",
        "Unclear acceptance criteria can widen the task without approval.",
    ]
    done_when = [
        "A reviewable implementation plan exists as an artifact.",
        "A blocking decision card is ready for human approval.",
    ]
    if deadline:
        steps.append(f"Respect the requested deadline: {deadline}.")
    if isinstance(planning_guidance, str) and planning_guidance.strip():
        steps.append(f"Module guidance: {planning_guidance.strip()}")

    return {
        "summary": command,
        "objectives": [command],
        "steps": steps,
        "risks": risks,
        "done_when": done_when,
        "agent_module": {
            "id": agent_module.get("id"),
            "version": agent_module.get("version"),
            "display_name": module_name,
            "role_key": agent_module.get("role_key"),
        }
        if agent_module
        else {},
        "agent_runtime": {
            "job_kind": agent_runtime.get("job_kind"),
            "matched": agent_runtime.get("matched"),
            "checklist": compiled_prompt.get("checklist") or [],
        }
        if agent_runtime
        else {},
    }


def build_marketplace_validation_result(job: LeasedJob) -> dict[str, Any]:
    agent_runtime = job.payload.get("agent_runtime") or {}
    agent_module = job.payload.get("agent_module") or {}
    module_id = str(job.payload.get("module_id") or agent_module.get("id") or "unknown")
    required_entitlement = str(
        job.payload.get("required_entitlement")
        or job.payload.get("capability_key")
        or "private_agent_marketplace"
    )
    entitlement_status = str(job.payload.get("entitlement_status") or "unknown")
    allowed = entitlement_status in {"allowed", "active", "present", "granted"}
    status = "validated" if allowed else "blocked"
    return {
        "status": status,
        "summary": (
            f"Marketplace validation for {module_id}: {required_entitlement} "
            f"is {entitlement_status}."
        ),
        "module_id": module_id,
        "required_entitlement": required_entitlement,
        "entitlement_status": entitlement_status,
        "agent_module": agent_module,
        "agent_runtime": {
            "job_kind": agent_runtime.get("job_kind"),
            "matched": agent_runtime.get("matched"),
            "checklist": (
                (agent_runtime.get("compiled_prompt") or {}).get("checklist")
                if isinstance(agent_runtime.get("compiled_prompt"), dict)
                else []
            )
            or [],
        },
        "work_product": {
            "contract": "EntitlementGateReport",
            "sections": [
                "module_identity",
                "required_entitlement",
                "entitlement_status",
                "decision",
                "audit_references",
            ],
            "safety_gates": [
                "do_not_install_without_entitlement",
                "no_external_effects",
                "emit_audit_record",
            ],
            "decision_triggers": ["missing_entitlement", "private_registry_access"],
        },
        "findings": [
            f"Module {module_id} requires {required_entitlement}.",
            f"Entitlement status is {entitlement_status}.",
            "Private enterprise marketplace validation performs no external effects.",
        ],
        "recommendations": [
            "Allow installation only when the required entitlement is active.",
            "Preserve install-denial or install-success records for audit.",
        ],
        "required_decisions": []
        if allowed
        else [
            {
                "reason": "missing_marketplace_entitlement",
                "required_entitlement": required_entitlement,
                "default_action": "hold",
            }
        ],
        "evidence_refs": job.payload.get("evidence_refs") or [],
    }


def agent_module_id_from_payload(payload: dict[str, Any]) -> Any:
    if payload.get("agent_module_id"):
        return payload.get("agent_module_id")
    agent_module = payload.get("agent_module")
    if isinstance(agent_module, dict):
        return agent_module.get("id")
    return None


def execute_job(job: LeasedJob, client: ControlPlaneClient) -> dict[str, Any]:
    if job.kind == "skill.execute":
        skill_id = str(job.payload.get("skill_id") or "").strip()
        if not skill_id:
            raise ValueError("skill.execute requires payload.skill_id")
        result = client.invoke_skill(
            skill_id,
            {
                "project_id": job.project_id,
                "caller": job.payload.get("caller") or "worker",
                "inputs": job.payload.get("inputs") or {},
                "mock": False,
                "created_by_job_id": job.id,
                "worker_id": client.worker_id,
                "lease_token": job.lease_token,
            },
        )
        if result.get("status") != "succeeded":
            raise RuntimeError(
                result.get("reason") or "Real skill invocation failed."
            )
        return result
    if job.kind in {"github.triage", "github.pr_review"}:
        return client.run_github_community_job(
            job.id,
            {
                "project_id": job.project_id,
                "worker_id": client.worker_id,
                "lease_token": job.lease_token,
            },
        )
    if job.kind.startswith("agent.") and job.kind != "agent.resume":
        return build_agent_brief_result(job)
    if job.kind == "enterprise.marketplace.validate":
        return build_marketplace_validation_result(job)
    if job.kind in {"plan.command", "plan.generate"}:
        return build_plan_result(job)
    if job.kind == "openai.plan":
        result = client.create_openai_plan(
            {
                "project_id": job.project_id,
                "reservation_id": job.payload["reservation_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "prompt": job.payload["prompt"],
                "system_prompt": job.payload.get("system_prompt"),
                "agent_module_id": agent_module_id_from_payload(job.payload),
                "created_by_job_id": job.id,
                "task_id": job.task_id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "OpenAI plan job failed.")
        return result
    if job.kind == "meeting.step":
        result = client.run_meeting_step({"run_id": job.payload["run_id"]})
        if result.get("status") == "failed":
            raise RuntimeError(result.get("error") or "Meeting step failed.")
        return result
    if job.kind == "code.task":
        result = client.run_code_task({"run_id": job.payload["run_id"]})
        if result.get("status") == "failed":
            raise RuntimeError(result.get("error") or "Code task failed.")
        return result
    if job.kind == "docs.update":
        result = client.run_docs_update(
            {
                "project_id": job.project_id,
                "task_id": job.task_id,
                "event_id": job.payload["event_id"],
                "event_type": job.payload["event_type"],
                "entity_type": job.payload["entity_type"],
                "entity_id": job.payload["entity_id"],
                "data_boundary": job.payload.get("data_boundary", job.data_boundary),
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") in {"failed", "blocked"}:
            error = result.get("message") or "Documentation update failed."
            if result.get("retryable"):
                raise RetryableJobError(error)
            raise RuntimeError(error)
        return result
    if job.kind == "agent.resume":
        result = client.resume_agent_run({"run_id": job.payload["run_id"]})
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "Agent resume job failed.")
        return result
    if job.kind == "software_factory.propose_pr":
        result = client.create_pull_request_proposal(
            {
                "project_id": job.project_id,
                "task_id": job.task_id,
                "reservation_id": job.payload["reservation_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "title": job.payload["title"],
                "branch_name": job.payload["branch_name"],
                "summary": job.payload["summary"],
                "files": job.payload["files"],
                "agent_module_id": agent_module_id_from_payload(job.payload),
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(
                result.get("message") or "Software-factory proposal job failed."
            )
        return result
    if job.kind == "github.pull_request.merge":
        result = client.merge_pull_request(
            {
                "project_id": job.project_id,
                "decision_id": job.payload["decision_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "pull_request_number": job.payload["pull_request_number"],
                "pull_request_url": job.payload["pull_request_url"],
                "title": job.payload["title"],
                "branch_name": job.payload.get("branch_name"),
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "Pull-request merge failed.")
        return result
    if job.kind == "github.deployment.create":
        result = client.create_deployment(
            {
                "project_id": job.project_id,
                "decision_id": job.payload["decision_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "merged_sha": job.payload["merged_sha"],
                "pull_request_url": job.payload["pull_request_url"],
                "title": job.payload["title"],
                "branch_name": job.payload.get("branch_name"),
                "environment": job.payload["environment"],
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "Deployment creation failed.")
        return result
    if job.kind == "infra.droplet.create":
        result = client.create_infra_droplet(
            {
                "project_id": job.project_id,
                "task_id": job.task_id,
                "decision_id": job.payload["decision_id"],
                "reservation_id": job.payload["reservation_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "title": job.payload["title"],
                "region": job.payload["region"],
                "size": job.payload["size"],
                "ttl_minutes": job.payload["ttl_minutes"],
                "tags": job.payload.get("tags") or [],
                "data_boundary": job.payload.get("data_boundary"),
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(
                result.get("message") or "Infrastructure provisioning failed."
            )
        return result
    if job.kind == "infra.droplet.delete":
        result = client.terminate_infra_resource(
            job.payload["resource_id"],
            {
                "project_id": job.project_id,
                "dedupe_key": job.payload["dedupe_key"],
                "reason": job.payload.get("reason"),
                "created_by_job_id": job.id,
            },
        )
        if result.get("status") == "failed":
            raise RuntimeError(
                result.get("message") or "Infrastructure termination failed."
            )
        return result
    if job.kind == "comms.outbox.deliver":
        result = client.deliver_outbox_message(
            job.payload["outbox_message_id"],
            {
                "project_id": job.project_id,
                "reservation_id": job.payload["reservation_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "created_by_job_id": job.id,
            },
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "Outbound communication failed.")
        return result
    if job.kind == "webhook.outbound.deliver":
        result = client.deliver_webhook(
            job.payload["delivery_id"],
            {
                "project_id": job.project_id,
                "created_by_job_id": job.id,
                "worker_id": client.worker_id,
                "lease_token": job.lease_token,
            },
        )
        if result.get("status") == "failed":
            raise RetryableJobError(
                result.get("message") or "Outbound webhook delivery failed."
            )
        return result
    if job.kind == "providerops.sync.readonly":
        result = client.sync_provider_resources(
            {
                "project_id": job.project_id,
                "provider_account_id": job.payload.get("provider_account_id"),
                "actor": job.payload.get("actor") or "system",
            }
        )
        if any(run.get("status") == "failed" for run in result.get("runs") or []):
            failed = next(
                run for run in result.get("runs") or [] if run.get("status") == "failed"
            )
            message = ((failed.get("error") or {}).get("message")) or (
                "ProviderOps sync failed."
            )
            raise RuntimeError(message)
        return {
            "status": "succeeded",
            "summary": (
                f"ProviderOps read-only sync completed for "
                f"{len(result.get('runs') or [])} provider account(s)."
            ),
            "sync": result,
            "runs": result.get("runs") or [],
            "resources_seen": result.get("resources_seen", 0),
            "resources_changed": result.get("resources_changed", 0),
            "costs_seen": result.get("costs_seen", 0),
        }
    raise ValueError(f"Unsupported job kind: {job.kind}")


def run_worker_cycle(client: ControlPlaneClient, settings: WorkerSettings) -> bool:
    job = client.poll(
        settings.worker_id,
        capabilities(),
        settings.lease_ttl_seconds,
    )
    if not job:
        LOGGER.info("worker_idle worker_id=%s", settings.worker_id)
        return False

    LOGGER.info(
        "job_leased worker_id=%s job_id=%s kind=%s attempt=%s",
        settings.worker_id,
        job.id,
        job.kind,
        job.attempt,
    )
    client.heartbeat(
        settings.worker_id,
        job.id,
        job.lease_token,
        settings.lease_ttl_seconds,
    )

    # Keep the lease alive for the WHOLE (possibly many-minute) job. execute_job is a
    # single blocking call — e.g. code.task runs the entire governed pipeline, whose
    # write_tests/implement phases can each run minutes (code_agent_timeout is 900s).
    # Without renewing the lease mid-run it expires (default 300s) and the reclaimer
    # restarts the STILL-running job → an infinite loop on the longest phase. A daemon
    # thread renews every lease_ttl/2 (its OWN client — the main one is busy in
    # execute_job) until the job finishes.
    hb_stop = threading.Event()
    hb_client = ControlPlaneClient(settings.control_plane_url, api_key=settings.api_key)

    def _renew_lease() -> None:
        interval = max(15, settings.lease_ttl_seconds // 2)
        while not hb_stop.wait(interval):
            try:
                hb_client.heartbeat(
                    settings.worker_id, job.id, job.lease_token,
                    settings.lease_ttl_seconds,
                )
            except Exception:  # noqa: BLE001 — a transient heartbeat failure must not kill the job
                LOGGER.warning(
                    "heartbeat_renew_failed worker_id=%s job_id=%s",
                    settings.worker_id, job.id,
                )

    hb_thread = threading.Thread(target=_renew_lease, name=f"lease-hb-{job.id}", daemon=True)
    hb_thread.start()

    try:
        result = execute_job(job, client)
    except Exception as error:
        LOGGER.exception(
            "job_execution_failed worker_id=%s job_id=%s kind=%s",
            settings.worker_id,
            job.id,
            job.kind,
        )
        retryable = isinstance(error, RetryableJobError)
        retry_delay_seconds = (
            min(300, 5 * (2 ** min(max(job.attempt - 1, 0), 6)))
            if retryable
            else 0
        )
        client.fail(
            settings.worker_id,
            job.id,
            job.lease_token,
            {
                "message": str(error),
                "type": error.__class__.__name__,
            },
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
        )
        return True
    finally:
        hb_stop.set()
        hb_thread.join(timeout=3)
        try:
            hb_client.close()
        except Exception:  # noqa: BLE001
            pass

    completion = client.complete(
        settings.worker_id,
        job.id,
        job.lease_token,
        result,
    )
    LOGGER.info(
        "job_completed worker_id=%s job_id=%s status=%s artifact_id=%s decision_id=%s",
        settings.worker_id,
        job.id,
        completion["status"],
        completion.get("artifact_id"),
        completion.get("decision_id"),
    )
    return True
