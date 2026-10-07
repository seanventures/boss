"""THE job kind -> catalogue permission map (IDENTITY.md §6.3, §7.14).

POST /v1/jobs is declared `work_item:manage` (delegable, held by an Operator), but what a job DOES
is decided by its kind: `infra.droplet.delete` terminates a server, `github.pull_request.merge`
merges a PR. Enqueueing a job is therefore also checked against the kind's own permission, at the
job's project, by the same PDP — so `work_item:manage` alone never buys a critical effect, and an
agent run (whose token holds only delegable permissions) cannot enqueue a non-delegable one.

Every kind a BOSS worker leases (apps/worker/boss_worker/runner.py `capabilities()`) is named
here explicitly; `test_identity_job_kinds_are_permissioned` fails on a kind with no entry. A kind
this map does not know is refused under enforcement (`unknown_job_kind`): an unmapped effect is
never allowed by default.
"""
from __future__ import annotations

from typing import Optional

from .permissions_gen import P, PERMISSION_META

#: kind -> the permission enqueueing it needs, beyond the route's own `work_item:manage`.
JOB_KIND_PERMISSIONS: dict[str, str] = {
    # agent work-product briefs: a role writes a document; no outside effect
    "agent.cao.architecture_review": P.WORK_ITEM_MANAGE,
    "agent.coo.ops_digest": P.WORK_ITEM_MANAGE,
    "agent.cto.implementation_plan": P.WORK_ITEM_MANAGE,
    "agent.deployment_manager.deployment_review": P.WORK_ITEM_MANAGE,
    "agent.finops_sentinel.cost_brief": P.WORK_ITEM_MANAGE,
    "agent.infra_steward.infra_review": P.WORK_ITEM_MANAGE,
    "agent.metatron.status_brief": P.WORK_ITEM_MANAGE,
    "agent.model_spend_analyst.model_spend_brief": P.WORK_ITEM_MANAGE,
    "agent.pm.scope_brief": P.WORK_ITEM_MANAGE,
    "agent.program_manager.delivery_plan": P.WORK_ITEM_MANAGE,
    "agent.providerops_manager.providerops_brief": P.WORK_ITEM_MANAGE,
    "agent.qa.validation_report": P.WORK_ITEM_MANAGE,
    "agent.release_manager.release_packet": P.WORK_ITEM_MANAGE,
    "agent.resource_janitor.cleanup_brief": P.WORK_ITEM_MANAGE,
    "agent.secrets_steward.secret_boundary_review": P.WORK_ITEM_MANAGE,
    "agent.vp_agents.role_review": P.WORK_ITEM_MANAGE,
    "plan.command": P.WORK_ITEM_MANAGE,
    "plan.generate": P.WORK_ITEM_MANAGE,
    "openai.plan": P.WORK_ITEM_MANAGE,
    # agents and plans that act
    "agent.studio.execute": P.AGENT_TEST,
    "agent.orchestrator.next_move": P.AUTOPILOT_OPERATE,
    "agent.resume": P.CODE_TASK_STEER,
    "skill.execute": P.SKILL_INVOKE,
    "meeting.step": P.MEETING_CONVENE,
    "discovery.run": P.DISCOVERY_CREATE,
    "code.task": P.CODE_TASK_CREATE,
    "investigation.run": P.HOST_EXEC,
    # repositories and GitHub
    "docs.update": P.GITHUB_MANAGE,
    "software_factory.propose_pr": P.CODE_TASK_CREATE,   # opens a PR from a build's boss/* branch
    "github.pull_request.merge": P.GITHUB_MERGE,
    "github.deployment.create": P.DEPLOYMENT_CREATE,
    "github.triage": P.GITHUB_MANAGE,
    "github.pr_review": P.GITHUB_MANAGE,
    "github.repository.reconcile": P.REPOSITORY_MANAGE,
    # deployments and infrastructure
    "deployment.stack_apply": P.DEPLOYMENT_CREATE,
    "deployment.host_apply": P.HOST_MANAGE,
    "infra.droplet.create": P.INFRA_PROVISION,
    "infra.droplet.delete": P.INFRA_TERMINATE,
    # outside systems
    "comms.outbox.deliver": P.COMMS_SEND,
    "webhook.outbound.deliver": P.INTEGRATION_MANAGE,
    "a2a.dispatch": P.INTEGRATION_MANAGE,
    "work_tracker.issue.sync": P.INTEGRATION_MANAGE,
    "enterprise.marketplace.validate": P.PACK_INSTALL,
    "providerops.sync.readonly": P.MODEL_PROVIDER_MANAGE,
}


def permission_for(kind: str) -> Optional[str]:
    """The permission enqueueing a job of `kind` needs, or None for a kind BOSS does not know."""
    return JOB_KIND_PERMISSIONS.get(str(kind or ""))


def _check() -> None:
    unknown = sorted(p for p in JOB_KIND_PERMISSIONS.values() if p not in PERMISSION_META)
    if unknown:     # pragma: no cover — a catalogue rename would land here at import
        raise RuntimeError(f"job kinds name unknown permissions: {unknown}")


_check()
