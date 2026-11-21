"""EVERY WAY BOSS HANDS A FAILURE TO A PERSON, AND WHY (operator, 2026-11-21).

"we are selling the product mainly for Agents to work autonomously by fixing the issue, if you
are involving humans for these actions this is not a autonomous software factory".

This is the inventory. Each exit is one place BOSS stops and asks a person about a failure:
  * `agent`: True — BOSS's agents now do it themselves with the starter's authority (the org
    default, identity/fix_authority.py); the exit is reached only when that default cannot
    apply (turned off, revoked by the person, another org), and its card says so with exact
    steps. False — a TRUE human exit: `why` is the hard limit or the judgement it needs.
  * `where`: the module and function (or failure_owner `exit=` id) it lives in.

engine/tests/test_needs_person_exits_are_justified.py reads the code (every `NeedsPerson(…,
exit=…)` and `_hand_over(…, exit=…)` in failure_owner, every person-cause rule, every
ASK_PERSON-returning function in step_recovery, every `open_question` caller in the Shepherd,
every merge card reopened for a person) and fails on one not listed here — so a new way to hand
work to a person is never added silently. Every card a person gets carries exact guidance
(failure_guidance: what failed, why, the steps, what BOSS does after).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Exit:
    where: str
    agent: bool
    why: str


_CRED = "it needs a person to sign in or type a credential, which no agent may do"
E = Exit

EXITS: dict[str, Exit] = {
    # ── failure_owner: NeedsPerson(…, exit=…) ──────────────────────────────────────────────
    "surface_has_no_retry": E("failure_owner.Surface.retry", False,
                              "the surface (a GitHub connection BOSS found broken) has no "
                              "automatic retry: reconnecting it is " + _CRED),
    "surface_has_no_person_retry": E("failure_owner.Surface.person_retry", False,
                                     "no generic re-run exists; the card says where to start it"),
    "build_retry_withheld": E("failure_owner.CodeTasks.retry", True,
                              "an ended or 'this build only' grant no longer stops a retry "
                              "(consent.carry_retry_authority → fix_authority), nor does a "
                              "restart in between (after_update)"),
    "build_retry_refused": E("failure_owner.CodeTasks.retry", False,
                             "the build refuses the retry in its own words (its PR is already "
                             "merged, another run owns the branch): a retry would duplicate work"),
    "workflow_failed_before_restart": E("failure_owner.WorkflowRuns.retry", True,
                                        "re-run by the agent under the org default"),
    "workflow_authority_not_renewable": E("failure_owner.WorkflowRuns.retry", False,
                                          "the person revoked the authority, the default is "
                                          "off, or the run is in another org: hard limits"),
    "contract_repair_refused": E("failure_owner.WorkflowRuns._remedy", False,
                                 "the repair build refused to start, in its own words"),
    "workflow_step_unknown": E("failure_owner.WorkflowRuns._retry", False,
                               "BOSS cannot tell which step failed, so there is nothing to retry"),
    "workflow_retry_refused": E("failure_owner.WorkflowRuns._retry", False,
                                "the run's own actions refuse a retry (a person stopped it, a "
                                "test run): that stop is a person's"),
    "workflow_retry_consent": E("failure_owner.WorkflowRuns._retry", True,
                                "the run moves onto its person's fixer authority first "
                                "(fix_authority.renew_run); consent is asked only of a person "
                                "pressing retry themselves"),
    "deploy_owned_by_its_run": E("failure_owner.Deployments.retry", True,
                                 "its workflow run's owner (step recovery / this loop) retries it"),
    "production_host_deploy": E("failure_owner.Deployments.retry", False,
                                "a PRODUCTION deploy waits for a person when the org requires "
                                "it (hard limit); non-production ones are re-planned by the agent"),
    "redeploy_needs_signed_in_person": E("failure_owner.require_deploy_permission", False,
                                         "the person pressing Done — retry must be signed in"),
    "redeploy_not_permitted": E("failure_owner.require_deploy_permission", False,
                                "the person pressing Done — retry may not deploy here"),
    "job_ran_for_a_person": E("failure_owner.Jobs.retry", True,
                              "re-queued as exactly its person (deadletters for_its_person)"),
    "job_never_rerun_by_an_agent": E("failure_owner.Jobs.retry", False,
                                     "its effect is one no agent may cause or cannot be undone "
                                     "(a merge, a sent message): fix_authority.may_rerun_job"),
    "fidelity_spent": E("failure_owner._own", False,
                        "the input-fidelity repair is capped at 2 paid builds (#262); the card "
                        "names what was violated and the evidence"),
    "job_requeue_refused":E("failure_owner.Jobs._requeue", False,
                             "the dead letter refuses a re-run in its own words"),
    "merge_conflict_not_handed_off": E("failure_owner.Jobs._merge_conflict", False,
                                       "the build agent could not be started (no build found, "
                                       "or its retry refused); the card gives the exact steps"),
    "held_proof_unnamed": E("failure_owner.ConnectionHolds.retry", False,
                            "the hold names no receipt, so BOSS cannot tell which it is"),
    "held_proof_not_settled": E("failure_owner.ConnectionHolds.retry", False,
                                "settlement refused in its own words"),
    "panel_gone": E("failure_owner._reopen_panel", False, "nothing is left to re-run"),
    "panel_finished": E("failure_owner._reopen_panel", False, "nothing is left to re-run"),
    "panel_superseded": E("failure_owner._reopen_panel", False,
                          "the workflow went on without the panel; a new one is new work"),
    "panel_unknown_state": E("failure_owner._reopen_panel", False,
                             "the panel is in a state BOSS cannot resume from"),
    "panel_not_failed": E("failure_owner.Meetings._resume", False, "nothing is failed any more"),
    "person_retry_refused": E("failure_owner.on_answered", False,
                              "the person's own Done — retry could not run; it goes back to them"),
    # ── failure_owner: _hand_over(…, exit=…) ───────────────────────────────────────────────
    "old_work": E("failure_owner._own", False,
                  "work that failed more than ACT_HOURS ago is not re-run unasked: what it was "
                  "for may have changed (review #232); one click re-runs it"),
    "attempts_spent": E("failure_owner._own", False,
                        "BOSS's own attempts are spent (2 blind retries, 3 real fixes) and it "
                        "failed the same way each time; BOSS's side is recorded as its own bug"),
    # ── failure_owner: person-side causes (person_cause:<code>) ────────────────────────────
    "person_cause:budget": E("failure_owner._PERSON_RULES", False,
                             "spending above the org's budget is a hard limit"),
    "person_cause:no_repository": E("failure_owner._PERSON_RULES", False,
                                    "connecting a repository or installing the GitHub App: "
                                    + _CRED),
    "person_cause:repo_access": E("failure_owner._PERSON_RULES", False,
                                  "only the repository's owner can give BOSS's account access"),
    "person_cause:toolchain": E("failure_owner._PERSON_RULES", False,
                                "it changes the BOSS host (host:exec is never an agent's)"),
    "person_cause:registry_access": E("failure_owner._PERSON_RULES", False,
                                      "the organisation's registry credential: " + _CRED),
    "person_cause:connector_login": E("failure_owner._PERSON_RULES", False, _CRED),
    "person_cause:model_login": E("failure_owner._PERSON_RULES", False, _CRED),
    "person_cause:grant": E("failure_owner._PERSON_RULES", True,
                            "AGENT_REMEDIES: the agent continues with the starter's authority; "
                            "a card only when the org default is off or revoked"),
    "person_cause:approval_expired": E("failure_owner._PERSON_RULES", False,
                                       "an approval is a person's answer; agents never approve "
                                       "(IDENTITY.md §7.13)"),
    "person_cause:merge_conflict": E("failure_owner._PERSON_RULES", True,
                                     "AGENT_REMEDIES: handed to the build agent (keep both sides, "
                                     "test, re-ask merge)"),
    "person_cause:host": E("failure_owner._PERSON_RULES", False,
                           "host access, sudo and disk are the host owner's (host:exec)"),
    "person_cause:needs_human": E("failure_owner.classify", False,
                                  "the work itself says it needs an input only a person has"),
    "person_cause:asked_by_*": E("failure_owner.WorkflowRuns.classify", False,
                                 "the step's recovery agent read the evidence and asked ONE "
                                 "question; an authority-shaped ask is the grant remedy instead"),
    # ── step_recovery: functions that return ASK_PERSON ─────────────────────────────────────
    "step_recovery.coded_decision": E("step_recovery.coded_decision", False,
                                      "a port held by another owner's container, an egress "
                                      "rules 403 (the org's policy), or an ended authority the "
                                      "default could not renew"),
    "step_recovery.no_model_ask": E("step_recovery.no_model_ask", False,
                                    "no model is connected or it is out of credit: " + _CRED),
    "step_recovery.unsure_ask": E("step_recovery.unsure_ask", False,
                                  "the agent is below its confidence bar: it asks before acting"),
    "step_recovery.waiver_ask": E("step_recovery.waiver_ask", False,
                                  "waiving review findings is never an agent's (non-delegable)"),
    "step_recovery._ask": E("step_recovery._ask", False,
                            "a secret only the person has, or the same cause came back after "
                            "the same action"),
    "step_recovery.record_ask": E("step_recovery.record_ask", False,
                                  "the chosen action hit a hard ceiling (budget, attempts)"),
    # ── shepherd_service: open_question callers (legacy diagnosis; failure_owner owns) ──────
    "shepherd._escalate_exhausted_lineage": E("shepherd_service", False,
                                              "legacy autopilot (off by default); failure_owner "
                                              "owns the failure"),
    "shepherd._hand_to_person": E("shepherd_service", False, "legacy autopilot (off by default)"),
    "shepherd.autopilot": E("shepherd_service", False, "legacy autopilot (off by default)"),
    "shepherd.escalate_no_repo_access": E("shepherd_service", False,
                                          "repository access: " + _CRED),
    "shepherd.propose": E("shepherd_service", False,
                          "a costed remedy BOSS could not confirm by its own check"),
    "shepherd.watch": E("shepherd_service", False, "legacy watch (off by default)"),
    # ── merge_failures: a merge card reopened for a person ──────────────────────────────────
    "merge.handle": E("merge_failures.handle", False,
                      "a refused permission or protected branch; a conflict goes to the build "
                      "agent first and comes here only when it cannot start"),
    "merge.recover_orphaned_conflicts": E("merge_failures.recover_orphaned_conflicts", False,
                                          "an orphaned conflict whose build agent could not "
                                          "start is reopened for the person with exact steps"),
    "merge.on_job_deadlettered":E("merge_failures.on_job_deadlettered", False,
                                   "merging is a person's approval (never an agent's)"),
    # ── connection proofs held for reconciliation ───────────────────────────────────────────
    "connection_reconciliation": E("failure_owner.ConnectionHolds", True,
                                   "the agent settles the hold itself (connection_spend."
                                   "agent_settle): $0 when the provider served nothing"),
}


def counts() -> dict[str, int]:
    return {"agent": sum(1 for e in EXITS.values() if e.agent),
            "human": sum(1 for e in EXITS.values() if not e.agent)}


__all__ = ["EXITS", "Exit", "counts"]
