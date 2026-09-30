"""A failed preview must be classified, remedied, and acted on — not left spinning.

Five consecutive preview failures on one run produced no classification, no remedy and no
ledger entry. The console rendered them as the pending label "Starting preview…", so the
operator reported the preview as hung and there was nothing autonomous behind it.

Two defects, one symptom:
  1. task_shepherd had no failure class for a preview at all.
  2. The remedy executor implemented only retry_code_task/extend_code_task. Every other
     action — including the delivery classes that already shipped (deploy, health check,
     canary) — fell through to "no_action", so the shepherd recorded a decision and did
     nothing about it.
"""

import inspect

import pytest

from boss_engine.services import shepherd_service
from boss_engine.services.task_shepherd import (
    CLASS_NAMES,
    DEPLOY_FAILED,
    HEALTH_CHECK_FAILED,
    INFRASTRUCTURE,
    PREVIEW_FAILED,
    REMEDIES,
    classify,
)

# Verbatim from shared-server — prev_3bcf128c3a3a4d128c95, one of five identical failures.
REAL_FAILURE = (
    "app did not serve HTTP within timeout. log tail: tmp/boss-previews/"
    "prev_3bcf128c3a3a4d128c95-x8685xbv/node_modules/rollup/dist/es/shared/parseAst.js:313:42)"
    "\n    at ModuleLoader.loadEntryModule (file:///tmp/boss-previews/..."
)


def test_the_real_failure_classifies_as_a_preview_failure():
    assert classify(REAL_FAILURE) == PREVIEW_FAILED
    assert CLASS_NAMES[PREVIEW_FAILED] == "preview failed to start"


@pytest.mark.parametrize("message", [
    "app did not serve HTTP within timeout",
    "Could not resolve entry module 'index.html'",
    "preview failed to start",
])
def test_preview_failure_signatures(message):
    assert classify(message) == PREVIEW_FAILED


def test_not_filed_as_infrastructure_which_would_prescribe_a_blind_retry():
    """A build that cannot succeed must not be retried as though it were a network blip."""
    assert classify(REAL_FAILURE) != INFRASTRUCTURE


def test_remedies_try_the_free_retry_before_spending_on_a_fix():
    order = [r.id for r in REMEDIES[PREVIEW_FAILED]]
    assert order == ["retry_preview", "open_preview_fix_task", "escalate"]
    first = REMEDIES[PREVIEW_FAILED][0]
    assert first.prior_cost_usd == 0.0, "a retry that costs nothing must come first"
    assert first.action == "restart_preview"


def test_the_fix_task_remedy_warns_against_inventing_a_build_system():
    """The failure mode that produced this bug: the agent bolted Vite onto a non-Vite repo."""
    fix = next(r for r in REMEDIES[PREVIEW_FAILED] if r.id == "open_preview_fix_task")
    assert fix.action == "create_code_task"
    assert "invent" in fix.describe.lower()


def test_every_declared_remedy_action_is_implemented_by_the_executor():
    """The core defect: actions were declared in the ledger and unhandled in the executor.

    Guards the delivery classes too — deploy/health/canary all named actions that silently
    did nothing.
    """
    source = inspect.getsource(shepherd_service)
    declared = {
        r.action
        for remedies in REMEDIES.values()
        for r in remedies
        if r.action
    }
    unhandled = sorted(a for a in declared if f'"{a}"' not in source)
    assert not unhandled, f"remedy actions with no executor branch: {unhandled}"


@pytest.mark.parametrize("failure_class", [PREVIEW_FAILED, HEALTH_CHECK_FAILED, DEPLOY_FAILED])
def test_delivery_classes_end_in_an_action_not_only_an_escalation(failure_class):
    actions = [r.action for r in REMEDIES[failure_class] if r.action]
    assert actions, f"{CLASS_NAMES[failure_class]} can only escalate — nothing is autonomous"


def test_fix_task_carries_the_evidence_and_the_no_new_build_system_constraint():
    source = inspect.getsource(shepherd_service._open_fix_task)
    assert "EVIDENCE" in source, "a repair agent told only 'it does not work' guesses"
    assert "build system" in source
    assert "acceptance_criteria" in source


def test_the_shepherd_has_a_caller():
    """The defect under everything: act() existed and nothing in the codebase invoked it.

    sweep() and propose() only WROTE proposals, so BOSS could diagnose every failure in the
    project and never fix one. The remedy table, cost estimates and the confirmation gate were
    all already built — only the loop was missing.
    """
    source = inspect.getsource(shepherd_service)
    autopilot_src = inspect.getsource(shepherd_service.autopilot)
    assert "act(engine, settings, jobs, run_id" in autopilot_src, "autopilot must call act()"
    assert "confirm_needed" in autopilot_src, "the operator gate must still be honoured"
    assert "app_previews" in autopilot_src, (
        "sweep() only sees failed/blocked runs with no PR; a preview failure happens on a run "
        "that SUCCEEDED, so autopilot must look for those separately")
    assert "def autopilot" in source


def test_autopilot_defers_rather_than_spends_without_a_human():
    src = inspect.getsource(shepherd_service.autopilot)
    defer = src.index('proposal.get("needs_confirmation")')
    action = src.index("act(engine, settings, jobs, run_id")
    assert defer < action, "the confirmation check must come before acting"


def test_autopilot_reads_the_key_propose_actually_returns():
    """`propose()` returns `needs_confirmation`; reading `confirm_needed` off the dict silently
    yields None, so the operator gate would never fire and spend-incurring remedies would run
    unattended. A gate that cannot say no is not a gate."""
    src = inspect.getsource(shepherd_service.autopilot)
    assert 'proposal.get("needs_confirmation")' in src
    assert 'proposal.get("confirm_needed")' not in src

    import inspect as _i
    propose_src = _i.getsource(shepherd_service.propose)
    assert '"needs_confirmation"' in propose_src
    assert '"remedy_action"' in propose_src, "autopilot needs the action to tell repair from question"


def test_escalate_is_not_counted_as_a_repair():
    """`escalate` is the remedy whose content is 'ask a human'. Running it through act()
    reported `applied` with a null outcome — a summary claiming repairs that were questions."""
    src = inspect.getsource(shepherd_service.autopilot)
    assert 'if not proposal.get("remedy_action"):' in src
    assert "escalated.append" in src
    assert '"escalated": escalated' in src


def test_autopilot_will_not_starve_the_queue_it_shares():
    """The first live run fired six remedies into a worker that drains one job at a time.

    Those six code-task retries went in front of the operator's PRD meeting and pushed it hours
    out — repair work crowding out the interactive work it exists to protect — and every 180s
    tick added more, so the backlog grew faster than it drained.
    """
    from boss_engine.services.shepherd_service import (
        MAX_ACTIONS_PER_TICK, QUEUE_DEPTH_PAUSE,
    )
    assert 1 <= MAX_ACTIONS_PER_TICK <= 3, "a tick that starts six repairs is a stampede"
    assert QUEUE_DEPTH_PAUSE >= 1

    src = inspect.getsource(shepherd_service.autopilot)
    assert "paused_because" in src, "a paused tick must say WHY, not look like nothing to do"
    assert "queue_depth" in src
    assert "MAX_ACTIONS_PER_TICK" in src
    # The depth check must happen before any acting, not after some repairs are already queued.
    assert src.index("queue_depth") < src.index("act(engine, settings, jobs, run_id")
