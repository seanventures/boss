"""Metatron command — fleet overwatch and bounded powers."""

from boss_engine.services.metatron_captain import (
    ACTION_KINDS,
    findings_from_snapshot,
    interpret_turn,
    proposed_moves_from_findings,
    validate_move,
)
from boss_engine.services.metatron_command import (
    COMMAND_KINDS,
    command_moves,
    fleet_findings,
    validate_command,
)


def test_fleet_findings_silent_without_overlay():
    assert fleet_findings({}) == []
    assert fleet_findings({"official_in_flight": 0, "factory_stalled": 0}) == []


def test_fleet_stall_proposes_reprioritize_staff_loop():
    findings = fleet_findings({"official_in_flight": 2, "factory_stalled": 3})
    assert findings[0]["id"] == "fleet_stall"
    assert findings[0]["action"] == "reprioritize_and_staff"
    moves = command_moves(findings, auto=True)
    kinds = [m["kind"] for m in moves]
    assert kinds == ["reprioritize_official", "staff_captain", "loop_playbook"]
    assert all(m["apply"] is True for m in moves)


def test_fleet_push_does_not_launch_factory():
    findings = fleet_findings({"official_in_flight": 1, "official_queued": 0})
    assert findings[0]["id"] == "fleet_push"
    moves = command_moves(findings, auto=True)
    assert {m["kind"] for m in moves} == {"staff_captain", "loop_playbook"}
    assert "launch_factory_job" not in {m["kind"] for m in moves}


def test_stalled_without_official_still_staffs():
    findings = fleet_findings({"factory_stalled": 2})
    assert findings[0]["action"] == "staff_and_loop"
    moves = command_moves(findings, auto=False)
    assert all(m["status"] == "proposed" for m in moves)
    assert all(m["apply"] is False for m in moves)


def test_validate_command_blocks_red_budget_and_unknown_kind():
    ok = validate_command(
        {"kind": "staff_captain", "apply": True},
        {"official_in_flight": 1},
        auto=True,
        budget_state="green",
    )
    assert ok["ok"] is True

    red = validate_command(
        {"kind": "reprioritize_official", "apply": True},
        {},
        auto=True,
        budget_state="red",
    )
    assert red["ok"] is False
    assert "budget_not_red" in red["failed"]

    unknown = validate_command({"kind": "nuke_prod", "apply": True}, {}, auto=True)
    assert unknown["ok"] is False
    assert "known_power" in unknown["failed"]


def test_validate_move_routes_command_kinds():
    verdict = validate_move(
        {"kind": "loop_playbook", "apply": True},
        {"official_in_flight": 1},
        auto=True,
        budget_state="green",
    )
    assert verdict["ok"] is True
    assert "known_power" in {c["name"] for c in verdict["checks"]}


def test_findings_merge_fleet_into_captain_watch():
    findings = findings_from_snapshot(
        {
            "deadletters": 0,
            "pending_decisions": 0,
            "running_previews": 1,
            "completed_verify_green": 1,
            "budget_state": "green",
            "official_in_flight": 2,
            "factory_stalled": 1,
        },
        [],
    )
    ids = {f["id"] for f in findings}
    assert "fleet_stall" in ids
    assert "steady" not in ids
    moves = proposed_moves_from_findings(findings, auto=True)
    kinds = {m["kind"] for m in moves}
    assert {"reprioritize_official", "staff_captain", "loop_playbook"} <= kinds
    assert "launch_factory_job" not in kinds


def test_command_kinds_are_known_actions():
    for kind in COMMAND_KINDS:
        assert kind in ACTION_KINDS


def test_interpret_theater_and_reprioritize():
    assert interpret_turn("theater")["intent"] == "theater"
    # Free text that reads like a theater order is a HINT; a model decides (decide_free_text).
    assert interpret_turn("reprioritize official")["intent"] == "theater_inferred"
    assert interpret_turn("staff captains")["intent"] == "theater_inferred"
    assert interpret_turn("assign more resources")["intent"] == "theater_inferred"
    assert interpret_turn("command the fleet")["intent"] == "theater_inferred"
    # Generic prioritize still exists for non-fleet language.
    assert interpret_turn("prioritize the deadletter queue")["intent"] == "prioritize"
