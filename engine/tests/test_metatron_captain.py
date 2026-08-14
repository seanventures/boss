"""Metatron Captain — intent, scoring, and proposal-only drive.

These pin the captain contract: watch is evidence-only, drive never silently
applies BOSS self-modifications, and goals are scored from observed snapshots.
"""

from boss_engine.services.metatron_captain import (
    findings_from_snapshot,
    interpret_turn,
    measure_from_snapshot,
    proposed_moves_from_findings,
)


def test_interpret_turn_remember_goal_watch_drive():
    assert interpret_turn("remember the sandbox root is a volume")["intent"] == "remember"
    parsed = interpret_turn("set goal Ship HookWatch kind=working_software")
    assert parsed["intent"] == "set_goal"
    assert parsed["kind"] == "working_software"
    assert "HookWatch" in parsed["text"]
    assert interpret_turn("watch")["intent"] == "watch"
    assert interpret_turn("drive")["intent"] == "drive"
    assert interpret_turn("what is the budget")["intent"] == "status"


def test_working_software_goal_needs_preview_and_verify():
    half = measure_from_snapshot("working_software", {"completed_verify_green": 1, "running_previews": 0})
    assert half["score"] == 50
    full = measure_from_snapshot("working_software", {"completed_verify_green": 2, "running_previews": 1})
    assert full["score"] == 100
    assert full["status"] == "achieved"


def test_queue_goal_penalizes_deadletters():
    ok = measure_from_snapshot("queue", {"deadletters": 0, "pending_decisions": 1})
    assert ok["score"] == 100
    bad = measure_from_snapshot("queue", {"deadletters": 4, "pending_decisions": 10})
    assert bad["score"] < 80


def test_findings_never_auto_resolve_decisions():
    findings = findings_from_snapshot(
        {"deadletters": 2, "pending_decisions": 3, "running_previews": 0, "completed_verify_green": 1},
        [],
    )
    ids = {f["id"] for f in findings}
    assert "decisions" in ids
    assert "deadletters" in ids
    assert "preview_expired" in ids
    decision = next(f for f in findings if f["id"] == "decisions")
    assert decision["auto_safe"] is False
    moves = proposed_moves_from_findings(findings, auto=True)
    human = [m for m in moves if m.get("status") == "needs_human" or m.get("requires_human")]
    assert human, "pending decisions must stay human"
    assert all(m.get("status") != "applied" for m in moves)


def test_steady_watch_has_hold_finding():
    findings = findings_from_snapshot(
        {"deadletters": 0, "pending_decisions": 0, "running_previews": 1, "completed_verify_green": 1, "budget_state": "green"},
        [],
    )
    assert findings[0]["action"] == "hold"
    assert proposed_moves_from_findings(findings, auto=False) == []
