from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .config import WorkerSettings
from .control_plane import ControlPlaneClient, LeasedJob


LOGGER = logging.getLogger("boss.worker")

#: Set when the container has been asked to stop (SIGTERM from a deploy). Lives here rather
#: than in main.py because the code that can act on it — the lease heartbeat thread — is here;
#: main.py's signal handler sets it. An Event, not a bool, so a waiter can block on it.
SHUTDOWN = threading.Event()

#: The lease TTL we re-heartbeat with when stopping, so the job is reclaimable almost at once.
#: Not zero: a lease already in the past could race the reclaimer against a worker that is
#: still, briefly, alive. A few seconds is enough to be sure we are gone.
SHUTDOWN_LEASE_TTL_SECONDS = 5


def _wait_in_slices(stop: threading.Event, seconds: float, slice_s: float = 1.0, *,
                    deadline: Callable[[], float | None] | None = None) -> bool:
    """Wait up to ``seconds`` for ``stop``, checking SHUTDOWN every ``slice_s``.

    Returns True if ``stop`` was set, False when the interval, shutdown request,
    or optional absolute deadline needs the caller's attention.
    """
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if stop.wait(min(slice_s, remaining)):
            return True
        expires = deadline() if deadline is not None else None
        if SHUTDOWN.is_set() or (expires is not None and time.monotonic() >= expires):
            return False
        remaining -= slice_s
    return False


def job_progressing(heartbeat_response: Any) -> bool:
    """Did the control plane say this job's work is alive and making progress?

    Only an explicit True counts: an older control plane, a non-code job or an unreadable answer
    all mean "no evidence", and the runtime ceiling applies as before."""
    return isinstance(heartbeat_response, dict) and heartbeat_response.get("progressing") is True


class RetryableJobError(RuntimeError):
    """A transient effect failure that should use the durable job backoff path."""


# Claims frozen / missing host task: the code-task run is already terminal.
# Raising here would fail() the job into the DLQ. Complete instead.
SANDBOX_FAIL_CLOSED_REASON_CODES = frozenset(
    {"microvm_claims_frozen", "sandbox_task_missing"}
)


def is_sandbox_fail_closed(result: dict[str, Any] | None) -> bool:
    """True when a code.task result should complete (not DLQ) as a 422-class stop."""
    if not isinstance(result, dict):
        return False
    code = str(result.get("reason_code") or "")
    if code in SANDBOX_FAIL_CLOSED_REASON_CODES:
        return True
    blob = " ".join(
        str(result.get(key) or "")
        for key in ("error", "message", "detail")
    ).lower()
    return (
        "claims_frozen" in blob
        or "not claimed for community ga" in blob
        or "sandbox host task missing" in blob
        or ("404" in blob and "sandbox" in blob)
    )


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
    "agent.studio.execute": {
        "contract": "AgentStudioWorkProduct",
        "role_logic": (
            "Execute the immutable Agent Studio version inside its compiled "
            "RoleSpec, scope, skill, integration, model, and budget envelope."
        ),
        "sections": [
            "objective",
            "findings",
            "recommendations",
            "dissent",
            "required_decisions",
        ],
        "evidence": ["agent_version", "artifact", "event", "receipt"],
        "safety_gates": [
            "effective_capability_is_intersection",
            "preserve_role_authority",
            "evidence_before_claim",
        ],
        "required_decision_when": [
            "scope_expansion",
            "policy_override",
            "budget_change",
        ],
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
        "agent.studio.execute",
        # Control capability: advances a dispatched plan's ready phase tasks.
        # Not a work-product brief (see AGENT_WORK_PRODUCT_STRATEGIES exclusion).
        "agent.orchestrator.next_move",
        "a2a.dispatch",
        "enterprise.marketplace.validate",
        "providerops.sync.readonly",
        "work_tracker.issue.sync",
        "plan.command",
        "plan.generate",
        "openai.plan",
        "meeting.step",
        "code.task",
        # A real debugging session: shell, repo, read-only SQL, the fleet. Declared here or the
        # worker will not lease the job and the investigation sits queued forever.
        "investigation.run",
        # Governed Discovery research: each stage reserves, leases and receipts its own spend.
        "discovery.run",
        # An approved stack deployment (boss.deploy.yaml): the API applies its ChangeSet under
        # this job's lease. Declared here or the worker never leases it and the deploy sits
        # `running` at step one.
        "deployment.stack_apply",
        # BOSS fixes itself (services/self_fix_build.py): Mender's build of a fix to BOSS's own
        # code; the API runs it under this job's lease. Declared here or it sits queued.
        "self_fix.build",
        "infra.operation",
        # An approved host deployment (services/governed_host_deploy.py): the API applies its
        # ChangeSet through the SSH connector under this job's lease.
        "deployment.host_apply",
        "docs.update",
        # A security scan (F4): the API runs the scanners in the build sandbox under this lease.
        "security.scan",
        # The code graph of a registered repository (knowledge/repo_index.py): the API fetches
        # the branch and indexes it under this lease. Declared here or it sits queued.
        "knowledge.repository.index",
        # The onboarding agent (project_experience/onboarding.py): the API checks access, maps
        # the repository and proposes the backlog under this lease. Declared here or it sits queued.
        "project.onboarding",
        # Review this PR again (services/pr_review_again.py): the API runs the reviewer on the
        # pull request's real diff and posts the round. Declared here or it sits queued.
        "pull_request.review_again",
        # A build that finished while GitHub was down (github_outage): the API pushes its branch
        # from the local mirror and opens its PR under this lease. Declared here or it sits queued.
        "repo.push_pending",
        "agent.resume",
        "software_factory.propose_pr",
        "github.pull_request.merge",
        "change_set.merge",
        "github.deployment.create",
        "infra.droplet.create",
        "infra.droplet.delete",
        "comms.outbox.deliver",
        "webhook.outbound.deliver",
        "github.triage",
        "github.pr_review",
        "github.repository.reconcile",
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
    runtime_contract = compiled_prompt.get("runtime_contract")
    if job.kind == "agent.studio.execute" and isinstance(runtime_contract, dict):
        declared_sections = runtime_contract.get("sections")
        if isinstance(declared_sections, list) and declared_sections:
            strategy = {
                **strategy,
                "contract": str(
                    runtime_contract.get("artifact_type")
                    or strategy["contract"]
                ),
                "sections": [
                    str(section)
                    for section in declared_sections
                    if str(section).strip()
                ],
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
    if job.kind == "github.repository.reconcile":
        run_id = str(job.payload.get("run_id") or "").strip()
        if not run_id:
            raise ValueError(
                "github.repository.reconcile requires payload.run_id"
            )
        return client.run_github_reconciliation(
            run_id,
            {
                "project_id": job.project_id,
                "job_id": job.id,
                "worker_id": client.worker_id,
                "lease_token": job.lease_token,
            },
        )
    if job.kind == "a2a.dispatch":
        try:
            return client.dispatch_a2a_message(
                job.payload["message_id"],
                {
                    "project_id": job.project_id,
                    "worker_id": client.worker_id,
                    "lease_token": job.lease_token,
                    "created_by_job_id": job.id,
                },
            )
        except Exception as error:  # transport/API outages use durable retry
            raise RetryableJobError(str(error)) from error
    if job.kind == "work_tracker.issue.sync":
        try:
            result = client.sync_work_tracker(
                {
                    "project_id": job.project_id,
                    "binding_id": job.payload["binding_id"],
                    "worker_id": client.worker_id,
                    "lease_token": job.lease_token,
                    "created_by_job_id": job.id,
                }
            )
        except Exception as error:  # provider/API outages use durable retry
            raise RetryableJobError(str(error)) from error
        if result.get("status") != "succeeded":
            raise RetryableJobError(
                result.get("reason") or "Work-tracker sync failed."
            )
        return result
    if job.kind == "agent.orchestrator.next_move":
        event_data = job.payload.get("event_data")
        if not isinstance(event_data, dict):
            event_data = {}
        result = client.run_orchestrator_next_move(
            {
                "project_id": job.project_id,
                "event_id": job.payload.get("event_id"),
                "event_type": job.payload.get("event_type"),
                "entity_type": job.payload.get("entity_type"),
                "entity_id": job.payload.get("entity_id"),
                "event_data": event_data,
                "task_id": job.task_id or event_data.get("task_id"),
                "data_boundary": job.payload.get("data_boundary", job.data_boundary),
                "created_by_job_id": job.id,
                "worker_id": client.worker_id,
                "lease_token": job.lease_token,
            }
        )
        if result.get("status") in {"failed", "blocked"}:
            error = result.get("message") or "Orchestrator next_move failed."
            if result.get("retryable"):
                raise RetryableJobError(error)
            raise RuntimeError(error)
        return result
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
        # Send the lease token so the pipeline can FENCE its writes: a worker whose lease
        # was reclaimed must not be able to mutate the run any more. Without this the
        # pipeline has no way to know which execution it is.
        result = client.run_code_task({"run_id": job.payload["run_id"],
                                       "fencing_token": job.lease_token,
                                       "job_id": job.id})
        if result.get("status") == "failed":
            # Host 404 / claims frozen already marked the run failed. Completing
            # the job avoids a retry loop that deadletters the same 422.
            if is_sandbox_fail_closed(result):
                return result
            raise RuntimeError(result.get("error") or "Code task failed.")
        return result
    if job.kind == "discovery.run":
        # The research agent lives in the API container. The lease token goes with it: every
        # stage's model lease is bound to this job's lease, so a reclaimed job cannot spend.
        from boss_engine.services.jobs import job_run_id

        result = client.run_discovery({
            "run_id": job_run_id(job.kind, job.payload), "job_id": job.id,
            "worker_id": client.worker_id, "lease_token": job.lease_token,
        })
        if result.get("status") == "failed" and result.get("retry"):
            # A stage failed outright; the next attempt replays the stages already paid for.
            raise RuntimeError(result.get("error") or "Discovery stage failed.")
        return result
    if job.kind == "deployment.stack_apply":
        # A deploy of a whole stack takes minutes; the API owns it and the lease fences it.
        result = client.run_stack_deploy({
            "deployment_id": job.payload["deployment_id"], "job_id": job.id,
            "worker_id": client.worker_id, "lease_token": job.lease_token,
        })
        # Waiting on the target (another deployment is writing it) is not a failure: the API
        # has already handed the job back to the queue without spending an attempt, so there
        # is nothing to complete or fail here (see the job_deferred check after execution).
        return result
    if job.kind == "self_fix.build":
        # A BOSS self-fix build: diagnosis, red test, fix, gates, reviewers. The API owns it
        # under the lease; it ends at the approval card (or a refusal), never retried blind.
        return client.run_self_fix_build({
            "self_fix_id": job.payload["self_fix_id"], "job_id": job.id,
            "worker_id": client.worker_id, "lease_token": job.lease_token,
            "note": job.payload.get("note") or ""})
    if job.kind == "infra.operation":
        # One lifecycle hook on one provisioned resource (platform/operations.py); the API runs
        # it under this lease and writes its receipt.
        result = client.run_infra_operation({"job_id": job.id, "worker_id": client.worker_id,
                                             "lease_token": job.lease_token})
        if result.get("status") == "failed" and result.get("retry"):
            raise RuntimeError(result.get("error") or "Infrastructure operation failed.")
        return result
    if job.kind == "deployment.host_apply":
        # One app to a verified host through the SSH connector; the API owns the apply and the
        # lease fences it. A busy target hands the job back without spending an attempt.
        return client.run_host_deploy({
            "deployment_run_id": job.payload["deployment_run_id"], "job_id": job.id,
            "worker_id": client.worker_id, "lease_token": job.lease_token,
        })
    if job.kind == "investigation.run":
        # A long one by design: a real debugging session reads the code, the database and the
        # host, and forms more than one hypothesis. The API endpoint owns the timeout.
        # Its model call is governed under this lease (debug_session.InvestigationSpend).
        return client.run_investigation({"investigation_id": job.payload["investigation_id"],
                                         "job_id": job.id, "worker_id": client.worker_id,
                                         "lease_token": job.lease_token})
    if job.kind == "pull_request.review_again":
        # The reviewer and the GitHub connection live in the API container.
        return client.run_pr_review_again({"job_id": job.id, **(job.payload or {})})
    if job.kind == "knowledge.repository.index":
        # Parsing only, no model: the GitHub credential and the graph are in the API container.
        # A run the bounds refuse is a recorded result (status too_large, in words), not a retry.
        result = client.run_repository_index({"job_id": job.id, "worker_id": client.worker_id,
                                              "lease_token": job.lease_token})
        if result.get("status") == "failed" and result.get("retry"):
            raise RuntimeError(result.get("error") or "Repository index failed.")
        return result
    if job.kind == "project.onboarding":
        # One bounded pass of the onboarding agent; the API holds GitHub's credentials and the
        # graph. A pass that waits on the code graph queues its own next pass, so this completes.
        result = client.run_project_onboarding({"job_id": job.id, "worker_id": client.worker_id,
                                                "lease_token": job.lease_token})
        return result
    if job.kind == "repo.push_pending":
        # A build that finished while GitHub was down (github_outage): push its branch from the
        # local mirror and open its PR. GitHub still down is a recorded wait with its own next
        # try queued (backoff), never a failure of this job.
        return client.run_push_pending({"job_id": job.id, "worker_id": client.worker_id,
                                        "lease_token": job.lease_token})
    if job.kind == "security.scan":
        # The scanners and the sandbox helper are in the api image: the API runs the scan. A
        # scan whose tools failed is a recorded result (status failed, with why), not a retry.
        return client.run_security_scan({"scan_id": job.payload["scan_id"]})
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
        result = client.resume_agent_run({
            "run_id": job.payload["run_id"], "job_id": job.id,
            "worker_id": client.worker_id, "lease_token": job.lease_token,
        })
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
        # BLOCKED IS NOT MERGED. A merge the API could not perform returned "blocked" and the job
        # was marked succeeded, so nothing said the approved PR was still open (2026-11-06).
        # A moved head (409) or a GitHub hiccup retries with backoff (merge_failures); a
        # conflict or a superseded PR was handled by the API and is not raised at all.
        if result.get("status") == "failed" and result.get("retryable"):
            raise RetryableJobError(result.get("message") or "Pull-request merge failed.")
        if result.get("status") in ("failed", "blocked"):
            raise RuntimeError(result.get("message") or result.get("reason")
                               or "Pull-request merge failed.")
        return result
    if job.kind == "change_set.merge":
        # A multi-repo change after its one approval: merged as a set (heads re-checked, in
        # dependency order, reverted if one fails). The API owns the merge; this hands it over.
        result = client.merge_change_set({"change_set_id": job.payload["change_set_id"], "job_id": job.id,
                                          "worker_id": client.worker_id, "lease_token": job.lease_token})
        if result.get("status") == "failed" and not result.get("reverted") and not result.get("failed"):
            raise RuntimeError(result.get("message") or "Change set merge failed.")
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
                # The transport principal is the seeded system member. Keep
                # the API actor bound to that principal for this worker step.
                "actor": "system",
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


AI_SRE_OVERWATCH_INTERVAL_SECONDS = 60
_last_ai_sre_overwatch_monotonic = 0.0


def _tick_ai_sre_overwatch(settings: WorkerSettings) -> None:
    """Observe-only AI SRE tick. Fail-closed. Never raise out of the worker loop."""
    global _last_ai_sre_overwatch_monotonic
    now = time.monotonic()
    if now - _last_ai_sre_overwatch_monotonic < AI_SRE_OVERWATCH_INTERVAL_SECONDS:
        return
    try:
        from boss_engine.config import get_settings
        from boss_engine.db import get_engine
        from boss_engine.services.ai_sre_overwatch import safe_tick

        engine_settings = get_settings()
        project_id = (
            getattr(settings, "default_project_id", None)
            or getattr(engine_settings, "default_project_id", None)
            or getattr(settings, "project_id", None)
            or "boss-dogfood"
        )
        result = safe_tick(
            get_engine(engine_settings),
            engine_settings,
            project_id=str(project_id),
            actor="metatron",
        )
        _last_ai_sre_overwatch_monotonic = now
        if isinstance(result, dict) and result.get("status") not in {"empty", "observed", "in_progress"}:
            LOGGER.info(
                "ai_sre_overwatch status=%s reason=%s",
                result.get("status"),
                result.get("reason") or result.get("error"),
            )
    except Exception:  # noqa: BLE001
        LOGGER.debug("ai_sre_overwatch_failed", exc_info=True)


def _record_redelivery(client: ControlPlaneClient, job) -> None:
    """Note on the code task that this attempt is a REPLAY, so its timeline can say so.

    Best effort in every direction: a run that cannot be annotated must still be executed, and
    a job whose payload names no run (most kinds do not) simply has nothing to annotate.
    """
    run_id = None
    payload = getattr(job, "payload", None)
    if isinstance(payload, dict):
        run_id = payload.get("run_id") or payload.get("code_task_run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return
    try:
        client.note_code_task_redelivery(
            run_id.strip(), int(job.attempt), job.id, job.lease_token,
        )
    except Exception:  # noqa: BLE001 — annotation is never worth losing the job over
        LOGGER.debug("redelivery_note_failed job_id=%s", job.id, exc_info=True)


def _is_conflict(error: BaseException) -> bool:
    """A 409 from the control plane: the lease this worker holds is no longer its to settle."""
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 409


def _hand_back_after_conflict(client: ControlPlaneClient, settings: WorkerSettings,
                              job: LeasedJob, what: str, error: BaseException) -> None:
    """The control plane refused to settle this job (409). Hand the lease back and carry on.

    Raising here used to take the whole cycle down with the job still leased to this worker: it
    sat for a full lease TTL (300s) before anything could run it again, and a worker started
    with --drain-once simply exited. The API settles an expired-but-untaken lease, so a 409 now
    means the job is someone else's or already settled; handing back is then a no-op the API
    also refuses, which is fine — either way the loop lives and the job is not held by us.
    """
    LOGGER.warning(
        "job_settle_conflict worker_id=%s job_id=%s kind=%s step=%s detail=%s — the control "
        "plane no longer holds this lease for us; handing it back and moving on",
        settings.worker_id, job.id, job.kind, what, str(error)[:200],
    )
    try:
        client.heartbeat(settings.worker_id, job.id, job.lease_token,
                         SHUTDOWN_LEASE_TTL_SECONDS, handing_back=True)
        LOGGER.info("job_handed_back_after_conflict worker_id=%s job_id=%s",
                    settings.worker_id, job.id)
    except Exception:  # noqa: BLE001 — not ours any more: nothing left to release
        LOGGER.info("job_not_ours_after_conflict worker_id=%s job_id=%s",
                    settings.worker_id, job.id)


def run_worker_cycle(client: ControlPlaneClient, settings: WorkerSettings) -> bool:
    _tick_ai_sre_overwatch(settings)
    job = client.poll(
        settings.worker_id,
        capabilities(),
        settings.lease_ttl_seconds,
    )
    if not job:
        LOGGER.info("worker_idle worker_id=%s", settings.worker_id)
        # NO MONITORING SWEEP HERE. Every lease slot runs this function, so a sweep on this idle
        # branch ran once PER SLOT, concurrently: with `BOSS_WORKER_SLOTS=auto` (3 slots) the
        # laptop recorded three health samples and three metrics scrapes of HelpDesk AI within
        # 25 ms, each windowed against the same previous scrape, so every request was counted
        # three times. It also stopped entirely while every slot was busy. The one sweep runs on
        # the worker's background thread (main._run_background_sweeps), busy or idle.
        return False

    if job.attempt > 1:
        # SAY THAT THE WORK IS BEING REDONE. A re-delivery means a previous attempt was
        # interrupted — a deploy, a crash, a lost lease — and every minute it had spent is
        # gone. Recorded at WARNING because it is charged to the run's phase timing as though
        # it were work: without this line the console shows "Engineer implementing 27m" for a
        # stage that was killed at minute 20 and started over, and there is nothing anywhere
        # that says so.
        LOGGER.warning(
            "job_redelivered worker_id=%s job_id=%s kind=%s attempt=%s — a previous attempt "
            "was interrupted and its in-flight work was lost; this run restarts the stage",
            settings.worker_id, job.id, job.kind, job.attempt,
        )
        # AND PUT IT WHERE THE RUN IS READ, not only in a log nobody has open.
        #
        # The line above existed for this exact case and it still took a forensic session to
        # explain a 46-minute build: the console showed "Engineer implementing 30m1s" with no
        # model call and nothing anywhere said the stage had been restarted. Recorded on the run
        # so the phase timeline can say so itself.
        _record_redelivery(client, job)
    LOGGER.info(
        "job_leased worker_id=%s job_id=%s kind=%s attempt=%s",
        settings.worker_id,
        job.id,
        job.kind,
        job.attempt,
    )
    try:
        client.heartbeat(
            settings.worker_id,
            job.id,
            job.lease_token,
            settings.lease_ttl_seconds,
        )
    except Exception as error:
        if not _is_conflict(error):
            raise
        # Leased and immediately lost: never start work we do not hold.
        _hand_back_after_conflict(client, settings, job, "heartbeat", error)
        return True

    # Keep the lease alive for the WHOLE (possibly many-minute) job. execute_job is a
    # single blocking call — e.g. code.task runs the entire governed pipeline, whose
    # write_tests/implement phases can each run minutes (code_agent_timeout is 900s).
    # Without renewing the lease mid-run it expires (default 300s) and the reclaimer
    # restarts the STILL-running job → an infinite loop on the longest phase. A daemon
    # thread renews at a capped fraction of the TTL (its OWN client — the main one
    # is busy) through bounded settlement acknowledgment.
    hb_stop = threading.Event()
    control_plane_url = getattr(settings, "control_plane_url", None)
    owns_hb_client = bool(control_plane_url)
    hb_client = (
        ControlPlaneClient(control_plane_url, timeout=3.0, api_key=getattr(settings, "api_key", ""))
        if owns_hb_client
        else client
    )

    def _hand_back_on_shutdown() -> bool:
        """Shorten the lease so a deploy costs seconds of recovery, not a full TTL.

        A deploy recreates the worker: Docker sends SIGTERM and SIGKILLs ~10s later, killing
        the main thread mid-stage. Nothing handed the job back, so it sat "running" under a
        lease nobody was serving until the 300s TTL expired — measured on a real run, the gap
        between the deploy and the round restarting was exactly one TTL.

        We do NOT call `fail()`: that consumes a retry attempt and can deadletter or fail the
        code task, and being interrupted by a deploy is not the job's fault. Re-heartbeating
        with a tiny TTL expires the lease almost immediately, so the reclaimer takes the job on
        its next poll — the same recovery path as a crash, just 60x sooner.

        Runs on the heartbeat thread, which has its OWN client. The main thread is blocked deep
        inside execute_job and cannot be asked to do anything.
        """
        try:
            # handing_back: the reclaim must not cost the job an attempt (jobs.heartbeat).
            hb_client.heartbeat(settings.worker_id, job.id, job.lease_token,
                                SHUTDOWN_LEASE_TTL_SECONDS, handing_back=True)
            LOGGER.warning(
                "job_handed_back_for_shutdown worker_id=%s job_id=%s kind=%s — worker is "
                "stopping (deploy or restart); lease shortened to %ss so this job is "
                "reclaimed promptly instead of waiting out its %ss TTL",
                settings.worker_id, job.id, job.kind,
                SHUTDOWN_LEASE_TTL_SECONDS, settings.lease_ttl_seconds,
            )
            return True
        except Exception:  # noqa: BLE001 — best effort; the TTL is still the backstop
            LOGGER.warning(
                "job_hand_back_failed worker_id=%s job_id=%s — falling back to lease expiry",
                settings.worker_id, job.id,
            )
            return False

    completion_pending = threading.Event()
    settlement_deadline: float | None = None
    # Even an operator-disabled execution ceiling cannot renew a stuck acknowledgment
    # forever. The sliced wait notices this deadline within one second.
    settlement_limit = max(0.1, min(float(getattr(client, "lease_timeout_seconds", 30.0)), 30.0))

    def _renew_lease() -> None:
        # The heartbeat proves the WORKER is alive, never that the JOB is progressing. A
        # hung stage therefore kept its lease renewed indefinitely: nothing reclaimed it,
        # nothing failed it, and the stall only ended when the process happened to die.
        # Observed: a review check hung and the run showed a "164 min adversarial review"
        # that was pure lease latency, not work.
        #
        # So renewal is BOUNDED. Past the ceiling we stop renewing; the lease then expires
        # within one TTL and the reclaimer picks the job up (or fails it on max_attempts)
        # instead of the run hanging open forever.
        # ONE MISSED HEARTBEAT MUST NOT THROW AWAY THIRTY MINUTES OF WORK.
        #
        # This was ttl // 2, which leaves exactly ONE heartbeat between a job and its lease
        # expiring. Miss it once — a blip on the control plane, a slow query — and the lease
        # dies on the dot, the reclaimer re-delivers the job, and everything the stage had done
        # is replayed from the clone.
        #
        # Measured on crun_b09f23f3bd134953b961: implement ran 1800.902s and recorded no model
        # call at all, then a second provisioning + implement did the same work in 10m41s. TTL
        # was 1800 and the interval 900, so the whole build hung on a single heartbeat.
        #
        # A quarter of the TTL tolerates three consecutive failures before expiry, which is the
        # difference between a transient blip and a genuinely dead worker.
        # HEARTBEAT OFTEN, AND SAY SO.
        #
        # ttl // 4 was still a guess dressed as a fix: it assumes the beats land. Two runs in a
        # row lost their lease at exactly the TTL with NO failure logged — so either the beats
        # were not happening or they were not extending anything, and the logs could not tell
        # the two apart, because only failures were ever written.
        #
        # A minute is cheap — one small POST — and it turns a lease into something that
        # survives thirty consecutive misses instead of three. And every beat is now recorded,
        # so the next occurrence answers "was it heartbeating?" instead of raising it.
        interval = max(15, min(60, settings.lease_ttl_seconds // 4))
        # And a FAILED heartbeat retries soon rather than sleeping out another full interval —
        # waiting the same 450s after a failure gives back the margin this just bought.
        retry_interval = max(10, min(30, max(5, interval // 2)))
        misses = 0
        beats = 0
        max_runtime = max(0, int(getattr(settings, "job_max_runtime_seconds", 5400) or 0))
        started_at = time.monotonic()
        progressing = False
        ceiling_noted = False
        # SLICED, so a stop request is noticed within a second rather than up to half a lease
        # TTL later. Docker's grace period before SIGKILL is ~10s: a renewal interval of 150s
        # would mean the process is dead long before the loop next looks at anything.
        while True:
            if _wait_in_slices(hb_stop, retry_interval if misses else interval,
                               deadline=lambda: settlement_deadline):
                return  # this cycle settled or exited; nothing further to renew
            if SHUTDOWN.is_set():
                _hand_back_on_shutdown()
                return
            if settlement_deadline is not None and time.monotonic() >= settlement_deadline:
                LOGGER.error("lease_renewal_stopped_ack_deadline worker_id=%s job_id=%s — "
                             "acknowledgment exceeded its renewal window; lease expiry is the backstop",
                             settings.worker_id, job.id)
                return
            elapsed = time.monotonic() - started_at
            if max_runtime and elapsed > max_runtime and progressing:
                # THE CEILING IS FOR HUNG WORK, NOT LONG WORK. A build's single implement turn
                # may itself run BOSS_CODE_AGENT_TIMEOUT_SECONDS (5400s on the laptop), so a
                # whole build routinely outlives a fixed job ceiling. Letting that lease lapse
                # got the job reclaimed while it ran and its live agent SIGTERMed
                # (crun_1839849d). The control plane says whether the run is still producing
                # agent turns; while it is, the lease is renewed.
                if not ceiling_noted:
                    LOGGER.warning(
                        "lease_renewal_past_ceiling_progressing worker_id=%s job_id=%s kind=%s "
                        "elapsed_s=%d max_s=%d — past the runtime ceiling but the control plane "
                        "reports progress; renewing", settings.worker_id, job.id, job.kind,
                        int(elapsed), max_runtime)
                    ceiling_noted = True
            elif max_runtime and elapsed > max_runtime:
                LOGGER.error(
                    "lease_renewal_stopped_runtime_exceeded worker_id=%s job_id=%s "
                    "kind=%s elapsed_s=%d max_s=%d — job exceeded its runtime ceiling; "
                    "letting the lease expire so it is reclaimed instead of hanging",
                    settings.worker_id, job.id, job.kind, int(elapsed), max_runtime,
                )
                return
            try:
                beat = hb_client.heartbeat(
                    settings.worker_id, job.id, job.lease_token,
                    settings.lease_ttl_seconds,
                )
                progressing = job_progressing(beat)
                beats += 1
                misses = 0
                # Every beat for the first few, then one a minute's worth apart, so a long job
                # leaves a trail without filling the log with itself.
                if beats <= 3 or beats % 10 == 0:
                    LOGGER.info(
                        "lease_heartbeat worker_id=%s job_id=%s beat=%d ttl=%ds every=%ds",
                        settings.worker_id, job.id, beats, settings.lease_ttl_seconds, interval,
                    )
            except Exception as exc:  # noqa: BLE001
                # A 409 is NOT transient: the control plane is telling us this lease now belongs
                # to somebody else, because ours expired and another worker reclaimed the job.
                # Swallowing it left BOTH workers executing the same run — measured on
                # job_2fbe88e6 (attempt 3/3): worker-local kept running and heartbeating into
                # 409s while worker-local-3 owned the lease, both writing phases, so the run's
                # phase moved backwards and stages re-ran. Stop renewing and signal the job.
                if "409" in str(exc) or "Conflict" in str(exc):
                    if completion_pending.is_set():
                        # Completion may already have committed while its response is delayed.
                        # Stop renewal; the pending acknowledgment still determines its outcome.
                        LOGGER.info("lease_ended_during_completion worker_id=%s job_id=%s",
                                    settings.worker_id, job.id)
                    else:
                        LOGGER.error(
                            "lease_lost worker_id=%s job_id=%s — another worker owns this lease; "
                            "abandoning our execution to avoid duplicate concurrent work",
                            settings.worker_id, job.id,
                        )
                    lease_lost.set()
                    return
                misses += 1
                LOGGER.warning(
                    "heartbeat_renew_failed worker_id=%s job_id=%s miss=%d retry_in=%ds "
                    "— the lease expires in at most %ds if these keep failing",
                    settings.worker_id, job.id, misses, retry_interval,
                    settings.lease_ttl_seconds,
                )

    def _heartbeat_lifetime() -> None:
        try:
            _renew_lease()
        finally:
            # The renewing thread owns its client, including when a bounded request
            # takes longer than the main thread's cleanup join. Never close it in-flight.
            if owns_hb_client:
                try:
                    hb_client.close()
                except Exception:  # noqa: BLE001
                    LOGGER.warning("heartbeat_client_close_failed job_id=%s", job.id)

    lease_lost = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat_lifetime, name=f"lease-hb-{job.id}", daemon=True)
    hb_thread.start()

    # Keep renewing through the server acknowledgment, not only local execution.
    # The existing whole-job renewal ceiling also covers this phase.
    try:
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
            settlement_deadline = time.monotonic() + settlement_limit
            try:
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
            except Exception as fail_error:
                if not _is_conflict(fail_error):
                    raise
                _hand_back_after_conflict(client, settings, job, "fail", fail_error)
            return True

        if isinstance(result, dict) and result.get("job_deferred"):
            # The control plane re-queued this job itself (JobLeaseService.defer_leased): our
            # lease is gone, and completing it would fail or, worse, mark waiting work done.
            LOGGER.info("job_deferred_by_api worker_id=%s job_id=%s reason=%s",
                        settings.worker_id, job.id, result.get("error"))
            return True

        # Reconciliation owns its bounded retry/deadletter transition in the same
        # transaction that records provider-read Receipts and observations.
        if job.kind == "github.repository.reconcile":
            LOGGER.info(
                "job_finalized_by_reconciler worker_id=%s job_id=%s status=%s",
                settings.worker_id,
                job.id,
                (result.get("job") or {}).get("status"),
            )
            return True

        completion_pending.set()
        settlement_deadline = time.monotonic() + settlement_limit
        try:
            completion = client.complete(
                settings.worker_id,
                job.id,
                job.lease_token,
                result,
            )
        except Exception as error:
            if not _is_conflict(error):
                raise
            _hand_back_after_conflict(client, settings, job, "complete", error)
            return True
        LOGGER.info(
            "job_completed worker_id=%s job_id=%s status=%s artifact_id=%s decision_id=%s",
            settings.worker_id,
            job.id,
            completion["status"],
            completion.get("artifact_id"),
            completion.get("decision_id"),
        )
        return True
    finally:
        hb_stop.set()
        hb_thread.join(timeout=3)
        if hb_thread.is_alive():
            LOGGER.warning("heartbeat_cleanup_pending job_id=%s — bounded lease request still finishing", job.id)
