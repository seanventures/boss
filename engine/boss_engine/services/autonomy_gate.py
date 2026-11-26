"""THE ONE CHOKE POINT for dev autonomy (BOSSAPP-1441): which cards BOSS answers itself.

The operator, 2026-11-23, the third time: "I dont like everytime asking me to do stuff.. it is
unnecessary ask, that too for development, it makes sense for PROD deployment and other fixes
that release to PROD. But for local development I dont need to validate just make the output..
then human can validate it." And earlier: "BOSS agents are supposed to fix autonomously without
help unattended."

Every card BOSS raises already goes through `core._create_blocking_decision_postgres` (and the
direct inserts the producer scan names). That sink calls `on_created` here. `route` classifies the
card by its action (`ACTIONS`, or `GATES` for a workflow gate), resolves production through
services/production.py from the card's own ids, and reads the policy (services/autonomy_policy.py).

When the answer is ACT the card is still written — one audit trail — and is then decided with the
SAME follow-on a person's Approve runs (DecisionService.transition, so a merge merges, a deploy
deploys, a plan resumes its build). It is recorded as decided by `autonomy:dev:<agent>`
("Decided by <Agent> under dev autonomy"), with the verdict, the evidence and an undo kind on the
card (`recommendation.autonomy`) and a `decision.auto_decided` event. It is decided AFTER the
producer's transaction commits (the producer still links its run to the card after creating it),
by a short-lived thread, and by the scheduler's sweep as the backstop.

Outside production a card may wait for a person ONLY for these classes (ALLOWED_DEV_WAITS,
pinned by test_only_allowed_classes_wait_outside_production):
  * ask_credential   a real credential a person types (a secret, a sign-in, a missing key)
  * ask_spend        spend beyond the budget the person set (plus the dev top-up)
  * explicit_stop    a stop a PERSON set ("Review the plan first", a tool set to ask, a review
                     stage with people on it, "Review always-on builds before they start")
  * ask_prod_release a release to the public or to production by construction
And every ask_prod_only card asks when the resolver says production — an unlabelled remote, an
unknown target, a merge into a default branch whose destination nobody set.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from sqlalchemy import text

from ..db import release_failed_savepoint

log = logging.getLogger(__name__)

# ---- classes ----------------------------------------------------------------------------------

ACT = "act"                         # carry it out and log it
ACT_CAPPED = "act_capped"           # carry it out within the dev top-up / tier cap
NOTICE = "notice"                   # a notice, not a question: acknowledge and log
ASK_PROD_ONLY = "ask_prod_only"     # act outside production, ask in production (or unknown)
ASK_CREDENTIAL = "ask_credential"
ASK_SPEND = "ask_spend"
EXPLICIT_STOP = "explicit_stop"
ASK_PROD_RELEASE = "ask_prod_release"

CLASSES = (ACT, ACT_CAPPED, NOTICE, ASK_PROD_ONLY, ASK_CREDENTIAL, ASK_SPEND, EXPLICIT_STOP,
           ASK_PROD_RELEASE)
#: The only reasons a card may wait for a person OUTSIDE production.
ALLOWED_DEV_WAITS = frozenset({ASK_CREDENTIAL, ASK_SPEND, EXPLICIT_STOP, ASK_PROD_RELEASE})
#: The classes BOSS answers itself in development.
ACTS_IN_DEV = frozenset({ACT, ACT_CAPPED, NOTICE, ASK_PROD_ONLY})

ACTOR_PREFIX = "autonomy:dev:"
EVENT = "decision.auto_decided"

# ---- undo -------------------------------------------------------------------------------------

#: Undo kinds with a handler (`undo`). A kind starting `none:` has no undo, and says why.
UNDO_KINDS = ("send_back", "revert_merge", "rollback_deploy", "revoke_grant", "disable_skill",
              "remove_self_fix", "cancel_run")
NONE_RECORD = "none:it only recorded an answer; nothing was carried out that can be taken back"
NONE_NOTICE = "none:a notice; acknowledging it changed nothing"
NONE_SPEND = "none:spend cannot be recalled; lower the top-up in the autonomy policy"
NONE_RETRY = "none:a retry only re-ran the work; there is nothing to take back"
NONE_VERSION = "none:the previous version stays in its history; restore it from there"
NONE_PUBLISH = "none:a draft or page can be edited or deleted where it was written"


@dataclass(frozen=True)
class Rule:
    cls: str
    undo: str
    agent: str
    #: Where its production verdict comes from: target | merge | install | incident | ""
    scope: str = ""


def _r(cls: str, undo: str, agent: str, scope: str = "") -> Rule:
    return Rule(cls, undo, agent, scope)


_EM = "engineering_captain"
_RM = "release_manager"
_DM = "deployment_manager"
_PM = "product_manager"
_FIN = "finops_sentinel"
_INF = "infra_steward"
_SEC = "security_reviewer"
_SRE = "infra_steward"

#: EVERY ACTION BOSS WRITES ON A CARD (decision_actions.KNOWN_ACTIONS and the producer scan).
ACTIONS: dict[str, Rule] = {
    # A. build pipeline
    "resume_code_task": _r(ACT, "send_back", _EM),
    "review_plan_draft": _r(ACT, "send_back", _EM),
    "review_pull_request": _r(ASK_PROD_ONLY, "revert_merge", _RM, "merge"),
    "approve_change_set": _r(ASK_PROD_ONLY, "revert_merge", _RM, "merge"),
    "retry_when_secret_set": _r(ASK_CREDENTIAL, NONE_RECORD, _EM),
    "start_recheck_followup": _r(ACT_CAPPED, "cancel_run", _EM),
    "start_delivery_workflow": _r(ACT, "cancel_run", _EM),
    "waive_finding": _r(ACT, NONE_RECORD, _SEC),
    "restore_model_capacity_or_raise_budget": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "model_capacity_exhausted": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "model_capacity_denied": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "allow_repository_or_change_clone_target": _r(ASK_PROD_RELEASE, NONE_NOTICE, _INF),
    "rules_forbidden": _r(ASK_PROD_RELEASE, NONE_NOTICE, _INF),
    "configure_target_github_and_retry_phase": _r(ASK_CREDENTIAL, NONE_RECORD, _EM),
    "fix_phase_spec_and_retry": _r(ACT, NONE_RECORD, _EM),
    "stuck_work_needs_person": _r(ASK_CREDENTIAL, NONE_NOTICE, _EM),
    "failure_owner.retry": _r(ACT, NONE_RETRY, _EM),
    "grant_builder_authority": _r(ASK_PROD_RELEASE, NONE_RECORD, _EM),
    "review_always_on_build": _r(EXPLICIT_STOP, NONE_RECORD, _EM),
    "sign_in_to_subscription_again": _r(ASK_CREDENTIAL, NONE_RECORD, _INF),
    # B. spec, PRD, architecture, meetings
    "execute_agent_tool": _r(ACT, "send_back", _PM),
    "review_architecture": _r(ACT, "send_back", "chief_architect"),
    "review_stalled_meeting": _r(ACT, NONE_RECORD, _PM),
    "decide": _r(ACT, NONE_RECORD, _PM),
    "choose_option": _r(ACT, NONE_RECORD, _PM),
    "answer_form": _r(ACT, NONE_RECORD, _PM),
    "execution_stage": _r(ACT, NONE_RECORD, _PM),
    "ticket_pickup": _r(ACT, "cancel_run", _PM),
    "ticket_changed_mid_run": _r(ACT, NONE_RECORD, _PM),
    "resume_workflow": _r(ACT, NONE_RECORD, _PM),
    "review_proposed_rules": _r(ACT, NONE_RECORD, _PM),
    "review_proposed_trace": _r(ACT, NONE_RECORD, _PM),
    # C. agent tool calls, grants, skills, prompts
    "resume_agent_run": _r(ACT_CAPPED, NONE_RECORD, "software_engineer"),
    "resume_interrupt": _r(ACT_CAPPED, NONE_RECORD, "software_engineer"),
    "gateway_tool_call": _r(ACT_CAPPED, NONE_RECORD, "software_engineer"),
    "gateway_ask_person": _r(ACT, NONE_RECORD, "software_engineer"),
    "gateway_request_approval": _r(ACT_CAPPED, NONE_RECORD, "software_engineer"),
    "review_tool_change": _r(ASK_PROD_RELEASE, NONE_VERSION, _SEC),
    "grant_tool_access": _r(ACT_CAPPED, "revoke_grant", _SEC),
    # These three change the agents production work runs with: a person approves them
    # (PERSON_ONLY_ACTIONS, the list a setting may never approve either).
    "activate_authored_skill": _r(ASK_PROD_RELEASE, "disable_skill", _EM),
    "apply_agent_prompt_diff": _r(ASK_PROD_RELEASE, NONE_VERSION, _EM),
    "apply_wiki_patch": _r(ACT, NONE_VERSION, _PM),
    "apply_confluence_publish": _r(ACT, NONE_PUBLISH, _PM),
    "role.hire": _r(ASK_PROD_RELEASE, NONE_RECORD, "vp_agents"),
    "role.autonomy_change": _r(ASK_PROD_RELEASE, NONE_RECORD, "vp_agents"),
    "ambient_card": _r(ACT, NONE_RECORD, "metatron"),
    "ambient_route": _r(ACT, NONE_RECORD, "metatron"),
    # D. budget and spend
    "increase_budget_or_defer": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "increase_allocation_or_defer": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "raise_cap_or_wait_for_headroom": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "budget_tier2_frozen": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "budget_denied": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "budget_deferred": _r(NOTICE, NONE_NOTICE, _FIN),
    "budget.override": _r(ACT_CAPPED, NONE_SPEND, _FIN),
    "typed_decision_reconciliation": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "architecture_reconciliation": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "discovery_reconciliation": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "conversation_reconciliation": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "typed_decision_lease_expired": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "reconcile_legacy_model_exposure": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "reconcile_legacy_provider_exposure": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "verify_provider_outcome_before_retry": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "review_model_spend_and_host_effects_before_retry": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "review_model_spend_and_receipts_before_retry": _r(ASK_SPEND, NONE_RECORD, _FIN),
    "review_budget_policy": _r(NOTICE, NONE_NOTICE, _FIN),
    "budget_state_deferred_job": _r(NOTICE, NONE_NOTICE, _FIN),
    "create_budget_reservation": _r(ACT, NONE_RECORD, _FIN),
    "reserve_budget_before_deployment": _r(ACT, NONE_RECORD, _FIN),
    "reserve_provider_budget": _r(ACT, NONE_RECORD, _FIN),
    "increase_budget_or_defer_work": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "approve_budget_increase_or_accept_degraded_mode": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "review_cost_anomaly": _r(ASK_SPEND, NONE_NOTICE, _FIN),
    "review_provider_spend_kill_switch": _r(ASK_SPEND, NONE_NOTICE, _FIN),
    "review_commitment": _r(NOTICE, NONE_NOTICE, _FIN),
    "approve_initiative_budget": _r(ASK_SPEND, NONE_SPEND, _FIN),
    "set_BOSS_INFRA_REAL_PROVISION_ALLOWED": _r(ASK_SPEND, NONE_SPEND, _INF),
    # E. deploy, infra, environments
    "deploy_host": _r(ASK_PROD_ONLY, "rollback_deploy", _DM, "target"),
    "deploy_stack": _r(ASK_PROD_ONLY, "rollback_deploy", _DM, "target"),
    "config_deploy": _r(ASK_PROD_ONLY, "rollback_deploy", _DM, "target"),
    "review_deployment": _r(ASK_PROD_ONLY, "rollback_deploy", _DM, "target"),
    "review_deployment_health": _r(ASK_PROD_ONLY, NONE_RECORD, _DM, "target"),
    "review_rollback_or_retry": _r(ASK_PROD_ONLY, NONE_RECORD, _DM, "target"),
    "approve_changeset": _r(ASK_PROD_ONLY, NONE_RECORD, _DM, "target"),
    "reconcile_provider_changeset": _r(ASK_PROD_ONLY, NONE_RECORD, _DM, "target"),
    "infra_operation": _r(ASK_PROD_ONLY, NONE_RECORD, _INF, "target"),
    "update_deployment_environment_policy": _r(ASK_PROD_ONLY, NONE_RECORD, _DM, "target"),
    "plan_governed_changeset": _r(ASK_PROD_ONLY, NONE_RECORD, _DM, "target"),
    "review_infra_provisioning": _r(ASK_PROD_ONLY, NONE_RECORD, _INF, "target"),
    "configure_or_enable_infra_provisioning": _r(ASK_SPEND, NONE_SPEND, _INF),
    "incident_needs_owner": _r(ASK_PROD_ONLY, NONE_RECORD, _SRE, "target"),
    "promote_agent_deployment": _r(ASK_PROD_RELEASE, NONE_RECORD, "vp_agents"),
    "restore_metric_evaluator": _r(ACT, NONE_RECORD, _SRE),
    "investigate_regression": _r(ACT, NONE_RECORD, _SRE),
    "use_settled_conclusion_or_reopen_with_new_evidence": _r(ACT, NONE_RECORD, _SRE),
    "attach_required_evidence": _r(ACT, NONE_RECORD, _SRE),
    "resolve_assurance_case": _r(ACT, NONE_RECORD, _SRE),
    "coordinate_with_surface_owner": _r(ASK_PROD_ONLY, NONE_RECORD, _SRE, "target"),
    "restore_verifier_preconditions": _r(ACT, NONE_RECORD, _SRE),
    "reopen_and_investigate": _r(ACT, NONE_RECORD, _SRE),
    # F. BOSS's own install, storage
    "self_fix.install": _r(ASK_PROD_ONLY, "remove_self_fix", _EM, "install"),
    "self_fix.remove": _r(ACT, NONE_RECORD, _EM),
    "self_improvement.begin_canary": _r(ASK_PROD_ONLY, NONE_VERSION, _EM, "install"),
    "self_improvement.rollout": _r(ASK_PROD_ONLY, NONE_VERSION, _EM, "install"),
    "storage_delete": _r(ASK_PROD_ONLY, "none:deleted storage cannot be restored by undoing this decision", _INF, "install"),
    "storage_rule": _r(ACT, NONE_RECORD, _INF),
    # G. credentials, external channels, data boundary
    "provide_github_token": _r(ASK_CREDENTIAL, NONE_RECORD, _EM),
    "provide_openai_api_key": _r(ASK_CREDENTIAL, NONE_RECORD, _EM),
    "review_entitlement_sources": _r(ASK_CREDENTIAL, NONE_RECORD, _EM),
    "store_proposed_secret": _r(ASK_CREDENTIAL, NONE_RECORD, _SEC),
    "configure_supported_channel": _r(ASK_CREDENTIAL, NONE_RECORD, _PM),
    "upsert_comms_channel_policy": _r(ACT, NONE_RECORD, _PM),
    "reclassify_or_configure_approved_boundary_adapter": _r(ASK_PROD_RELEASE, NONE_NOTICE, _SEC),
    "review_plane_sync_failure": _r(NOTICE, NONE_NOTICE, _PM),
    "review_plane_sync_uncertain": _r(NOTICE, NONE_NOTICE, _PM),
    "confluence_publish_failed": _r(NOTICE, NONE_NOTICE, _PM),
    "github.community.triage_review": _r(ACT, NONE_RECORD, _RM),
    "github.community.dispatch_plan": _r(ACT, NONE_RECORD, _RM),
    "github.community.merge_authorization": _r(ASK_PROD_RELEASE, "revert_merge", _RM),
    # H. workflow control
    "change_sync_branch": _r(ACT, NONE_RECORD, _RM),
    # The incident squad's mitigation (ambient Needs-you): production incidents keep a person.
    "incident_squad.mitigate": _r(ASK_PROD_ONLY, NONE_RECORD, _SRE, "incident"),
}
#: Cards that write no `action` (the producer scan's NO_ACTION_BY_DESIGN), by what they carry.
KINDS: dict[str, Rule] = {
    # An external SRE runbook on an open incident: production incidents keep a person.
    "sre_runbook": _r(ASK_PROD_ONLY, NONE_RECORD, _SRE, "incident"),
    # A deploy would use a LIVE app secret: a credential a person stands behind.
    "app_secret_live": _r(ASK_CREDENTIAL, NONE_RECORD, _SEC),
}
#: A meeting's own decision item: the panel's recommendation is recorded as the answer.
MEETING_ITEM = _r(ACT, NONE_RECORD, _PM)
#: Producers that build the action from a provider key (comms: configure_or_enable_slack_...):
#: turning on a channel provider is a credential a person enters.
PREFIXES: dict[str, Rule] = {
    "configure_or_enable_": _r(ASK_CREDENTIAL, NONE_RECORD, _PM),
}

#: THE ACTIONS ONLY A PERSON APPROVES — never dev autonomy (route() asks) and never a setting
#: (decisions.approve_by_policy refuses SETTING_NEVER_APPROVES, which contains these). They change
#: the agents production work runs with: an agent-drafted skill, an agent's prompt, a new hire.
PERSON_ONLY_ACTIONS = frozenset({"activate_authored_skill", "apply_agent_prompt_diff",
                                 "role.hire"})
#: What a SETTING (the autonomy policy's approve_by_policy) never approves: the person-only
#: actions, plus an architecture and BOSS's own self-fix (their follow-ons need a person's
#: transition; dev autonomy routes them by ACTIONS).
SETTING_NEVER_APPROVES = PERSON_ONLY_ACTIONS | frozenset({
    "review_architecture", "self_fix.install", "self_fix.remove"})

#: Workflow gates, by capability (a gate card carries `capability`, not an action).
_DEPLOY_GATES = ("deploy", "redeploy", "config_deploy")
GATES: dict[str, Rule] = {
    **{key: _r(ASK_PROD_ONLY, "rollback_deploy", _DM, "target") for key in _DEPLOY_GATES},
    # Promoting configuration INTO production is a production release by construction.
    "promote_config": _r(ASK_PROD_RELEASE, NONE_RECORD, _DM),
    # Rolling configuration back: staging acts, production (or unknown) asks (BOSSAPP-1173).
    "rollback_config": _r(ASK_PROD_ONLY, NONE_RECORD, _DM),
    # A release gate a workflow's author put in front of a release: a stop a person set.
    "release_approval": _r(EXPLICIT_STOP, NONE_RECORD, _RM),
    "social_publish": _r(ASK_PROD_RELEASE, NONE_RECORD, "comms_manager"),
    "spec_review": _r(ACT, "send_back", _PM),
    "ux_review": _r(ACT, NONE_RECORD, "design_critic"),
    "security_review": _r(ACT, NONE_RECORD, _SEC),
    "security_decide": _r(ACT, NONE_RECORD, _SEC),
    "rules_review": _r(ACT, NONE_RECORD, _PM),
    "regulatory_trace_review": _r(ACT, NONE_RECORD, _PM),
    "fix_pr": _r(ACT, "revert_merge", _EM),
    # A message or a comment that may reach people outside the team: production by the
    # resolver's own rule (production.resolve_audience), unless it names the team's own channel.
    "notify": _r(ASK_PROD_ONLY, NONE_RECORD, "comms_manager", "audience"),
    "github_comment": _r(ASK_PROD_ONLY, NONE_RECORD, _RM, "audience"),
    "merge": _r(ASK_PROD_ONLY, "revert_merge", _RM, "merge"),
    # A step whose AUTHOR chose "manual" ("Let an agent work it out", ask me first).
    "agent_work": _r(EXPLICIT_STOP, NONE_RECORD, _EM),
}
#: A gate of a capability not listed: decided by its tier (tier 3 is a production release).
GATE_DEFAULT = _r(ACT, NONE_RECORD, _EM)


def rule_for(decision: Mapping[str, Any]) -> Optional[Rule]:
    rec = _rec(decision)
    action = str(rec.get("action") or "")
    if action in ACTIONS:
        return ACTIONS[action]
    if decision_actions_is_ambient(decision):
        # An always-on agent's Needs-you card: its action is the asker's own (route() still
        # keeps an entry on Where people decide for a person).
        return ACTIONS["ambient_card"]
    kind = str(rec.get("kind") or "")
    if not action and kind in KINDS:
        return KINDS[kind]
    if not action and rec.get("meeting_id"):
        return MEETING_ITEM
    for prefix, rule in PREFIXES.items():
        if action.startswith(prefix):
            return rule
    capability = str(rec.get("capability") or "")
    if capability and not action:
        return GATES.get(capability, GATE_DEFAULT)
    if not action and rec.get("workflow_run_id"):
        return GATE_DEFAULT
    reason = str(rec.get("reason_code") or "")
    if reason in ACTIONS:
        return ACTIONS[reason]
    return None


def decision_actions_is_ambient(decision: Mapping[str, Any]) -> bool:
    from .decision_actions import is_ambient_card

    try:
        return is_ambient_card(dict(decision))
    except Exception:  # noqa: BLE001
        return False


def _rec(decision: Mapping[str, Any]) -> dict[str, Any]:
    rec = decision.get("recommendation") if isinstance(decision, Mapping) else None
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    return dict(rec) if isinstance(rec, Mapping) else {}


# ---- routing ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    answer: str                     # act | ask
    cls: str
    why: str
    agent: str = _EM
    undo: str = NONE_RECORD
    verdict: Optional[dict] = None
    policy: dict = field(default_factory=dict)

    @property
    def acts(self) -> bool:
        return self.answer == "act"

    @property
    def what_to_do(self) -> str:
        """The person's next action and where, when the card waits for them; else ""."""
        if self.acts:
            return ""
        return ASK_WHAT_TO_DO.get(self.cls, ASK_WHAT_TO_DO[ASK_PROD_RELEASE])

    def record(self) -> dict[str, Any]:
        out = {"route": self.answer, "class": self.cls, "why": self.why, "agent": self.agent,
               "undo": self.undo, "verdict": self.verdict, "policy": self.policy,
               "human_reviewed": False}
        if not self.acts:
            out["what_to_do"] = self.what_to_do
        return out


#: WHAT THE PERSON DOES with a card BOSS left for them, by its class (BOSSAPP-1428: every
#: failure, and every "why this waits for you", says what to do and where). The route's `why`
#: is the what; this is the action, stamped beside it on the card's autonomy record.
ASK_WHAT_TO_DO: dict[str, str] = {
    ASK_CREDENTIAL: "Enter the sign-in or key it needs, then answer it on this card.",
    ASK_SPEND: "Approve or reject the spend on this card, or raise the cap in Govern.",
    ASK_PROD_RELEASE: "Approve or reject it on this card.",
    EXPLICIT_STOP: "Answer it on this card; the stop stays until the person who set it lifts "
                   "it.",
    "unclassified": "Answer it on this card.",
}


#: A failure only a person can clear: a sign-in or key they type, or the host's admin (sudo).
_CREDENTIAL_CAUSES = frozenset({"repo_access", "model_login", "registry_access",
                                "connector_login", "host", "engine_write_unavailable"})
_SPEND_CAUSES = frozenset({"budget"})


def _explicit_stop(action: str, rec: Mapping[str, Any]) -> Optional[str]:
    """A stop a PERSON set, in words, or None."""
    said = rec.get("explicit_stop")
    if said:
        return str(said) if isinstance(said, str) else "a person asked to be asked"
    if action == "execution_stage":
        people = [p for p in (rec.get("participants") or [])
                  if not str(p).startswith(("agent:", "role:"))]
        if people:
            return "a review stage a person set, with people on it"
    if str(rec.get("approval_mode_requested") or "") == "manual":
        return "the workflow's author set this step to ask"
    return None


def _scope_of(rec: Mapping[str, Any], rule: Rule) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    if rule.scope == "install":
        return {"install": True}
    for key in ("target_id", "deployment_target_id", "deployment_run_id", "changeset_id"):
        if rec.get(key):
            scope[key] = rec[key]
    if rule.scope == "merge":
        url = str(rec.get("pull_request_url") or rec.get("pr_url") or "")
        if "://boss-local/" in url:
            # A local repository's change set: BOSS's own bare repository on this machine.
            return {"environment": "local", "host": "localhost"}
        base = rec.get("pr_base") or rec.get("base_branch") or rec.get("base")
        repo = rec.get("full_name") or rec.get("repo_full_name") or (
            f"{rec['owner']}/{rec['repo']}" if rec.get("owner") and rec.get("repo")
            else rec.get("repo"))
        if not repo and "/pull/" in url:
            parts = url.split("://", 1)[-1].split("/")
            if len(parts) >= 3:
                repo = f"{parts[1]}/{parts[2]}"
        if isinstance(rec.get("change_set"), Mapping):
            base = base or rec["change_set"].get("base")
        # No base named: the repository's default branch (production.resolve_merge reads it).
        return {"pr_base": base or production_default_base(), "repo": repo}
    if rec.get("environment") and not scope:
        scope["environment"] = rec["environment"]
        if rec.get("host") or rec.get("address"):
            scope["host"] = rec.get("address") or rec.get("host")
    return scope


def production_default_base() -> str:
    """The marker for "the repository's default branch" (production.DEFAULT_BRANCH)."""
    from .production import DEFAULT_BRANCH

    return DEFAULT_BRANCH


def route(connection: Any, decision: Mapping[str, Any], *, policy: Any = None) -> Route:
    """What dev autonomy does with this card: act, or ask — and why, in words."""
    from . import autonomy_policy, production

    rec = _rec(decision)
    action = str(rec.get("action") or "")
    rule = rule_for(decision)
    project_id = str(decision.get("project_id") or "")
    if policy is None:
        policy = autonomy_policy.load(connection, project_id or None)
    pol = {"dev": policy.dev, "source": policy.source, "set_by": policy.set_by}
    if rule is None:
        # A card nobody classified asks — and the enforcing test fails on the producer.
        return Route("ask", "unclassified", "no autonomy class for this card", policy=pol)
    stop = _explicit_stop(action, rec)
    if stop:
        return Route("ask", EXPLICIT_STOP, stop, rule.agent, rule.undo, policy=pol)
    if rule.scope == "merge":
        from .checks_failing import of_decision

        if of_decision(decision) is not None:
            # CHECKS FAILING (checks_failing.py): merging over red checks is a person's stated
            # decision with a written reason; dev autonomy never makes it. The follow-up build
            # fixes the checks and the merge card comes back green.
            return Route("ask", EXPLICIT_STOP, "its checks are failing; merging anyway needs a "
                         "person's reason", rule.agent, rule.undo, policy=pol)
    two = _two_person_tier(decision, rec)
    if two is not None:
        # THE TWO-PERSON RULE (identity/approvals.TWO_PERSON_TIERS). Dev autonomy decides through
        # transition(system_policy=True), which does not run approvals.require_approval, so a
        # card of a two-person tier is never answered by it — whatever its class, findings or
        # environment.
        return Route("ask", ASK_PROD_RELEASE, f"a tier-{two} card needs two people to approve "
                     "it (the two-person rule)", rule.agent, rule.undo, policy=pol)
    if action in PERSON_ONLY_ACTIONS:
        return Route("ask", ASK_PROD_RELEASE, "it changes the agents production work runs "
                     "with; a person approves it", rule.agent, rule.undo, policy=pol)
    cls = rule.cls
    # Dynamic classes a card's own facts decide.
    if action == "execute_agent_tool" and str(rec.get("tool") or "") != "publish_document":
        # An agent-proposed BOSS tool (boss.propose_fix): only a reversible, tier-1/2 tool that
        # does not ship, delete, grant or reach a host acts; the rest are production-shaped.
        tool = str(rec.get("tool") or "")
        tier = int(decision.get("risk_tier") or 2)
        if tier >= 3 or tool in _PROPOSED_TOOLS_THAT_ASK:
            return Route("ask", ASK_PROD_RELEASE, f"the proposed tool {tool or '?'} ships, "
                         "deletes, grants or reaches a host", rule.agent, rule.undo, policy=pol)
    if action == "grant_tool_access":
        tiers = [int((t or {}).get("tier") or 0) for t in rec.get("tools") or []
                 if isinstance(t, Mapping)]
        if not tiers or max(tiers) >= 3:
            return Route("ask", ASK_PROD_RELEASE, "the grant includes a tier-3 tool",
                         rule.agent, rule.undo, policy=pol)
    if action == "choose_option" and not _has_recommendation(rec):
        return Route("ask", EXPLICIT_STOP, "the agent offered choices without recommending "
                     "one", rule.agent, rule.undo, policy=pol)
    if action == "failure_owner.retry":
        cause = str(rec.get("cause_code") or "")
        if cause in _CREDENTIAL_CAUSES:
            cls = ASK_CREDENTIAL
        elif cause in _SPEND_CAUSES:
            cls = ASK_SPEND
        elif connection is not None and _retried_recently(connection, project_id, rec):
            # ONE RETRY BY ITSELF, not a loop: the same failure back after BOSS retried it is
            # spend on the same failure again.
            return Route("ask", ASK_SPEND, "BOSS already retried this failure itself today; "
                         "another retry would spend again on the same failure", rule.agent,
                         rule.undo, policy=pol)
    if action in ("resume_agent_run", "resume_interrupt", "gateway_tool_call",
                  "gateway_request_approval", "grant_tool_access", "review_tool_change"):
        tier = int(decision.get("risk_tier") or rec.get("risk_tier") or 2)
        if tier >= 3:
            return Route("ask", ASK_PROD_RELEASE, f"a tier-{tier} tool call is treated as "
                         "production", rule.agent, rule.undo, policy=pol)
        if rec.get("unreviewed_server") or rec.get("tool_screen") == "untrusted":
            return Route("ask", ASK_PROD_RELEASE,
                         "it acts on an unreviewed server or on untrusted content, which is "
                         "treated like an unlabelled remote: production", rule.agent, rule.undo,
                         policy=pol)
    if rule is GATE_DEFAULT:
        tier = int(decision.get("risk_tier") or 2)
        if rec.get("requires_person"):
            return Route("ask", ASK_PROD_RELEASE, str(rec["requires_person"])[:200],
                         rule.agent, rule.undo, policy=pol)
        if tier >= 3 and not rec.get("findings"):
            return Route("ask", ASK_PROD_RELEASE, f"a tier-{tier} step is treated as "
                         "production", rule.agent, rule.undo, policy=pol)
    if action in ("ambient_card", "ambient_route") or rule is ACTIONS["ambient_card"]:
        # AN ALWAYS-ON AGENT'S ASK: the asking agent decides it, except an entry on Where people
        # decide (decide_list.classify), which keeps its person.
        from . import decide_list

        kind = decide_list.classify(dict(decision))
        if kind:
            cls_for = {"spend": ASK_SPEND, "secrets": ASK_CREDENTIAL}.get(kind, ASK_PROD_RELEASE)
            return Route("ask", cls_for, f"on Where people decide: {kind}", rule.agent,
                         rule.undo, policy=pol)
    if rec.get("requires_person_approval") or rec.get("ci_changed"):
        return Route("ask", ASK_PROD_RELEASE, "it changes CI, which gates every production "
                     "release", rule.agent, rule.undo, policy=pol)
    if cls in (ASK_CREDENTIAL, ASK_SPEND, ASK_PROD_RELEASE, EXPLICIT_STOP):
        words = {ASK_CREDENTIAL: "a credential only a person can enter",
                 ASK_SPEND: "spend beyond the budget a person set",
                 ASK_PROD_RELEASE: "a release to production or the public",
                 EXPLICIT_STOP: "a stop a person set on this step"}[cls]
        return Route("ask", cls, words, rule.agent, rule.undo, policy=pol)
    if not policy.dev_acts:
        return Route("ask", EXPLICIT_STOP, "this project's autonomy policy says ask in "
                     "development", rule.agent, rule.undo, policy=pol)
    if cls == ASK_PROD_ONLY:
        scope = _scope_of(rec, rule)
        if rule.scope == "incident":
            scope = {"environment": rec.get("environment") or rec.get("incident_environment")}
        elif rule.scope == "audience":
            scope = {"audience": rec.get("channel") or rec.get("audience") or ""}
        verdict = production.resolve(connection, scope, project_id=project_id or None)
        if verdict.prod:
            return Route("ask", ASK_PROD_RELEASE, verdict.words, rule.agent, rule.undo,
                         verdict.as_dict(), pol)
        return Route("act", cls, verdict.words, rule.agent, rule.undo, verdict.as_dict(), pol)
    if cls == ACT_CAPPED and action == "budget.override":
        amount = _usd(rec.get("amount_usd") or rec.get("usd") or rec.get("amount"))
        cap = float(policy.dev_topup_usd.get("per_item", 0))
        if amount is None or amount > cap:
            return Route("ask", ASK_SPEND, f"over the dev top-up of ${cap:,.2f}", rule.agent,
                         rule.undo, policy=pol)
    words = {ACT: "development work; the agent acts and logs it",
             ACT_CAPPED: "development work within the autonomy cap",
             NOTICE: "a notice; nothing waits on an answer"}.get(cls, cls)
    return Route("act", cls, words, rule.agent, rule.undo, policy=pol)


def _two_person_tier(decision: Mapping[str, Any], rec: Mapping[str, Any]) -> Optional[int]:
    """The card's risk tier when it is one the two-person rule covers, else None. A tier that
    needs only a seat (tier 2: the ceo seat) is development work dev autonomy answers, by the
    operator's rule; the seat still binds every PERSON's answer and approve_by_policy."""
    from ..identity.approvals import TWO_PERSON_TIERS

    tiers = []
    for raw in (decision.get("risk_tier"), rec.get("risk_tier")):
        try:
            tiers.append(int(raw))
        except (TypeError, ValueError):
            continue
    hit = [t for t in tiers if t in TWO_PERSON_TIERS]
    return max(hit) if hit else None


#: Agent-proposed tools (execute_agent_tool) that always keep a person.
_PROPOSED_TOOLS_THAT_ASK = frozenset({
    "deploy_app", "redeploy", "remove_app", "run_on_host", "retarget_app", "waive_gates",
    "set_project_setting", "merge_apps", "delete_workflow", "create_deployment_target",
    "register_repository", "forget"})


def _has_recommendation(rec: Mapping[str, Any]) -> bool:
    if rec.get("recommended") or rec.get("recommended_option"):
        return True
    return any(isinstance(o, Mapping) and (o.get("recommended") or o.get("is_recommended"))
               for o in rec.get("options") or [])


def _retried_recently(connection: Any, project_id: str, rec: Mapping[str, Any]) -> bool:
    point = connection.begin_nested()
    try:
        seen = connection.execute(text(
            "SELECT 1 FROM decisions WHERE project_id = :p AND approved_by LIKE :pre "
            "AND recommendation->>'action' = 'failure_owner.retry' "
            "AND recommendation->>'cause_code' = :c "
            "AND COALESCE(recommendation->>'surface', '') = :s "
            "AND approved_at > now() - interval '24 hours' LIMIT 1"),
            {"p": project_id, "pre": ACTOR_PREFIX + "%", "c": str(rec.get("cause_code") or ""),
             "s": str(rec.get("surface") or "")}).first()
    except Exception as error:  # noqa: BLE001 — unknown: no auto retry
        release_failed_savepoint(point, error, connection=connection)
        return True
    point.commit()
    return seen is not None


def _usd(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---- the sink hook ----------------------------------------------------------------------------

def _immediate() -> bool:
    return str(os.environ.get("BOSS_AUTONOMY_IMMEDIATE", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def stamp(connection: Any, decision_id: str, found: Route) -> None:
    connection.execute(text(
        "UPDATE decisions SET recommendation = COALESCE(recommendation, '{}'::jsonb) "
        "|| jsonb_build_object('autonomy', CAST(:a AS JSONB)) WHERE id = :d"),
        {"a": json.dumps(found.record()), "d": decision_id})


def on_created(connection: Any, project_id: str, decision_id: str) -> Optional[Route]:
    """Called by the decision sink right after a NEW card is written: classify it, stamp the
    route on it, and — when BOSS answers it — decide it once the producer's transaction commits.
    Never fails the producer."""
    try:
        point = connection.begin_nested()
    except Exception as error:  # noqa: BLE001 — a connection without savepoints: the sweep routes it
        log.debug("autonomy route for %s deferred: %s", decision_id, error)
        return None
    try:
        row = connection.execute(text(
            "SELECT id, project_id, risk_tier, recommendation, question FROM decisions "
            "WHERE id = :d"), {"d": decision_id}).mappings().first()
        if row is None:
            point.rollback()
            return None
        found = route(connection, dict(row))
        stamp(connection, decision_id, found)
    except Exception as error:  # noqa: BLE001 — a classification never fails the producer
        release_failed_savepoint(point, error, connection=connection)
        log.warning("autonomy route for %s failed: %s", decision_id, error)
        return None
    point.commit()
    if found.acts and _immediate():
        _decide_after_commit(decision_id)
    return found


def _decide_after_commit(decision_id: str, *, wait_s: float = 30.0) -> threading.Thread:
    """Decide the card on a short-lived thread once the producer's row is visible (committed).
    A producer that rolled back leaves no row: the thread gives up; the sweep is the backstop."""
    def run() -> None:
        try:
            from ..config import get_settings
            from ..db import begin, get_engine

            settings = get_settings()
            engine = get_engine(settings)
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                time.sleep(0.5)
                with begin(engine) as c:
                    status = c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                                       {"d": decision_id}).scalar()
                if status is None:
                    continue
                if status != "pending":
                    return
                with begin(engine) as c:
                    decide(c, settings, decision_id)
                return
        except Exception as error:  # noqa: BLE001 — the sweep decides it later
            log.info("autonomy decide of %s deferred to the sweep: %s", decision_id, error)

    from ..db import spawn_thread

    return spawn_thread(run, name=f"autonomy-{decision_id}", daemon=True, as_background=True)


# ---- deciding -----------------------------------------------------------------------------------

def agent_name(agent_key: str) -> str:
    return str(agent_key or "agent").replace("_", " ").strip().capitalize()


def decided_words(agent_key: str) -> str:
    return f"Decided by {agent_name(agent_key)} under dev autonomy"


def _recommended_option(connection: Any, decision_id: str) -> Optional[str]:
    try:
        from . import decision_options

        row = connection.execute(text("SELECT * FROM decisions WHERE id = :d"),
                                 {"d": decision_id}).mappings().first()
        if row is None or not decision_options.is_option_decision(dict(row)):
            return None
        rec = _rec(dict(row))
        options = rec.get("options") or (row.get("options") if isinstance(row.get("options"),
                                                                           list) else [])
        for opt in options or []:
            if isinstance(opt, Mapping) and (opt.get("recommended") or opt.get("is_recommended")):
                return str(opt.get("id") or opt.get("key") or "")
        said = rec.get("recommended_option") or rec.get("recommended")
        return str(said) if isinstance(said, str) and said else None
    except Exception:  # noqa: BLE001
        return None


def decide(connection: Any, settings: Any, decision_id: str) -> dict[str, Any]:
    """Decide a pending card under dev autonomy, with the SAME follow-on a person's Approve runs.
    Re-routes first (the policy or the target may have changed). Returns what happened."""
    from .core import emit_event
    from .decisions import DecisionService

    outer = connection.begin_nested()
    try:
        # One decider at a time: the after-commit thread and the sweep never both answer it.
        row = connection.execute(text(
            "SELECT id, project_id, status, risk_tier, question, recommendation FROM decisions "
            "WHERE id = :d FOR UPDATE SKIP LOCKED"), {"d": decision_id}).mappings().first()
        if row is None or row["status"] != "pending":
            outer.commit()
            return {"decided": False, "why": "not pending"}
        found = route(connection, dict(row))
        stamp(connection, decision_id, found)
    except Exception as error:  # noqa: BLE001 — never poisons the caller's transaction
        release_failed_savepoint(outer, error, connection=connection)
        return {"decided": False,
                "why": (f"BOSS could not route this card ({type(error).__name__}: "
                        f"{str(error)[:160]}), so it waits for a person; answer it on this "
                        "card")[:300]}
    outer.commit()
    if not found.acts:
        return {"decided": False, "why": found.why, "class": found.cls,
                "what_to_do": found.what_to_do}
    actor = f"{ACTOR_PREFIX}{found.agent}"
    rec = _rec(dict(row))
    note = (f"{decided_words(found.agent)}: {found.why}. Undo: "
            f"{found.undo if not found.undo.startswith('none:') else found.undo[5:]}.")
    choose = _recommended_option(connection, decision_id) \
        if rec.get("action") == "choose_option" else None
    point = connection.begin_nested()
    try:
        DecisionService(settings).transition(
            connection, decision_id, action="approve", actor=actor, note=note[:900],
            system_policy=True, choose=choose or None)
    except Exception as refused:  # noqa: BLE001 — a refused or failed follow-on keeps the card
        # for a person, saying why; it is never retried by every sweep.
        release_failed_savepoint(point, refused, connection=connection)
        record = {**found.record(), "route": "ask", "refused": str(refused)[:300],
                  "what_to_do": "BOSS's answer was refused, so the card waits for you: read "
                                "why in Details, then answer it on this card."}
        connection.execute(text(
            "UPDATE decisions SET recommendation = COALESCE(recommendation, '{}'::jsonb) "
            "|| jsonb_build_object('autonomy', CAST(:a AS JSONB)) WHERE id = :d"),
            {"a": json.dumps(record), "d": decision_id})
        emit_event(connection, str(row["project_id"]), "decision.auto_decide_refused", actor,
                   "decision", decision_id, {"why": str(refused)[:300], "class": found.cls})
        return {"decided": False, "why": str(refused)[:300], "class": found.cls}
    point.commit()
    emit_event(connection, str(row["project_id"]), EVENT, actor, "decision", decision_id,
               {"agent": found.agent, "action": rec.get("action") or rec.get("capability"),
                "class": found.cls, "policy": found.policy, "verdict": found.verdict,
                "evidence": found.why, "undo_kind": found.undo,
                "words": decided_words(found.agent)})
    return {"decided": True, "class": found.cls, "agent": found.agent, "undo": found.undo}


def record_decided(connection: Any, project_id: str, *, task_id: Optional[str], question: str,
                   recommendation: Mapping[str, Any], found: Route) -> str:
    """An audit row for an act that opens no card at all (a workflow Deploy step on a
    development target runs without a gate): written already approved, named, with its undo."""
    from .core import emit_event, make_id, now_utc

    decision_id = make_id("dec")
    actor = f"{ACTOR_PREFIX}{found.agent}"
    rec = {**dict(recommendation), "autonomy": found.record()}
    connection.execute(text(
        "INSERT INTO decisions (id, project_id, task_id, question, recommendation, options, "
        "risk_tier, cost_summary, deadline_at, default_action, status, approved_by, approved_at, "
        "resolution_note) VALUES (:id, :p, :t, :q, CAST(:r AS JSONB), '[]'::jsonb, 2, "
        "'{}'::jsonb, NULL, 'hold', 'approved', :a, :at, :n)"),
        {"id": decision_id, "p": project_id, "t": task_id, "q": question[:500],
         "r": json.dumps(rec, default=str), "a": actor, "at": now_utc(),
         "n": f"{decided_words(found.agent)}: {found.why}"[:900]})
    emit_event(connection, project_id, EVENT, actor, "decision", decision_id,
               {"agent": found.agent, "action": rec.get("action") or rec.get("capability"),
                "class": found.cls, "policy": found.policy, "verdict": found.verdict,
                "evidence": found.why, "undo_kind": found.undo, "no_card": True,
                "words": decided_words(found.agent)})
    return decision_id


def sweep(engine: Any, settings: Any, *, project_id: Optional[str] = None,
          limit: int = 25) -> dict[str, int]:
    """Decide the pending cards dev autonomy answers (the backstop for the immediate thread,
    and the path for cards written by the direct inserts). Bounded per call."""
    from ..db import begin

    with begin(engine) as c:
        rows = c.execute(text(
            "SELECT id FROM decisions WHERE status = 'pending' "
            + ("AND project_id = :p " if project_id else "")
            + "AND (recommendation->'autonomy'->>'route' = 'act' "
              "     OR (recommendation->'autonomy' IS NULL AND created_at > COALESCE("
              "         (SELECT applied_at FROM schema_once WHERE key = 'dev_autonomy_started'),"
              "         now()))) ORDER BY created_at LIMIT :n"),
            {"n": int(limit), **({"p": project_id} if project_id else {})}).scalars().all()
    out = {"decided": 0, "asked": 0}
    for decision_id in rows:
        try:
            with begin(engine) as c:
                result = decide(c, settings, str(decision_id))
            out["decided" if result.get("decided") else "asked"] += 1
        except Exception as error:  # noqa: BLE001 — one card never stops the sweep
            log.warning("autonomy sweep: %s: %s", decision_id, error)
            # Kept for a person, saying why — never retried by every sweep.
            try:
                with begin(engine) as c:
                    c.execute(text(
                        "UPDATE decisions SET recommendation = COALESCE(recommendation, "
                        "'{}'::jsonb) || jsonb_build_object('autonomy', jsonb_build_object("
                        "'route', 'ask', 'class', 'error', 'why', CAST(:w AS TEXT))) "
                        "WHERE id = :d AND status = 'pending'"),
                        {"w": f"BOSS could not decide it: {error}"[:300], "d": decision_id})
            except Exception:  # noqa: BLE001
                pass
    return out


# ---- review and undo afterwards -------------------------------------------------------------------

def decided_for_you(connection: Any, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Every card BOSS decided under dev autonomy, latest first, with its undo."""
    rows = connection.execute(text(
        "SELECT id, question, approved_by, approved_at, recommendation, status, updated_at "
        "FROM decisions WHERE project_id = :p AND approved_by LIKE :pre "
        "ORDER BY approved_at DESC NULLS LAST, updated_at DESC LIMIT :n"),
        {"p": project_id, "pre": ACTOR_PREFIX + "%", "n": max(1, min(int(limit), 200))}
    ).mappings().all()
    out = []
    for r in rows:
        rec = _rec(dict(r))
        auto = rec.get("autonomy") if isinstance(rec.get("autonomy"), Mapping) else {}
        agent = str(r["approved_by"])[len(ACTOR_PREFIX):]
        undo = str(auto.get("undo") or NONE_RECORD)
        at = r["approved_at"]
        out.append({
            "decision_id": r["id"], "question": r["question"], "agent": agent,
            "words": decided_words(agent), "class": auto.get("class"),
            "why": auto.get("why"), "verdict": auto.get("verdict"),
            "action": rec.get("action") or rec.get("capability"),
            "decided_at": at.isoformat() if hasattr(at, "isoformat") else at,
            "undo": None if undo.startswith("none:") else undo,
            "undo_unavailable": undo[5:] if undo.startswith("none:") else None,
            "undone": bool(auto.get("undone")),
            "reviewed": bool(auto.get("human_reviewed")),
            "links": {k: rec[k] for k in ("run_id", "workflow_run_id", "deployment_run_id",
                                          "target_id", "pr_url", "pull_request_url")
                      if rec.get(k)},
        })
    return out


class UndoRefused(ValueError):
    pass


def _auto(connection: Any, decision_id: str, project_id: str) -> tuple[dict, dict]:
    row = connection.execute(text(
        "SELECT id, project_id, approved_by, recommendation FROM decisions "
        "WHERE id = :d AND project_id = :p FOR UPDATE"),
        {"d": decision_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError("no such decision")
    if not str(row["approved_by"] or "").startswith(ACTOR_PREFIX):
        raise UndoRefused("This was not decided under dev autonomy.")
    rec = _rec(dict(row))
    return rec, dict(rec.get("autonomy") or {})


def _mark(connection: Any, decision_id: str, auto: dict, **changes: Any) -> None:
    connection.execute(text(
        "UPDATE decisions SET recommendation = COALESCE(recommendation, '{}'::jsonb) "
        "|| jsonb_build_object('autonomy', CAST(:a AS JSONB)), updated_at = now() WHERE id = :d"),
        {"a": json.dumps({**auto, **changes}, default=str), "d": decision_id})


def mark_reviewed(connection: Any, decision_id: str, *, project_id: str, actor: str) -> dict:
    """"Looks good": a person reviewed what BOSS decided. Nothing else changes."""
    from .core import emit_event, now_utc

    _rec_, auto = _auto(connection, decision_id, project_id)
    _mark(connection, decision_id, auto, human_reviewed=True, reviewed_by=actor,
          reviewed_at=now_utc().isoformat())
    emit_event(connection, project_id, "decision.auto_decision_reviewed", actor, "decision",
               decision_id, {})
    return {"decision_id": decision_id, "reviewed": True}


def _runs_started_by(connection: Any, decision_id: str, rec: Mapping[str, Any]) -> list[str]:
    runs = [str(rec[k]) for k in ("run_id", "code_task_run_id") if rec.get(k)]
    point = connection.begin_nested()
    try:
        found = connection.execute(text(
            "SELECT DISTINCT COALESCE(data->>'run_id', entity_id) FROM events "
            "WHERE (data->>'decision_id' = :d OR entity_id = :d) AND "
            "event_type IN ('code_task.started_from_prd', 'decision.follow_on_job_queued') "
            ), {"d": decision_id}).scalars().all()
    except Exception as error:  # noqa: BLE001 — an events table of another shape: what the card names
        release_failed_savepoint(point, error, connection=connection)
        found = []
    else:
        point.commit()
    return list(dict.fromkeys(runs + [str(r) for r in found if r]))


def _undo_cancel(connection, settings, decision_id, project_id, rec, actor) -> str:
    from .code_task_service import cancel_run

    runs = [r for r in _runs_started_by(connection, decision_id, rec) if r.startswith("crun")]
    if not runs:
        raise UndoRefused("Nothing this decision started is still running to stop.")
    for run_id in runs:
        cancel_run(connection, run_id, project_id, actor=actor,
                   reason=f"undo of {decision_id} (decided under dev autonomy)")
    return f"stopped {', '.join(runs)}"


def _undo_send_back(connection, settings, decision_id, project_id, rec, actor) -> str:
    words = _undo_cancel(connection, settings, decision_id, project_id, rec, actor)
    return words + "; send it back with a note from the run to have it redone"


def _undo_revert_merge(connection, settings, decision_id, project_id, rec, actor) -> str:
    from .code_task_service import start_followup_task
    from .jobs import JobLeaseService

    parent = rec.get("run_id") or rec.get("code_task_run_id")
    if not parent:
        raise UndoRefused("This merge names no build to revert it from.")
    pr = rec.get("pr_url") or rec.get("pull_request_url") or rec.get("pr_number") or "the merge"
    started = start_followup_task(
        connection, parent_run_id=str(parent), jobs=JobLeaseService(settings),
        instructions=(f"Revert {pr} (merged into {rec.get('pr_base') or 'its base'} under dev "
                      "autonomy; a person asked to undo it). Open the revert as its own change."),
        title=f"Revert {pr}", actor=actor, is_person_request=True, new_change=True)
    return f"started revert build {started.get('run_id') or started.get('id') or ''}".strip()


def _undo_rollback(connection, settings, decision_id, project_id, rec, actor) -> str:
    stack_id = rec.get("stack_deployment_id") or rec.get("deployment_id")
    if rec.get("action") == "deploy_stack" and stack_id:
        from .stack_deploy import rollback_stack_deploy

        done = rollback_stack_deploy(connection.engine, settings, deployment_id=str(stack_id),
                                     actor=actor)
        return f"rolling back: {done.get('id') or done.get('deployment_id') or 'planned'}"
    target = rec.get("target_id")
    run_id = rec.get("deployment_run_id")
    if not target and run_id:
        target = connection.execute(text(
            "SELECT deployment_target_id FROM deployment_runs WHERE id = :i"),
            {"i": run_id}).scalar()
    if not target:
        raise UndoRefused("This deploy names no target to roll back.")
    previous = connection.execute(text(
        "SELECT id, commit_ref FROM deployment_runs WHERE deployment_target_id = :t AND "
        "status = 'succeeded' AND id <> COALESCE(:r, '') ORDER BY finished_at DESC NULLS LAST "
        "LIMIT 1"), {"t": target, "r": run_id}).mappings().first()
    if previous is None or not previous["commit_ref"]:
        raise UndoRefused("There is no earlier successful deployment of this app to return to.")
    from .resource_plane import ResourcePlaneService

    planned = ResourcePlaneService(settings).plan_deployment_run(
        connection, project_id=project_id, deployment_target_id=str(target),
        commit_ref=str(previous["commit_ref"]),
        release_packet={"rollback_of": run_id, "undo_of_decision": decision_id},
        expected_cost_usd_micros=0, reservation_id=None, actor=actor)
    return f"redeploying {str(previous['commit_ref'])[:12]} ({planned.get('id')})"


def _undo_revoke(connection, settings, decision_id, project_id, rec, actor) -> str:
    from .tool_access_requests import revoke_grant

    grants = connection.execute(text(
        "SELECT id FROM mcp_access_grants WHERE decision_id = :d AND project_id = :p "
        "AND status = 'active'"), {"d": decision_id, "p": project_id}).scalars().all()
    if not grants:
        raise UndoRefused("No grant from this decision is still active.")
    for grant_id in grants:
        revoke_grant(connection, project_id, str(grant_id), actor=actor)
    return f"revoked {len(grants)} grant(s)"


def _undo_disable_skill(connection, settings, decision_id, project_id, rec, actor) -> str:
    from .skill_registry import disable_skill

    skill = rec.get("skill_id") or rec.get("skill_key")
    if not skill:
        raise UndoRefused("This decision names no skill.")
    disable_skill(connection, project_id, str(skill), actor=actor)
    return f"disabled {skill}"


def _undo_remove_self_fix(connection, settings, decision_id, project_id, rec, actor) -> str:
    from .self_fix_install import request_removal

    removal = request_removal(connection, settings, project_id=project_id, actor=actor,
                              sfx_id=rec.get("sfx_id") or rec.get("self_fix_id"))
    return f"removal asked ({removal.get('decision_id') or 'queued'})"


UNDO_HANDLERS = {
    "send_back": _undo_send_back,
    "revert_merge": _undo_revert_merge,
    "rollback_deploy": _undo_rollback,
    "revoke_grant": _undo_revoke,
    "disable_skill": _undo_disable_skill,
    "remove_self_fix": _undo_remove_self_fix,
    "cancel_run": _undo_cancel,
}


def undo(connection: Any, settings: Any, decision_id: str, *, project_id: str,
         actor: str) -> dict[str, Any]:
    """Undo one auto-decision by its stored undo kind."""
    from .core import emit_event, now_utc

    rec, auto = _auto(connection, decision_id, project_id)
    if auto.get("undone"):
        raise UndoRefused("Already undone.")
    kind = str(auto.get("undo") or NONE_RECORD)
    if kind.startswith("none:"):
        raise UndoRefused(f"No undo: {kind[5:]}.")
    handler = UNDO_HANDLERS.get(kind)
    if handler is None:
        raise UndoRefused(f"No undo handler for {kind}.")
    words = handler(connection, settings, decision_id, project_id, rec, actor)
    _mark(connection, decision_id, auto, undone=True, undone_by=actor,
          undone_at=now_utc().isoformat(), undo_result=words)
    emit_event(connection, project_id, "decision.auto_decision_undone", actor, "decision",
               decision_id, {"undo_kind": kind, "result": words})
    return {"decision_id": decision_id, "undo": kind, "result": words}


def permits(decision: Mapping[str, Any], actor: Any) -> bool:
    """Whether `actor` is dev autonomy answering a card whose route says act (the person-only
    guards — an architecture, a self-fix install, a paid fix — accept it then, and only then)."""
    if not str(actor or "").startswith(ACTOR_PREFIX):
        return False
    auto = _rec(decision).get("autonomy")
    return isinstance(auto, Mapping) and auto.get("route") == "act"


def describe_rule(action: str) -> dict[str, Any]:
    rule = ACTIONS.get(action) or GATES.get(action)
    return asdict(rule) if rule else {}
