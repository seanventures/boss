"""Metatron Captain — intent, scoring, and proposal-only drive.

These pin the captain contract: watch is evidence-only, drive never silently
applies BOSS self-modifications, and goals are scored from observed snapshots.
"""

from boss_engine.services.metatron_captain import (
    findings_from_snapshot,
    interpret_turn,
    measure_from_snapshot,
    proposed_moves_from_findings,
    validate_move,
)


def test_interpret_turn_remember_goal_watch_drive():
    assert interpret_turn("remember the sandbox root is a volume")["intent"] == "remember"
    parsed = interpret_turn("set goal Ship HookWatch kind=working_software")
    assert parsed["intent"] == "set_goal"
    assert parsed["kind"] == "working_software"
    assert "HookWatch" in parsed["text"]
    assert interpret_turn("watch")["intent"] == "watch"
    assert interpret_turn("drive")["intent"] == "drive"
    assert interpret_turn("drive")["auto"] is False
    assert interpret_turn("go")["intent"] == "drive"
    assert interpret_turn("go")["auto"] is True
    assert interpret_turn("build a settings page")["intent"] == "act"
    assert interpret_turn("what is the budget")["intent"] == "status"


def test_interpret_turn_build_and_prioritize():
    """NL leftovers after parent land: build/act vs question, plus prioritize."""
    assert interpret_turn("implement the next HookWatch slice")["intent"] == "act"
    assert interpret_turn("ship ApproveIt")["intent"] == "act"
    assert interpret_turn("fix the expired preview")["intent"] == "act"
    # Questions stay status Q&A — they must not become an act.
    assert interpret_turn("should we build this?")["intent"] == "status"
    assert interpret_turn("what should we implement?")["intent"] == "status"

    prio = interpret_turn("prioritize the deadletter queue")
    assert prio["intent"] == "prioritize"
    assert "deadletter" in prio["text"]
    assert interpret_turn("prioritise HookWatch")["intent"] == "prioritize"
    assert interpret_turn("p0 the queue")["intent"] == "prioritize"
    assert interpret_turn("P1 ApproveIt")["intent"] == "prioritize"

    auto = interpret_turn("drive auto")
    assert auto["intent"] == "drive"
    assert auto["auto"] is True
    propose = interpret_turn("propose next")
    assert propose["intent"] == "drive"
    assert propose["auto"] is False

    assert interpret_turn("")["intent"] == "help"
    assert interpret_turn("measure")["intent"] == "measure"
    assert interpret_turn("goals")["intent"] == "goals"
    assert interpret_turn("memory")["intent"] == "memory"


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
    preview = next(m for m in moves if m["kind"] == "restart_preview")
    assert preview["apply"] is True


def test_every_action_is_validated_before_apply():
    snapshot = {
        "budget_state": "green",
        "in_flight": 0,
        "completed_verify_green": 1,
        "running_previews": 0,
    }
    ok = validate_move(
        {"kind": "restart_preview", "apply": True},
        snapshot, auto=True, in_flight=0, budget_state="green",
        verify_green_run_id="crun_ok",
    )
    assert ok["ok"] is True
    assert ok["failed"] == []

    blocked = validate_move(
        {"kind": "escalate_decision", "status": "needs_human"},
        snapshot, auto=True, budget_state="green",
    )
    assert blocked["ok"] is False
    assert "not_human_only" in blocked["failed"]

    red = validate_move(
        {"kind": "launch_factory_job", "apply": True},
        snapshot, auto=True, in_flight=0, budget_state="red",
        verify_green_run_id="crun_ok",
    )
    assert red["ok"] is False
    assert "budget_not_red" in red["failed"]

    dup = validate_move(
        {"kind": "launch_factory_job", "apply": True},
        snapshot, auto=True, in_flight=2, budget_state="green",
    )
    assert dup["ok"] is False
    assert "no_duplicate_inflight" in dup["failed"]

    preview_miss = validate_move(
        {"kind": "restart_preview", "apply": True},
        snapshot, auto=True, budget_state="green", verify_green_run_id=None,
    )
    assert preview_miss["ok"] is False
    assert "verify_green_target" in preview_miss["failed"]


def test_steady_watch_has_hold_finding():
    findings = findings_from_snapshot(
        {"deadletters": 0, "pending_decisions": 0, "running_previews": 1, "completed_verify_green": 1, "budget_state": "green"},
        [],
    )
    assert findings[0]["action"] == "hold"
    assert proposed_moves_from_findings(findings, auto=False) == []
