"""The governed-pipeline doc must describe the pipeline that actually ships.

`GOVERNED_CODING_PIPELINE.md` is the canonical customer-facing description of BOSS's
differentiator, and it said "final gate · FAILS CLOSED" and "nothing merges without a
passing gate" in bold, present tense, while `BOSS_CODE_GATE_NEVER_BLOCK` shipped `true`
and the runtime shipped with dissent recorded instead.

Both halves were defensible on their own. The default is a deliberate autonomy choice with
the dissent recorded, re-queued and reversible — a reasonable product decision. The doc was
describing a different product. An evaluator running stock config would have met the second
one and concluded BOSS overclaims, which is the single thing `COMMUNITY_READINESS.md` says
must not happen before a public release.

This test pins the two together: change a default and the doc fails until it is updated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.config import Settings

DOC = Path("docs/product/GOVERNED_CODING_PIPELINE.md")


@pytest.fixture(scope="module")
def doc() -> str:
    if not DOC.exists():  # pragma: no cover - engine installed standalone
        pytest.skip(f"{DOC} not present in this checkout")
    return DOC.read_text()


def test_the_gate_default_is_what_the_doc_says_it_is(doc: str) -> None:
    settings = Settings()
    if settings.code_gate_never_block:
        assert "ships with the dissent recorded" in doc, (
            "BOSS_CODE_GATE_NEVER_BLOCK defaults to true, so the doc must not promise a "
            "gate that fails closed"
        )
        assert "FAILS CLOSED" not in doc
        assert "nothing merges without a passing gate" not in doc
    else:
        assert "fail-closed" in doc or "FAILS CLOSED" in doc


def test_the_doc_names_the_flag_so_the_default_can_be_changed(doc: str) -> None:
    assert "BOSS_CODE_GATE_NEVER_BLOCK" in doc


def test_the_doc_states_the_panel_size_default(doc: str) -> None:
    """A 'refute panel' of one reviewer is a single reviewer. Say so."""
    size = Settings().code_review_panel_size
    assert "BOSS_CODE_REVIEW_PANEL_SIZE" in doc
    assert f"defaults to **{size}**" in doc


def test_the_two_hard_floors_are_documented_as_still_blocking(doc: str) -> None:
    """Advisory mode is not 'ignore the gates'. A regression and an empty diff are facts,
    not opinions, and still stop the run — that distinction is the reason the default is
    defensible at all, so it has to be in the doc."""
    assert "regression" in doc and "empty diff" in doc


def test_an_empty_diff_still_blocks_in_advisory_mode() -> None:
    """Advisory means a reviewer's OPINION cannot withhold the PR. "No code was produced"
    is not an opinion."""
    from boss_engine.services.governed_code_task_service import (
        resolve_gate_failure_action,
    )

    assert resolve_gate_failure_action(
        failing=["code_written"], advisory_gate=True, rounds_used=0,
        max_repair_rounds=3, never_block=True) == "repair"


@pytest.mark.xfail(
    reason="BOSS-419: _ADVISORY_HARD_FLOOR is ('code_written',) only. "
           "resolve_gate_failure_action's own docstring says TWO failures are facts rather "
           "than opinions and still get the full repair treatment — 'the change broke tests "
           "that passed before it (a regression), or no code was produced at all' — but the "
           "regression half is not in the tuple, so in advisory mode a change that breaks "
           "previously-passing tests SHIPS. Left failing deliberately: adding a blocking "
           "condition changes delivery behaviour and is the operator's call, not a silent fix.",
    strict=True,
)
def test_a_regression_should_also_block_in_advisory_mode() -> None:
    from boss_engine.services.governed_code_task_service import (
        resolve_gate_failure_action,
    )

    assert resolve_gate_failure_action(
        failing=["existing_tests_intact"], advisory_gate=True, rounds_used=0,
        max_repair_rounds=3, never_block=True) == "repair"


def test_fail_closed_is_reachable_and_really_blocks() -> None:
    from boss_engine.services.governed_code_task_service import (
        resolve_gate_failure_action,
    )

    assert resolve_gate_failure_action(
        failing=["review_code"], advisory_gate=False, rounds_used=3,
        max_repair_rounds=3, never_block=False) == "block"
    assert resolve_gate_failure_action(
        failing=["review_code"], advisory_gate=False, rounds_used=3,
        max_repair_rounds=3, never_block=True) == "ship_with_dissent"
