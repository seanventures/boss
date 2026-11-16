"""The brief's stack is kept. A PRD that swaps it is a blocking question (BOSSAPP-1208).

Northgate asked for Go. The panel wrote a Node PRD and the build shipped Node, because the
prompt told the panel to pick the closest stack this install could compile. A missing toolchain
is the question — enable it, accept the other stack, or stop — and the PRD still carries the
line `declared_stack` reads.
"""
from __future__ import annotations

import pytest

from boss_engine.services import decision_actions, stacks
from boss_engine.services import workflow_capability_runner as runner

GO_BRIEF = "Build the API gateway in Go."
NODE_PRD = "# PRD\n\nStack: backend=node/fastify\n"
GO_PRD = "# PRD\n\nStack: app=go\n"


def test_the_prompt_keeps_the_briefs_stack_as_the_blocking_open_question():
    block = stacks.prompt_block()
    assert "WHAT BOSS CAN BUILD" in block
    assert "Not supported at all" in block
    lowered = block.lower()
    assert "blocking open question" in lowered
    assert "closest supported" not in lowered
    assert "stack:" in lowered
    assert "brief" in lowered


def test_a_node_declaration_deviates_from_a_go_brief():
    deviation = stacks.check_deviation(GO_BRIEF, NODE_PRD)
    assert deviation["required"] == ["go"]
    assert deviation["declared"] == ["node"]
    assert deviation["missing"] == ["go"]
    assert deviation["substituted"] == ["node"]
    assert stacks.check_deviation(GO_BRIEF, GO_PRD) is None
    assert stacks.check_deviation(GO_BRIEF, "# PRD\n\nNo stack line.\n") is None
    pinned = stacks.check_deviation(
        [{"component": "northgate-gateway", "language": "go"}], NODE_PRD)
    assert pinned["missing"] == ["go"] and pinned["substituted"] == ["node"]


def test_the_prd_carries_a_stack_line_declared_stack_parses():
    written = stacks.ensure_stack_line("# PRD\n\nSummary\n", GO_BRIEF)
    assert stacks.declared_stack(written) == [
        {"service": "app", "language": "go", "framework": ""}]
    assert stacks.ensure_stack_line(NODE_PRD, GO_BRIEF) == NODE_PRD


def test_a_node_prd_for_a_go_brief_yields_the_stack_card():
    subject = runner._gate_subject(
        None, "prj_1",
        {"id": "g", "binding": {"capability": "spec_review"}},
        {"input": {"directive": GO_BRIEF},
         "steps": {"meeting": {"prd_markdown": NODE_PRD}}},
    )
    assert [option["label"] for option in subject["stack_card"]["options"]] == [
        "Enable toolchain", "Accept alt", "Stop"]
    assert subject["stack_card"]["deviation"]["substituted"] == ["node"]


def test_a_go_prd_for_a_go_brief_is_not_a_stack_card():
    subject = runner._gate_subject(
        None, "prj_1",
        {"id": "g", "binding": {"capability": "spec_review"}},
        {"input": {"directive": GO_BRIEF},
         "steps": {"meeting": {"prd_markdown": GO_PRD}}},
    )
    assert "stack_card" not in (subject or {})


def test_a_stack_card_offers_enable_accept_or_stop_and_not_revise():
    card = stacks.stack_card(GO_BRIEF, NODE_PRD)
    actions = decision_actions.allowed_actions({
        "status": "pending",
        "recommendation": {
            "capability": "spec_review",
            "workflow_run_id": "wfr_1",
            "stack_card": card,
        },
    })
    assert [item["label"] for item in actions] == [
        "Enable toolchain", "Accept alt", "Stop"]
    assert [item["action"] for item in actions] == [
        "enable_toolchain", "accept_alt", "stop"]
    assert "revise" not in {item["action"] for item in actions}


def test_accept_alt_approves_and_enable_or_stop_refuses():
    from boss_engine.services.decisions import stack_answer

    assert stack_answer("accept_alt") == ("approve", "accept_alt")
    assert stack_answer("enable_toolchain") == ("reject", "enable_toolchain")
    assert stack_answer("stop") == ("reject", "stop")
    assert stack_answer("approve") is None
    assert stack_answer("Stack choice: accept_alt. Resolved from the console.") == (
        "approve", "accept_alt")


def test_a_deviant_prd_is_not_auto_approved():
    from boss_engine.services.meeting_service import prd_blocks_auto_approve

    assert prd_blocks_auto_approve(GO_BRIEF, NODE_PRD) is True
    assert prd_blocks_auto_approve(GO_BRIEF, GO_PRD) is False


def test_the_stack_buttons_are_actions_a_decision_can_take():
    from boss_engine.services.decisions import DecisionService

    with pytest.raises(ValueError, match="Unsupported action"):
        DecisionService(None).transition(
            None, "dec_1", action="not_a_button", actor="a", note=None)
    with pytest.raises(Exception) as caught:
        DecisionService(None).transition(
            None, "dec_1", action="accept_alt", actor="a", note=None)
    assert "Unsupported action" not in str(caught.value)
