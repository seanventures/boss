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

from typing import Any, Optional

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
    "security.scan": P.SECURITY_SCAN_RUN,           # F4: scanners in the sandbox, own app only
    # repositories and GitHub
    "docs.update": P.GITHUB_MANAGE,
    "software_factory.propose_pr": P.CODE_TASK_CREATE,   # opens a PR from a build's boss/* branch
    "github.pull_request.merge": P.GITHUB_MERGE,
    "change_set.merge": P.GITHUB_MERGE,             # a multi-repo change, merged as a set
    "github.deployment.create": P.DEPLOYMENT_CREATE,
    "github.triage": P.GITHUB_MANAGE,
    "github.pr_review": P.GITHUB_MANAGE,
    "pull_request.review_again": P.CODE_TASK_STEER,   # a review round posted on the build's PR
    "github.repository.reconcile": P.REPOSITORY_MANAGE,
    # deployments and infrastructure
    "deployment.stack_apply": P.DEPLOYMENT_CREATE,
    "self_fix.build": P.IMPROVEMENT_MANAGE,         # BOSS fixes itself: Mender's build (self_fix_build)
    "deployment.host_apply": P.HOST_MANAGE,
    "infra.operation": P.INFRA_PROVISION,           # platform/operations.py (destroy is approved first)
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


# ---- THE check, for every path that queues a job of a caller-chosen kind (C-2) -------------------

class JobKindRefused(Exception):
    """Queuing (or RE-queuing) this kind of job is refused for the caller. `status`/`body` are the
    HTTP answer (the API maps it): 422 unknown_job_kind, 401 unauthenticated / step_up_required,
    403 forbidden."""

    def __init__(self, status: int, body: dict) -> None:
        super().__init__(body.get("error", "job_kind_refused"))
        self.status = status
        self.body = body


def _enforced() -> bool:
    from . import lineage

    enforced = lineage.request_enforced()
    if enforced is not None:
        return enforced
    # No request decided this call (a background path): fail CLOSED to the configured mode.
    from ..config import get_settings
    from .enforcement import AuthzMode, mode_for

    return mode_for("main", global_mode=get_settings().authz_mode or "enforce") == AuthzMode.ENFORCE


def require_job_kind(kind: str, project_id: str, *, db: Any = None) -> None:
    """THE job-kind authorization: the kind's own catalogue permission at the job's project,
    through the PDP — step-up included — as the verified caller. Every path that queues a job
    whose kind the CALLER chose (POST /v1/jobs) or re-creates one from a stored kind (a
    dead-letter requeue, retry or remediation) calls this, so `deadletter:manage` or
    `work_item:manage` alone never buys a critical effect. A kind BOSS does not know is refused.
    Shadow/off: allowed (the guard records shadow would-denies).
    `test_every_path_that_queues_a_caller_chosen_kind_authorizes_it` fails on a new path that
    queues a caller-chosen kind without it. A job sink is checked when it is set up and again at
    dispatch (event_subscription_service.job_sink_held), so one created before this check
    existed is held until a person who holds the kind's permission re-saves it."""
    if not _enforced():
        return
    permission = permission_for(kind)
    if permission is None:
        raise JobKindRefused(422, {"error": "unknown_job_kind",
                                   "message": "BOSS has no worker for this kind of job, so it cannot be queued."})
    from . import authz, lineage
    from .scope import ResolvedScope

    ctx = lineage.current_context()
    if ctx is None:
        raise JobKindRefused(401, {"error": "unauthenticated"})
    decision = authz.check(ctx, permission, ResolvedScope.in_project(ctx.principal.org_id, project_id), db=db)
    if not decision.allowed:
        raise JobKindRefused(decision.status, decision.error_body())
