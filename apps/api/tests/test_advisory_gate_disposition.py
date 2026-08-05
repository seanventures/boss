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
    """Opt-in fail-closed: with never_block=False the exhausted gate still parks for a human."""
    assert resolve_gate_failure_action(
        failing=["code_review_passed"],
        advisory_gate=False,
        rounds_used=2,
        max_repair_rounds=2,
        open_pr=True,
        never_block=False,
    ) == "block"


def test_exhausted_gate_ships_with_dissent_by_default():
    """Autonomy default: exhausting the repair budget ships with the dissent recorded, never halts."""
    assert resolve_gate_failure_action(
        failing=["code_review_passed"],
        advisory_gate=False,
        rounds_used=2,
        max_repair_rounds=2,
        open_pr=True,
    ) == "ship_with_dissent"


def test_no_pr_run_still_blocks_rather_than_shipping_nothing():
    """open_pr=False runs measure the gate, not delivery — there is nothing to ship dissent into."""
    assert resolve_gate_failure_action(
        failing=["code_review_passed"],
        advisory_gate=False,
        rounds_used=2,
        max_repair_rounds=2,
        open_pr=False,
    ) == "block"


def test_nonfinal_decomposition_slice_must_pass_open_pr_false():
    """Non-final slices suppress the PR and advance the chain on completed_no_pr.

    The call site must pass open_pr=False into resolve_gate_failure_action for those
    slices — otherwise ship_with_dissent marks broken work completed and the next slice
    builds on unresolved security/test failures with no follow-up task.
    """
    assert resolve_gate_failure_action(
        failing=["security_clean", "written_tests_pass"],
        advisory_gate=False,
        rounds_used=2,
        max_repair_rounds=2,
        open_pr=False,  # call site: not _can_ship_pr for non-final slices
        never_block=True,
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
