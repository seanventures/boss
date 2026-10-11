"""A decision offers only the buttons that do something, and the server refuses the rest.

On boss-1 the Approvals queue held a PR review, a `budget_denied` and a
`model_capacity_denied`, and every one of them showed Approve, Reject, Escalate and Request
changes. Approving the two refusals recorded an answer nothing reads; Escalate posted an
action the server never accepted and always failed; and "Request changes" on a workflow gate
parked it in a status the runner does not read, so the run waited for ever. These tests pin the
contract in boss_engine.services.decision_actions for EVERY kind, so a new kind cannot arrive
without deciding its buttons.
"""
from __future__ import annotations

import pytest

from boss_engine.services import decision_actions as da
from boss_engine.services.decisions import DecisionService

#: One real-shaped decision per kind, as the code that raises each one writes it.
SAMPLES = {
    "refusal": {"status": "pending", "default_action": "hold",
                "recommendation": {"action": "model_capacity_denied",
                                   "reason_code": "model_capacity_denied"}},
    "deploy_gate": {"status": "pending", "recommendation": {
        "workflow_run_id": "wfr_1", "capability": "deploy", "step_id": "deploy"}},
    "workflow_gate": {"status": "pending", "recommendation": {
        "workflow_run_id": "wfr_1", "capability": "spec_review", "step_id": "spec"}},
    "architecture_review": {"status": "pending", "recommendation": {
        "action": "review_architecture", "workflow_run_id": "wfr_1", "step_id": "arch",
        "capability": "architecture", "architecture_review_id": "arch_1", "version": 1}},
    "pull_request": {"status": "pending", "recommendation": {
        "action": "review_pull_request", "pull_request_url": "https://x/pull/7"}},
    "change_set": {"status": "pending", "recommendation": {
        "action": "approve_change_set", "change_set_id": "chs_1",
        "pull_requests": [{"repository": "acme/api", "pr_number": 1}, {"repository": "acme/web", "pr_number": 2}]}},
    "general": {"status": "pending", "recommendation": {"action": "role.autonomy_change"}},
}


def test_every_kind_has_a_sample_so_none_is_left_undecided():
    assert set(SAMPLES) == set(da.KINDS)


@pytest.mark.parametrize("kind", da.KINDS)
def test_each_sample_is_classified_as_its_kind(kind):
    assert da.kind_of(SAMPLES[kind]) == kind


@pytest.mark.parametrize("kind", da.KINDS)
def test_every_offered_action_is_one_the_transition_accepts(kind):
    """A button whose action the server rejects is the Escalate bug in another shape."""
    supported = {"approve", "reject", "revise", "delegate", "execute"}
    for offered in da.allowed_actions(SAMPLES[kind]):
        assert offered["action"] in supported
        assert offered["label"] and offered["effect"]
        da.require_allowed(SAMPLES[kind], offered["action"])


@pytest.mark.parametrize("reason", sorted(da.REFUSAL_REASONS))
def test_a_spending_refusal_is_a_notice_with_one_dismiss_and_a_way_to_fix_it(reason):
    # REWRITTEN: this asserted "Retry" for every refusal when there were three. The autoscaler
    # and the budget-deferred job are refusals too, but nothing failed: the work runs slower or
    # later by itself, so the honest instruction is "nothing is needed", not "retry".
    card = {"status": "pending", "recommendation": {"action": reason}}
    assert [a["action"] for a in da.allowed_actions(card)] == ["reject"]
    assert da.allowed_actions(card)[0]["label"] == "Dismiss"
    hint = da.guidance(card)
    assert hint and hint["area"] in {"budget", "setup"}
    assert "Approving this card would not" in hint["what_to_do"]
    if da.REFUSALS[reason] in {"capacity", "budget", "tier2_frozen"}:
        assert "Retry" in hint["what_to_do"]
    else:
        assert "Nothing is needed" in hint["what_to_do"]


def test_a_factory_build_out_of_model_capacity_is_a_notice_pointing_at_repo_and_model():
    """The refusal BOSS raises most on a real install, exactly as governed_code_task_service
    writes it: the provider's code as reason_code, BOSS's own as reason. It was `general`, so
    it offered Approve ("Carries out what this card proposes") and topped the Command Center."""
    card = {"status": "pending", "recommendation": {
        "action": "restore_model_capacity_or_raise_budget", "run_id": "ctr_1",
        "reason_code": "model_rate_limit", "reason": "model_capacity_exhausted",
        "failure_class": "needs_human"}}
    assert da.kind_of(card) == "refusal"
    assert [a["label"] for a in da.allowed_actions(card)] == ["Dismiss"]
    assert da.guidance(card)["area"] == "setup"
    assert "Repo and model" in da.guidance(card)["what_to_do"]


def test_a_refusal_is_recognised_by_its_reason_when_the_action_is_something_else():
    """Every reason field is read, not only the first one present."""
    card = {"status": "pending", "recommendation": {
        "action": "something_new", "reason_code": "model_rate_limit",
        "reason": "model_capacity_exhausted"}}
    assert da.kind_of(card) == "refusal"


@pytest.mark.parametrize("action, family", [
    ("raise_cap_or_wait_for_headroom", "tier2_frozen"),
    ("increase_budget_or_defer_work", "autoscale"),
    ("approve_budget_increase_or_accept_degraded_mode", "autoscale"),
    ("review_budget_policy", "deferred"),
])
def test_the_budget_governor_and_autoscaler_refusals_are_notices(action, family):
    """budget.py's tier-2 freeze, the autoscaler and the deferred job: none can be approved
    into happening, so none may offer Approve."""
    card = {"status": "pending", "recommendation": {"action": action}}
    assert da.kind_of(card) == "refusal"
    assert da.guidance(card) == da._REFUSAL_GUIDANCE[family]


def test_a_pull_request_cannot_be_sent_back_for_changes_nothing_makes():
    """'Request changes' on a PR parked it in revision_requested, which nothing reads and
    which refused a later approval: the PR could never be merged from BOSS."""
    offered = {a["action"] for a in da.allowed_actions(SAMPLES["pull_request"])}
    assert "revise" not in offered
    with pytest.raises(da.DecisionActionNotAllowed) as refused:
        da.require_allowed(SAMPLES["pull_request"], "revise")
    assert "Factory" in str(refused.value)
    keep = next(a for a in da.allowed_actions(SAMPLES["pull_request"]) if a["action"] == "reject")
    assert "Add a feature" in keep["effect"]


def test_a_decision_stuck_in_a_dead_end_revision_can_be_answered_again():
    """Rows parked that way before this contract exist on real installs; they reopen."""
    stuck = {**SAMPLES["pull_request"], "status": "revision_requested"}
    assert {"approve", "reject"} <= {a["action"] for a in da.allowed_actions(stuck)}
    read = {"status": "revision_requested",
            "recommendation": {"action": "role.autonomy_change"}}
    assert da.allowed_actions(read) == []


def test_approve_on_a_card_nothing_acts_on_does_not_claim_to_carry_it_out():
    card = {"status": "pending", "recommendation": {"action": "provide_github_token"}}
    approve = next(a for a in da.allowed_actions(card) if a["action"] == "approve")
    assert "starts nothing" in approve["effect"]
    assert "revise" not in {a["action"] for a in da.allowed_actions(card)}


def test_a_workflows_own_decision_step_is_a_workflow_gate():
    """workflows.py writes `resume_workflow` and no run id; as `general` it offered
    Request changes, which leaves the run waiting for ever."""
    card = {"status": "pending", "recommendation": {"action": "resume_workflow"}}
    assert da.kind_of(card) == "workflow_gate"
    assert "revise" not in {a["action"] for a in da.allowed_actions(card)}


def test_the_budget_governors_own_refusal_shape_is_a_refusal_too():
    """budget.py writes `increase_budget_or_defer` with the reason attached separately."""
    card = {"status": "pending", "recommendation": {
        "action": "increase_budget_or_defer", "reason_code": "budget_denied"}}
    assert da.kind_of(card) == "refusal"
    assert da.guidance(card)["area"] == "budget"


def test_approving_a_refusal_is_refused_and_says_what_to_do_instead():
    with pytest.raises(da.DecisionActionNotAllowed) as refused:
        da.require_allowed(SAMPLES["refusal"], "approve")
    assert "Repo and model" in str(refused.value)
    assert "Dismiss" in str(refused.value)


def test_a_workflow_gate_cannot_be_sent_back_for_changes_the_runner_never_reads():
    for kind in ("workflow_gate", "deploy_gate"):
        assert "revise" not in {a["action"] for a in da.allowed_actions(SAMPLES[kind])}
        with pytest.raises(da.DecisionActionNotAllowed):
            da.require_allowed(SAMPLES[kind], "revise")


def test_hand_to_someone_else_is_delegation_and_asks_who():
    """It was 'Escalate', posting `escalate` with an `escalate_to` field nothing read."""
    for kind in ("deploy_gate", "workflow_gate", "pull_request", "general"):
        delegate = [a for a in da.allowed_actions(SAMPLES[kind]) if a["action"] == "delegate"]
        assert delegate and delegate[0]["needs"] == "delegate_to"


def test_a_closed_decision_offers_nothing_and_an_approved_one_only_mark_executed():
    assert da.allowed_actions({**SAMPLES["general"], "status": "rejected"}) == []
    assert [a["action"] for a in da.allowed_actions({**SAMPLES["general"],
                                                     "status": "approved"})] == ["execute"]


def test_an_expired_hold_decision_can_still_be_answered():
    """transition() reopens an expired hold-default decision; the buttons must agree."""
    card = {**SAMPLES["deploy_gate"], "status": "expired", "default_action": "hold"}
    assert "approve" in {a["action"] for a in da.allowed_actions(card)}


def test_the_transition_refuses_an_action_the_card_does_not_offer(monkeypatch):
    """The server, not only the console, holds the line — MCP and the chat agent call this."""
    from types import SimpleNamespace

    service = object.__new__(DecisionService)
    # The member check runs first (who may decide at all); this actor passes it.
    service.members = SimpleNamespace(require_decision_actor=lambda *_a, **_k: None)
    service.get_row = lambda *_a, **_k: {**SAMPLES["refusal"], "id": "dec_1",
                                         "project_id": "p", "risk_tier": 1,
                                         "deadline_at": None}
    from boss_engine.services.decisions import InvalidDecisionTransitionError
    with pytest.raises(InvalidDecisionTransitionError) as refused:
        service.transition(object(), "dec_1", action="approve", actor="ceo", note=None)
    assert "notice" in str(refused.value)
