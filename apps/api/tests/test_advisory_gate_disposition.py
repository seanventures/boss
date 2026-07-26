"""Regression: advisory quality-gate failures must SHIP the PR, not fall into block.

Commit 8bd22f5 added advisory checks for web-app first-pass builds (review=False):
failing adversarial/security/consistency gates are surfaced but must not block delivery.
The original control flow cleared `failing` then fell through into the human-block path,
so every advisory failure stalled the build as `awaiting_gate_override` with an empty
failing_gates list — the opposite of the intended product behavior.
"""
from apps.api.app.services.governed_code_task_service import resolve_gate_failure_action


def test_advisory_gate_ships_despite_failing_checks():
    # Concrete trigger: web_app_build with risk_tier < 2 → advisory_gate=True;
    # adversarial review fails code_review_passed / security_clean / consistent.
    assert resolve_gate_failure_action(
        failing=["code_review_passed", "security_clean"],
        advisory_gate=True,
        rounds_used=0,
        max_repair_rounds=2,
        open_pr=True,
    ) == "ship"


def test_blocking_gate_repairs_before_block():
    assert resolve_gate_failure_action(
        failing=["code_review_passed"],
        advisory_gate=False,
        rounds_used=0,
        max_repair_rounds=2,
        open_pr=True,
    ) == "repair"


def test_blocking_gate_blocks_after_repair_budget():
    assert resolve_gate_failure_action(
        failing=["code_review_passed"],
        advisory_gate=False,
        rounds_used=2,
        max_repair_rounds=2,
        open_pr=True,
    ) == "block"


def test_empty_failing_ships():
    assert resolve_gate_failure_action(
        failing=[],
        advisory_gate=False,
        rounds_used=0,
        max_repair_rounds=2,
        open_pr=True,
    ) == "ship"


def test_open_pr_false_skips_repair_and_blocks():
    assert resolve_gate_failure_action(
        failing=["written_tests_pass"],
        advisory_gate=False,
        rounds_used=0,
        max_repair_rounds=2,
        open_pr=False,
    ) == "block"
