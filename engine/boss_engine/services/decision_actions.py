"""What a person can do with a decision, decided once, here.

THE SAME FOUR BUTTONS ON EVERY CARD WAS THE BUG.

The Approvals screen gave every pending card Approve, Reject, Escalate and Request changes,
whatever the card was. On boss-1 the queue held three pending decisions: a PR review, a
`budget_denied` and a `model_capacity_denied`. For the last two, Approve recorded a decision
nothing reads ("this type has no automated follow-on") and Reject did the same in red — the
step they came from had already failed and would not resume either way. The real fix
(raise the budget, add model credit) was mentioned nowhere. Escalate posted an action the
server did not accept, so it always failed. And "Request changes" on a workflow gate parked
the decision in `revision_requested`, which the workflow runner never reads, so the run
waited for ever.

The engine already decides once which actions a RUN offers; this is the same contract for
decisions. The console renders `actions` and nothing else, and `DecisionService.transition`
refuses an action that is not in the list — so a button that cannot work is never drawn, and
a caller that bypasses the console (MCP, the chat agent, curl) gets the same answer, with
what to do instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

# WHAT EVERY DECISION BOSS RAISES IS, DECIDED HERE AND CHECKED AGAINST THE CODE THAT RAISES IT.
#
# The first version of this contract recognised three refusal reasons by name and called
# everything else `general`. Review found that the refusals BOSS actually raises most were
# not among them: a Factory build that hit a rate limit writes
# `restore_model_capacity_or_raise_budget` / `model_capacity_exhausted`, the budget governor's
# tier-2 freeze writes `raise_cap_or_wait_for_headroom` / `budget_tier2_frozen`, and the
# autoscaler writes `increase_budget_or_defer_work`. All of them fell through to `general`,
# were ranked first in the Command Center as gates, and offered Approve, "Carries out what
# this card proposes", on a card nothing acts on. A list of names is only as good as the last
# person who remembered to extend it, so
# engine/tests/test_every_decision_boss_raises_has_its_buttons_decided.py walks every place in
# engine/ and apps/ that creates a decision and fails when the action it writes is not
# classified below.

#: Refusals: BOSS declined to spend (budget) or could not (model capacity). They are notices,
#: not questions. The work has already stopped or been deferred, and approving or rejecting
#: the card changes nothing about it. Keyed by the recommendation action OR the reason code a
#: producer writes; the value names the guidance family in `_REFUSAL_GUIDANCE`.
REFUSALS: dict[str, str] = {
    # typed_decision_spend: the reason doubles as the action.
    "budget_denied": "budget",
    "budget_deferred": "budget",
    "model_capacity_denied": "capacity",
    # budget.BudgetService: a reservation over the cap.
    "increase_budget_or_defer": "budget",
    "increase_allocation_or_defer": "budget",
    # budget.BudgetService: past freeze_tier2_pct, high-risk work is refused.
    "raise_cap_or_wait_for_headroom": "tier2_frozen",
    "budget_tier2_frozen": "tier2_frozen",
    # governed_code_task_service and deadletters: a build or a job ran out of model capacity
    # or credit. On a real install this is the refusal BOSS raises most often.
    "restore_model_capacity_or_raise_budget": "capacity",
    "model_capacity_exhausted": "capacity",
    # resource_plane autoscaler: the budget state stopped BOSS adding workers.
    "increase_budget_or_defer_work": "autoscale",
    "approve_budget_increase_or_accept_degraded_mode": "autoscale",
    # jobs: the budget state deferred a job; it is re-queued and runs later by itself.
    "review_budget_policy": "deferred",
    "budget_state_deferred_job": "deferred",
}
#: Kept under their old names; both now cover every refusal, by action or by reason.
REFUSAL_REASONS = frozenset(REFUSALS)
REFUSAL_ACTIONS = REFUSAL_REASONS

#: Approving CARRIES THESE OUT: DecisionService._handle_approved_follow_on acts on each one.
#: The enforcing test reads that function and fails when this set and it disagree.
CARRIED_OUT_ON_APPROVAL = frozenset({
    "execute_agent_tool",
    "resume_agent_run",
    "resume_code_task",
    "review_plan_draft",
    "github.community.dispatch_plan",
    "github.community.merge_authorization",
    "review_pull_request",
    "review_deployment",
    "review_infra_provisioning",
    "self_improvement.begin_canary",
    "self_improvement.rollout",
    "role.autonomy_change",
    # Approving a planned stack deployment approves its ChangeSet and queues the apply.
    "deploy_stack",
    # Approving a planned host deployment approves its ChangeSet and queues the apply.
    "deploy_host",
    # Approving a held destroy/restore of a provisioned resource queues its leased
    # `infra.operation` job (platform/operations.on_decision_approved).
    "infra_operation",
    # Approving an architecture records it on its review; the waiting step then succeeds and
    # every later build receives the approved document (architecture_docs.on_decision_answered).
    "review_architecture",
    # Approving a multi-repo change set pins its head SHAs and merges the whole set
    # (multirepo/change_sets.on_decision); a later push re-asks.
    "approve_change_set",
    # Approving a reconciliation settles the call's receipt (replaying a reply it holds, or
    # failing it) and puts the stopped step back in the queue
    # (typed_decision_spend.on_reconciliation_approved).
    "architecture_reconciliation",
    "typed_decision_reconciliation",
    # Approving a BOSS self-fix issues its signed, single-use install ticket for the host
    # self-installer (services/self_fix_install.on_approved).
    "self_fix.install",
    # Approving an agent-drafted skill writes that draft enabled. Until then it is not usable.
    "activate_authored_skill",
    # Approving the removal of installed self-fixes issues the signed `remove` / `remove_all`
    # ticket (services/self_fix_install.on_remove_approved).
    "self_fix.remove",
})

#: "Request changes" DOES SOMETHING ONLY FOR THESE: DecisionService._handle_non_approved_follow_on
#: sends the proposal back with the note. For every other decision `revision_requested` was a
#: status nothing reads and one that could no longer be approved — a pull request sent back
#: that way could never be merged from BOSS again. The enforcing test reads that function too.
REVISE_CONSUMED = frozenset({
    "self_improvement.begin_canary",
    "self_improvement.rollout",
    "role.autonomy_change",
    # The architect writes the next version from the note (architecture_docs.revise_sent_back).
    "review_architecture",
    # "Send back": every PR of the set gets a fix build from the note (change_sets._send_back).
    "approve_change_set",
    # Mender fixes it again from the note, then asks again (self_fix_install.on_revise).
    "self_fix.install",
})

#: A workflow decision step (workflows.py) writes this action and no run id. The runner reads
#: the decision's status exactly as it reads a capability gate's, so it is a workflow gate;
#: as `general` it offered "Request changes", which left the run waiting for ever.
#: `review_proposed_rules` is the Scan for business rules workflow's review gate
#: (knowledge/rule_scan.py): the rules themselves are answered one by one in Graph › Rules
#: review; the gate resolves itself when that scan's queue is empty, or a person closes it here.
#: `review_proposed_trace` is the regulatory_trace workflow's review gate (knowledge/
#: regulatory_trace.py): implemented_by and gap findings are answered one by one in Graph ›
#: regulatory trace review; the gate resolves itself once that trace's queue is empty.
WORKFLOW_GATE_ACTIONS = frozenset({
    "resume_workflow", "review_proposed_rules", "review_proposed_trace",
})

#: Approving these RECORDS a go-ahead and starts nothing. Some are read later by the work that
#: asked (an SRE runbook, an assurance reopen, a GitHub effect fenced to an approved decision);
#: most ask a person to do something first. Either way the Approve button must not claim that
#: it carries anything out, so it says what it does.
RECORDED_ON_APPROVAL = frozenset({
    "approve_changeset",
    "approve_initiative_budget",
    "attach_required_evidence",
    "configure_or_enable_infra_provisioning",
    "configure_supported_channel",
    "configure_target_github_and_retry_phase",
    "coordinate_with_surface_owner",
    "create_budget_reservation",
    # Community's discovery spend (discovery_spend.py) asks a person to reconcile a stage whose
    # paid call may already have been charged — the same shape as typed_decision_reconciliation.
    # Unknown to the console-a stream, which was written on the private tree (integration).
    "discovery_reconciliation",
    "fix_phase_spec_and_retry",
    "github.community.triage_review",
    "investigate_regression",
    "plan_governed_changeset",
    "provide_github_token",
    "provide_openai_api_key",
    "reclassify_or_configure_approved_boundary_adapter",
    "reopen_and_investigate",
    "reserve_budget_before_deployment",
    "reserve_provider_budget",
    "resolve_assurance_case",
    "restore_metric_evaluator",
    "restore_verifier_preconditions",
    "review_commitment",
    "review_cost_anomaly",
    "review_deployment_health",
    "review_entitlement_sources",
    "review_model_spend_and_host_effects_before_retry",
    "review_model_spend_and_receipts_before_retry",
    "review_plane_sync_failure",
    "review_plane_sync_uncertain",
    "review_provider_spend_kill_switch",
    "review_rollback_or_retry",
    "review_stalled_meeting",
    "set_BOSS_INFRA_REAL_PROVISION_ALLOWED",
    "typed_decision_lease_expired",
    "update_deployment_environment_policy",
    "upsert_comms_channel_policy",
    "use_settled_conclusion_or_reopen_with_new_evidence",
    # A paid call whose outcome is unknown (budget, model_gateway, changeset_apply, an
    # interrupted conversation turn): the person reconciles the recorded attempt; BOSS resolves
    # the card itself once the reservation is settled or released (budget._resolve_reconciliation).
    "conversation_reconciliation",
    "reconcile_legacy_model_exposure",
    "reconcile_legacy_provider_exposure",
    "reconcile_provider_changeset",
    "verify_provider_outcome_before_retry",
    "verify_provider_outcome_then_settle_or_release",
    # A next action an agent-room summary named as a decision (agent_converse): approving records
    # the person's answer on the conversation; nothing is started.
    "decide",
})
#: Producers that build the action from a provider key (comms: configure_or_enable_slack_…).
RECORDED_ON_APPROVAL_PREFIXES = ("configure_or_enable_",)

#: Every action BOSS is known to write. The enforcing test fails on a producer outside it.
KNOWN_ACTIONS = (REFUSAL_REASONS | CARRIED_OUT_ON_APPROVAL | WORKFLOW_GATE_ACTIONS
                 | RECORDED_ON_APPROVAL)

KINDS = ("refusal", "deploy_gate", "workflow_gate", "architecture_review", "pull_request",
         "change_set", "general")

#: A decision still awaiting an answer (not yet approved, rejected, executed, expired or
#: withdrawn). THE shared definition of "open": a guard elsewhere that needs to know whether a
#: decision can still be acted on (budget.py's legacy-exposure reconciliation,
#: self_fix_install.py's remove-request dedup) imports this rather than keeping its own
#: ("pending", "delegated") literal — see
#: engine/tests/test_every_surface_agrees_on_what_needs_the_operator.py.
OPEN_STATUSES = frozenset({"pending", "delegated"})
_OPEN = OPEN_STATUSES  # noqa: F841 — short local alias used throughout this module
#: An expired hold-default decision can still be answered: see DecisionService.transition.
_REOPENABLE = frozenset({"expired"})


def _rec(decision: Mapping[str, Any]) -> dict[str, Any]:
    rec = decision.get("recommendation")
    return rec if isinstance(rec, dict) else {}


def _action_of(decision: Mapping[str, Any]) -> str:
    return str(_rec(decision).get("action") or "")


def _reason(decision: Mapping[str, Any]) -> str:
    rec = _rec(decision)
    for key in ("reason_code", "reason"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(decision.get("reason") or "").strip()


def _refusal_family(decision: Mapping[str, Any]) -> Optional[str]:
    """Which refusal this is, by action first and then by ANY reason the producer wrote.

    Every reason field is consulted, not only the first: the governed build path writes the
    provider's own code (`model_rate_limit`) as `reason_code` and BOSS's (`model_capacity_
    exhausted`) as `reason`, and reading only the first missed it.
    """
    action = _action_of(decision)
    if action in REFUSALS:
        return REFUSALS[action]
    rec = _rec(decision)
    for value in (rec.get("reason_code"), rec.get("reason"), decision.get("reason")):
        if isinstance(value, str) and value.strip() in REFUSALS:
            return REFUSALS[value.strip()]
    return None


def kind_of(decision: Mapping[str, Any]) -> str:
    """Which of KINDS this decision is. Total: anything unrecognised is `general`."""
    rec = _rec(decision)
    action = _action_of(decision)
    if _refusal_family(decision):
        return "refusal"
    if action == "review_architecture":
        # A workflow's own sign-off, but one that can be sent back: checked before the generic
        # workflow gate, which offers no "Request changes" because nothing reads it there.
        return "architecture_review"
    if rec.get("workflow_run_id") or action in WORKFLOW_GATE_ACTIONS:
        if str(rec.get("capability") or "") in {"deploy", "redeploy"}:
            return "deploy_gate"
        return "workflow_gate"
    if action == "review_pull_request":
        return "pull_request"
    if action == "approve_change_set":
        return "change_set"
    return "general"


def carries_out_on_approval(decision: Mapping[str, Any]) -> bool:
    """True when approving makes BOSS do something itself, not only record the answer."""
    return _action_of(decision) in CARRIED_OUT_ON_APPROVAL


def consumes_revision(decision: Mapping[str, Any]) -> bool:
    """True when something reads `revision_requested` for this decision and acts on the note."""
    return _action_of(decision) in REVISE_CONSUMED


def _action(action: str, label: str, tone: str, effect: str,
            needs: Optional[str] = None) -> dict[str, Any]:
    return {"action": action, "label": label, "tone": tone, "effect": effect, "needs": needs}


# An incident fix raises two cards. This sentence is the order on both: the release is what
# ships, and the pull request is merged only after recovery is verified.
_RELEASE_THEN_MERGE = (
    "Approve the release first — that is what ships. "
    "Merge the pull request only after recovery is verified."
)


def _release_then_merge(decision: Mapping[str, Any]) -> bool:
    return _rec(decision).get("incident_order") == "release_then_merge"


def _with_order(effect: str) -> str:
    if _RELEASE_THEN_MERGE in effect:
        return effect
    return f"{effect} {_RELEASE_THEN_MERGE}"


def _delegate() -> dict[str, Any]:
    # "Escalate" in the old inbox, which posted an action the server never accepted. Handing a
    # decision to somebody else IS delegation, so it is called what it does and posts that.
    return _action(
        "delegate", "Hand to someone else", "neutral",
        "Moves this decision to the person or role you name; it stays open until they answer.",
        needs="delegate_to",
    )


_RETRY = ("Then retry the work: Retry on the failed step in Workflow, or on the build in "
          "Factory. Approving this card would not retry anything.")

_REFUSAL_GUIDANCE: dict[str, dict[str, str]] = {
    "capacity": {
        "what_happened": (
            "BOSS tried to call a model and it had no capacity or credit left (a rate limit, "
            "used-up credit, or the model budget spent), so the work that asked stopped "
            "instead of retrying for ever."),
        "what_to_do": (
            "Add credit or raise the limit on the model account, or connect another model, in "
            "Repo and models. If the budget was the limit, raise it in Govern. " + _RETRY),
        "area": "setup",
        "link_label": "Fix in Repo and models",
    },
    "budget": {
        "what_happened": (
            "BOSS refused to spend because the work would have gone over the budget, so the "
            "step that asked failed."),
        "what_to_do": (
            "Raise the cap or wait for the budget window to reset in Govern. " + _RETRY),
        "area": "budget",
        "link_label": "Open Govern",
    },
    "tier2_frozen": {
        "what_happened": (
            "Spending has passed the point where the budget policy freezes high-risk (tier 2 "
            "and above) work, so BOSS refused to start it. Lower-risk work still runs."),
        "what_to_do": (
            "Raise the cap or the freeze threshold in Govern, or wait for the budget window "
            "to reset. " + _RETRY),
        "area": "budget",
        "link_label": "Open Govern",
    },
    "autoscale": {
        "what_happened": (
            "The budget is close to or over its cap, so BOSS did not add workers. Queued work "
            "still runs on the workers it already has, only more slowly."),
        "what_to_do": (
            "Nothing is needed if slower is fine. To let BOSS add workers, raise the cap in "
            "Govern. Approving this card would not add any."),
        "area": "budget",
        "link_label": "Open Govern",
    },
    "deferred": {
        "what_happened": (
            "The budget state defers this kind of work, so BOSS moved the job to run later. "
            "It is still queued and runs again by itself."),
        "what_to_do": (
            "Nothing is needed if it can wait. To run it sooner, raise the cap in Govern. "
            "Approving this card would not run it."),
        "area": "budget",
        "link_label": "Open Govern",
    },
}


def guidance(decision: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """What happened and what to do, for a decision that is a notice rather than a question."""
    family = _refusal_family(decision)
    if family is None:
        return None
    return dict(_REFUSAL_GUIDANCE[family])


def _general_approve(decision: Mapping[str, Any]) -> dict[str, Any]:
    if carries_out_on_approval(decision):
        return _action("approve", "Approve", "approve", "Carries out what this card proposes.")
    # The old text promised to carry the card out for EVERY general decision, and for most of
    # them nothing does: a "provide_github_token" card approved stayed exactly as it was.
    return _action(
        "approve", "Approve", "approve",
        "Records your approval. BOSS starts nothing from this card by itself; work that needs "
        "this approval can use it when it runs again.")


def allowed_actions(decision: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every action a person may take on this decision right now, in display order.

    Empty means there is nothing to press: the decision is closed.
    """
    status = str(decision.get("status") or "")
    if status == "approved":
        # The approval happened; the only thing left is recording that it was carried out by
        # hand, which matters ONLY for the types BOSS cannot carry out itself. A deploy, a gate
        # or a merge BOSS ran itself showed "Mark executed" forever after it was done.
        if carries_out_on_approval(decision) or kind_of(decision) in (
                "workflow_gate", "deploy_gate", "architecture_review"):
            return []
        return [_action(
            "execute", "Mark executed", "approve",
            "Records what was carried out and where, for the audit trail.", needs="note")]
    if status not in _OPEN and not reopenable(decision):
        return []
    kind = kind_of(decision)
    from .release_scope import needs_reason as _release_needs_reason

    if kind != "refusal" and _release_needs_reason(decision.get("recommendation")):
        # A RELEASE CARRYING REJECTED OR UNREVIEWED COMMITS (release_scope): shipping it is a
        # person's stated decision. The approval needs a reason and is sent forced, as a merge
        # with checks failing is; decisions.transition refuses anything else.
        ship = ("Ships every commit the card lists, including those from rejected or "
                "unreviewed runs. Say why: your reason is recorded with the release.")
        if _release_then_merge(decision):
            ship = _with_order(ship)
        return [
            _action("approve", "Ship with unreviewed commits", "reject", ship, needs="note"),
            _action("reject", "Don't ship", "reject",
                    "Nothing ships. Rebuild the fix from the serving commit to release it "
                    "on its own."),
            _delegate(),
        ]
    skipped = _rec(decision).get("spec_gate_skipped")
    if kind != "refusal" and isinstance(skipped, dict):
        # A SPEC ITS WORKFLOW DID NOT WAIT FOR (spec_gate.mark_gate_skipped): the build is
        # already running, so approving cannot mean "build it" — and must not start another.
        build = str(skipped.get("build_run_id") or "the run's build")
        return [
            _action("approve", "Acknowledge", "approve",
                    f"Records that you accept this spec. Starts nothing: {build} is already "
                    "building it."),
            _action("reject", "Reject", "reject",
                    f"Records that you do not accept this spec. {build} is not stopped by this "
                    "card; cancel it in Factory if it should not go on."),
        ]
    if kind == "refusal":
        return [_action(
            "reject", "Dismiss", "neutral",
            "Clears this notice from the queue. It does not retry anything; fix the cause and "
            "retry the work.")]
    if kind == "deploy_gate":
        return [
            _action("approve", "Ship it", "approve",
                    "Builds a container from this app and runs it on the bound host."),
            _action("reject", "Don't ship", "reject",
                    "Fails the Deploy step. Nothing is sent to any host."),
            _delegate(),
        ]
    if kind == "workflow_gate" and _action_of(decision) == "review_proposed_rules":
        # THE RULES ARE ANSWERED ONE BY ONE in Graph › Rules review, never here: approving this
        # card approves no rule. It only ends the review (BOSS ends it by itself once the
        # scan's queue is empty).
        return [
            _action("approve", "Finish the review", "approve",
                    "Ends the review and the workflow continues. Approves no rule: rules still "
                    "proposed stay in Rules review."),
            _action("reject", "Stop the run", "reject",
                    "Fails the review step and the run stops there. Proposed rules stay in "
                    "Rules review."),
            _delegate(),
        ]
    if kind == "workflow_gate" and _action_of(decision) == "review_proposed_trace":
        # THE FINDINGS ARE ANSWERED ONE BY ONE in the regulatory trace review, never here:
        # approving this card approves no implemented_by or gap edge.
        return [
            _action("approve", "Finish the review", "approve",
                    "Ends the review and the workflow continues. Approves no finding: "
                    "proposed links stay in regulatory trace review."),
            _action("reject", "Stop the run", "reject",
                    "Fails the review step and the run stops there. Proposed links stay in "
                    "regulatory trace review."),
            _delegate(),
        ]
    if kind == "workflow_gate" and str(_rec(decision).get("reason_prompt") or "").strip():
        # A GATE WHOSE ANSWER IS A PERSON'S STATED DETERMINATION (the AML analyst decision, the
        # SAR filing): the reason is the record a later case is judged against, so the approval
        # is drawn open with its reason box and sent with it (forced + note, which
        # decisions.transition requires a note for). The step's own prompt says what to write.
        return [
            _action("approve", str(_rec(decision).get("reason_label") or "Decide"), "approve",
                    "Releases the step with your note as the determination and its reason; it "
                    "is recorded for later cases.", needs="note"),
            _action("reject", "Stop the run", "reject",
                    "Fails the step. Nothing is decided and the case stays open."),
            _delegate(),
        ]
    if (kind == "workflow_gate" and _release_then_merge(decision)
            and str(_rec(decision).get("capability") or "") == "release_approval"):
        # AN INCIDENT'S RELEASE, beside the fix's pull request. This card is what ships.
        # The pull request is merged only after recovery; its own card says so.
        return [
            _action("approve", "Approve the release", "approve",
                    _with_order("Releases the step and the workflow continues from it.")),
            _action("reject", "Don't ship", "reject",
                    "Nothing ships. The pull request stays unmerged."),
            _delegate(),
        ]
    if kind == "workflow_gate":
        # No "Request changes": the workflow runner reads approved, rejected and expired, and
        # a gate parked in revision_requested left its run waiting for ever.
        return [
            _action("approve", "Approve", "approve",
                    "Releases the step and the workflow continues from it."),
            _action("reject", "Reject", "reject",
                    "Fails the step. The run stops there unless you retry it."),
            _delegate(),
        ]
    if kind == "architecture_review":
        # Only a PERSON may approve (architecture_docs.require_a_person). Request changes does
        # something here: the architect writes the next version from the note.
        return [
            _action("approve", "Approve", "approve",
                    "Approves this version. The step completes and every later build step "
                    "receives this architecture in its brief."),
            _action("revise", "Request changes", "neutral",
                    "Sends it back to the architect with your note; a new version comes back "
                    "here for approval.", needs="note"),
            _action("reject", "Reject", "reject",
                    "Rejects the architecture. The step fails and the run stops there."),
            _delegate(),
        ]
    if _action_of(decision) in ("deploy_host", "deploy_stack"):
        # A DEPLOY SAYS WHAT IT DOES: the approval ships to a host, under its ChangeSet.
        return [
            _action("approve", "Approve deploy", "approve",
                    "BOSS deploys it to the host, checks its health, and rolls back if the "
                    "check fails."),
            _action("reject", "Reject", "reject",
                    "Nothing is deployed and the host is not contacted."),
            _delegate(),
        ]
    if kind == "change_set":
        # ONE DECISION FOR EVERY PR OF A MULTI-REPO CHANGE: no per-PR approval exists, so there
        # is no Delegate either — the set is approved as one by someone who may approve it.
        n = len(_rec(decision).get("pull_requests") or [])
        return [
            _action("approve", f"Approve and ship all {n}" if n else "Approve and ship", "approve",
                    "Merges every pull request of the set at the reviewed commits, in dependency order, "
                    "then deploys them together. A failed merge reverts the ones that landed."),
            _action("revise", "Send back", "neutral",
                    "Every pull request gets a fix build from your note; the set is reviewed and asked "
                    "again.", needs="note"),
            _action("reject", "Reject", "reject",
                    "Nothing is merged or deployed. The pull requests stay open."),
        ]
    if kind == "pull_request":
        from .checks_failing import of_decision

        if of_decision(decision) is not None:
            # CHECKS FAILING (checks_failing.py): the merge is a person's stated decision. The
            # approval needs a reason and is sent as a forced merge (ci_gate.refuse_approval
            # refuses anything else); keeping it blocked is the other answer.
            failing = ("Merges this pull request although BOSS's checks are failing. Say why: "
                       "your reason is recorded with the merge.")
            if _release_then_merge(decision):
                failing = _with_order(failing)
            return [
                _action("approve", "Merge with checks failing", "reject", failing, needs="note"),
                _action("reject", "Keep blocked", "reject",
                        "The pull request stays open and unmerged while its follow-up build "
                        "fixes the failing checks."),
                _delegate(),
            ]
        # No "Request changes" either. Nothing reads a PR review's revision_requested, and the
        # status could no longer be approved, so a PR sent back that way could never be merged
        # from BOSS. Changing the code is a new build, which Factory starts from a note.
        if _release_then_merge(decision):
            # THE FIX'S PULL REQUEST, beside the release card. Merging is not what ships.
            return [
                _action("approve", "Merge after recovery", "approve", _RELEASE_THEN_MERGE),
                _action("reject", "Keep blocked", "reject",
                        "The pull request stays open and unmerged. To have BOSS change it, open "
                        "the app in Factory and use Add a feature or Rebuild with a fresh spec, "
                        "saying what to change."),
                _delegate(),
            ]
        return [
            _action("approve", "Approve and merge", "approve",
                    "Authorizes the merge; BOSS merges once its own checks pass."),
            _action("reject", "Keep blocked", "reject",
                    "The pull request stays open and unmerged. To have BOSS change it, open "
                    "the app in Factory and use Add a feature or Rebuild with a fresh spec, "
                    "saying what to change."),
            _delegate(),
        ]
    actions = [
        _general_approve(decision),
        _action("reject", "Reject", "reject", "Declines it; nothing is carried out."),
    ]
    if consumes_revision(decision):
        actions.append(_action(
            "revise", "Request changes", "neutral",
            "Sends the proposal back with your note; nothing is applied until it returns.",
            needs="note"))
    actions.append(_delegate())
    return actions


def reopenable(decision: Mapping[str, Any]) -> bool:
    """A decision in a status that looks closed but can still be answered.

    An expired hold-default decision applied nothing (see DecisionService.transition). A
    decision parked in `revision_requested` whose type does not read that status is the same
    dead end: before this contract, "Request changes" was offered on pull requests and every
    general card, and those decisions sit in that status on real installs with no way to
    approve them. Answering reopens them.
    """
    status = str(decision.get("status") or "")
    if status in _REOPENABLE:
        return str(decision.get("default_action") or "hold").lower() == "hold"
    if status == "revision_requested":
        return not consumes_revision(decision)
    return False


#: Where a decision belongs in front of the operator. ONE answer, read by every surface that
#: counts or filters decisions: the Approvals inbox, the Command Center and the v2 queue.
#:
#: WHY ONE. Review round 2 found three definitions of "needs the operator". The Command Center
#: counted pending and delegated; the inbox counted any card the engine offered an answer on,
#: which swept in every expired hold-default decision (127 of 165 on the live project, some of
#: them "Approve and merge" on pull requests from weeks ago); the v2 queue counted every
#: revision_requested, including the ones waiting on the proposer. The landing page and the
#: inbox disagreed about what was waiting.
#:
#:   needs_action  pending or delegated and not past its deadline, or parked in a
#:                 revision_requested nothing reads (the stuck pull requests): the operator is
#:                 the only way it moves.
#:   expired       expired with a hold default, so nothing was applied and it can still be
#:                 answered. Shown in its own group with its age, never in the default view:
#:                 most of these are about work that has long moved on.
#:   resolved      everything else, including a revision_requested that the proposer is
#:                 working on.
ATTENTION = ("needs_action", "expired", "resolved")

#: The statuses a needs_action or expired decision can be in. A reader that selects in SQL
#: selects these and classifies the rows with `attention`; the rule itself is never in SQL.
ATTENTION_CANDIDATE_STATUSES = ("pending", "delegated", "revision_requested", "expired")
#: The statuses a needs_action decision can be in (the same, without expired).
NEEDS_ACTION_CANDIDATE_STATUSES = ("pending", "delegated", "revision_requested")


def _deadline_passed(decision: Mapping[str, Any], now: Optional[datetime]) -> bool:
    deadline = decision.get("deadline_at")
    if isinstance(deadline, str):
        try:
            deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(deadline, datetime):
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= (now or datetime.now(timezone.utc))


def attention(decision: Mapping[str, Any], now: Optional[datetime] = None) -> str:
    """Which of ATTENTION this decision is in, right now.

    A pending decision past its deadline is classified as the expiry would leave it. The inbox
    runs `expire_overdue` before it reads and the Command Center does not (it is a read), so
    without this a card overdue by a minute counted as waiting on the landing page and as
    expired in the inbox.
    """
    status = str(decision.get("status") or "")
    if status in _OPEN and _deadline_passed(decision, now):
        status = "expired"
    if status in _OPEN:
        return "needs_action"
    if status == "revision_requested":
        return "resolved" if consumes_revision(decision) else "needs_action"
    if status in _REOPENABLE and reopenable({**dict(decision), "status": status}):
        return "expired"
    return "resolved"


def _sort_key(row: Mapping[str, Any]) -> tuple:
    deadline = row.get("deadline_at")
    created = row.get("created_at")
    return (
        -int(row.get("risk_tier") or 0),
        deadline is None, str(deadline or ""),
        str(created or ""),
    )


def waiting_on_operator(connection, project_id: str, *, now: Optional[datetime] = None,
                        include_expired: bool = False, min_risk_tier: Optional[int] = None,
                        task_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Every decision in the project that `attention` puts in front of the operator.

    THE ONE READER for "how many decisions are waiting on you". Review round 3 found the
    Approvals nav badge saying 2 while the inbox and the Command Center said 3 over the same
    rows: GET /v1/status counted `status IN ('pending', 'delegated')` itself and so left out a
    pull request parked in a dead-end revision_requested. The Workflow screen's Decisions KPI,
    the dashboard's Awaiting You, the chat's factory briefing, health, ops and Metatron each
    kept the same list of their own. They all read this now, and
    engine/tests/test_every_surface_agrees_on_what_needs_the_operator.py fails on any reader
    in engine/ or apps/ that writes its own list again.

    Candidates are selected in SQL and classified here, never in SQL, so the rule lives in one
    place. Sorted most urgent first: highest risk, nearest deadline, oldest. `include_expired`
    adds the expired hold-default decisions that can still be answered.
    """
    from sqlalchemy import bindparam, text

    statuses = ATTENTION_CANDIDATE_STATUSES if include_expired else NEEDS_ACTION_CANDIDATE_STATUSES
    where = ["project_id = :project_id", "status IN :statuses"]
    params: dict[str, Any] = {"project_id": project_id, "statuses": list(statuses)}
    binds = [bindparam("statuses", expanding=True)]
    if min_risk_tier is not None:
        where.append("risk_tier >= :min_risk_tier")
        params["min_risk_tier"] = int(min_risk_tier)
    if task_ids is not None:
        if not task_ids:
            return []
        where.append("task_id IN :task_ids")
        params["task_ids"] = list(task_ids)
        binds.append(bindparam("task_ids", expanding=True))
    statement = text(
        "SELECT * FROM decisions WHERE " + " AND ".join(where)
    ).bindparams(*binds)
    wanted = {"needs_action", "expired"} if include_expired else {"needs_action"}
    rows = [dict(r) for r in connection.execute(statement, params).mappings().all()]
    return sorted((r for r in rows if attention(r, now) in wanted), key=_sort_key)


def count_waiting_on_operator(connection, project_id: str, *, now: Optional[datetime] = None,
                              min_risk_tier: Optional[int] = None) -> int:
    """How many decisions need the operator: the number every badge and KPI shows."""
    return len(waiting_on_operator(connection, project_id, now=now,
                                   min_risk_tier=min_risk_tier))


def describe(decision: Mapping[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """The fields every decision card carries: kind, actions, guidance and attention."""
    return {
        "kind": kind_of(decision),
        "allowed_actions": allowed_actions(decision),
        "guidance": guidance(decision),
        "attention": attention(decision, now),
    }


class DecisionActionNotAllowed(ValueError):
    """The action is not one this decision offers. The message says what to do instead."""


def require_allowed(decision: Mapping[str, Any], action: str) -> None:
    """Refuse an action the decision does not offer, saying what would help.

    Only the KIND is enforced here. Whether the status allows it (already approved, expired
    with a non-hold default) stays with the transition itself, which has always answered that
    and words it for the case.
    """
    kind = kind_of(decision)
    offered = {a["action"] for a in allowed_actions({**dict(decision), "status": "pending"})}
    offered.add("execute")
    if action in offered:
        return
    if kind == "refusal":
        hint = guidance(decision) or {}
        raise DecisionActionNotAllowed(
            f"This is a notice, not a question: {hint.get('what_happened', '')} "
            f"{action.capitalize()} would not change anything. {hint.get('what_to_do', '')} "
            "Dismiss it once you have."
        )
    if action == "revise":
        raise DecisionActionNotAllowed(
            "Nothing reads a request for changes on this decision, and it could not be "
            "approved afterwards. Reject it to decline, or approve it. To change the work "
            "itself, start that change: for an app, Add a feature or Rebuild with a fresh "
            "spec in Factory."
        )
    raise DecisionActionNotAllowed(
        f"'{action}' is not available on a {kind.replace('_', ' ')} decision. "
        f"Choose one of: {', '.join(sorted(offered - {'execute'}))}."
    )
