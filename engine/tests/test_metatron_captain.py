"""Metatron Captain — intent, scoring, validation, and overwatch.

These pin the captain contract: watch is evidence-only, drive never silently
applies BOSS self-modifications, and goals are scored from observed snapshots.
"""

from boss_engine.services.metatron_captain import (
    findings_from_snapshot,
    interpret_turn,
    measure_from_snapshot,
    proposed_moves_from_findings,
    validate_action,
    validate_move,
    verify_from_snapshots,
)


def test_interpret_turn_remember_goal_watch_drive():
    assert interpret_turn("remember the sandbox root is a volume")["intent"] == "remember"
    parsed = interpret_turn("set goal Ship HookWatch kind=working_software")
    assert parsed["intent"] == "set_goal"
    assert parsed["kind"] == "working_software"
    assert "HookWatch" in parsed["text"]
    assert interpret_turn("overwatch")["intent"] == "overwatch"
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
    assert interpret_turn("run overwatch")["intent"] == "overwatch"


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


def test_every_chat_action_is_validated():
    empty = validate_action("remember", {"text": ""})
    assert empty["ok"] is False
    assert "nonempty_content" in empty["failed"]

    remembered = validate_action("remember", {"text": "sandbox root is a volume"})
    assert remembered["ok"] is True

    bad_goal = validate_action("set_goal", {"text": "", "kind": "working_software"})
    assert bad_goal["ok"] is False
    assert "nonempty_title" in bad_goal["failed"]

    goal = validate_action("set_goal", {"text": "Ship HookWatch", "kind": "working_software"})
    assert goal["ok"] is True

    unknown = validate_action("explode_prod", {})
    assert unknown["ok"] is False
    assert "known_kind" in unknown["failed"]

    overwatch = validate_action("overwatch", {}, auto=True, budget_state="red")
    assert overwatch["ok"] is True, "overwatch still observes when budget is red"

    drive_red = validate_action("drive", {}, auto=True, budget_state="red")
    assert drive_red["ok"] is False
    assert "budget_not_red" in drive_red["failed"]


def test_overwatch_skips_factory_when_preview_restart_covers_working_software():
    findings = findings_from_snapshot(
        {"running_previews": 0, "completed_verify_green": 1, "deadletters": 0, "pending_decisions": 0},
        [{"id": "g1", "title": "Canaries work", "kind": "working_software", "status": "active"}],
    )
    ids = {f["id"] for f in findings}
    assert "preview_expired" in ids
    assert "goal:g1" not in ids

    factory = findings_from_snapshot(
        {"running_previews": 0, "completed_verify_green": 0, "deadletters": 0, "pending_decisions": 0},
        [{"id": "g2", "title": "Build HookWatch", "kind": "working_software", "status": "active"}],
    )
    pursue = next(f for f in factory if f["id"] == "goal:g2")
    assert pursue["action"] == "pursue_goal"
    assert pursue["auto_safe"] is True


def test_verify_from_snapshots_requires_applied_work_to_be_visible():
    before = {"running_previews": 0, "in_flight": 0, "completed_verify_green": 1}
    ok = verify_from_snapshots(
        before,
        {"running_previews": 1, "in_flight": 0, "completed_verify_green": 1},
        [{"kind": "restart_preview", "receipt_id": "rcpt_1"}],
    )
    assert ok["ok"] is True

    missing = verify_from_snapshots(
        before,
        {"running_previews": 0, "in_flight": 0, "completed_verify_green": 1},
        [{"kind": "restart_preview"}],
    )
    assert missing["ok"] is False
    assert "preview_running" in missing["failed"]

    observe = verify_from_snapshots(before, before, [])
    assert observe["ok"] is True
    assert any(c["name"] == "observe_only" for c in observe["checks"])


def test_steady_watch_has_hold_finding():
    findings = findings_from_snapshot(
        {"deadletters": 0, "pending_decisions": 0, "running_previews": 1, "completed_verify_green": 1, "budget_state": "green"},
        [],
    )
    assert findings[0]["action"] == "hold"
    assert proposed_moves_from_findings(findings, auto=False) == []


def _gapped_scorecard(ui=None, ux=None, capability=None):
    def dim(dim_id, score):
        if score is None:
            return {"id": dim_id, "status": "not_scored", "score": None, "reason": "missing evidence"}
        return {"id": dim_id, "status": "scored", "score": score}

    return {
        "version": "1.1",
        "dimensions": [dim("ui", ui), dim("ux", ux), dim("capability", capability)],
    }


def test_working_software_does_not_treat_scorecard_gaps_as_done():
    """Verify + preview is not a 100 when the attached scorecard still has UI/UX gaps."""
    measured = measure_from_snapshot(
        "working_software",
        {
            "completed_verify_green": 2,
            "running_previews": 1,
            "scorecard": _gapped_scorecard(ui=None, ux=40, capability=90),
        },
    )
    assert measured["score"] is not None
    assert measured["score"] < 80
    assert measured["status"] == "active"
    evidence = measured["evidence"]
    assert "ui" in str(evidence).lower()
    assert "ux" in str(evidence).lower()


def test_working_software_missing_scorecard_does_not_invent_quality_scores():
    """No scorecard on the snapshot must not invent ui/ux/capability = 100."""
    measured = measure_from_snapshot(
        "working_software",
        {"completed_verify_green": 2, "running_previews": 1},
    )
    assert measured["score"] == 100
    evidence = measured["evidence"]
    assert not evidence.get("scorecard_gaps")
    assert "ui" not in evidence
    assert evidence.get("ui") != 100
    assert evidence.get("ux") != 100
    assert evidence.get("capability") != 100


def test_working_software_findings_pursue_named_scorecard_gaps():
    snapshot = {
        "running_previews": 1,
        "completed_verify_green": 1,
        "deadletters": 0,
        "pending_decisions": 0,
        "budget_state": "green",
        "scorecard": _gapped_scorecard(ui=None, ux=42, capability=65),
    }
    findings = findings_from_snapshot(
        snapshot,
        [{"id": "g-ship", "title": "Ship HookWatch", "kind": "working_software", "status": "active"}],
    )
    pursue = [item for item in findings if item.get("action") == "pursue_goal"]
    assert pursue, "working_software must keep pursuing measured quality gaps"
    blob = " ".join(str(item.get("title") or "") for item in pursue).lower()
    assert "ui" in blob
    assert "ux" in blob
    assert "capability" in blob
    assert "not_scored" in blob
    assert "42" in blob or "65" in blob
    moves = proposed_moves_from_findings(findings, auto=True)
    factory = next(move for move in moves if move["kind"] == "launch_factory_job")
    assert factory["apply"] is True
    assert "ui" in str(factory["detail"]).lower()


def test_scorecard_gaps_still_pursue_when_preview_restart_also_applies():
    """Preview restart covers reachability; quality gaps still need a factory improve."""
    findings = findings_from_snapshot(
        {
            "running_previews": 0,
            "completed_verify_green": 1,
            "deadletters": 0,
            "pending_decisions": 0,
            "scorecard": _gapped_scorecard(ui=10, ux=None, capability=90),
        },
        [{"id": "g1", "title": "Canaries work", "kind": "working_software", "status": "active"}],
    )
    ids = {item["id"] for item in findings}
    assert "preview_expired" in ids
    pursue = [item for item in findings if item.get("action") == "pursue_goal"]
    assert pursue
    assert "ui" in pursue[0]["title"].lower()


def test_scorecard_gap_findings_never_auto_apply_decisions():
    findings = findings_from_snapshot(
        {
            "running_previews": 1,
            "completed_verify_green": 1,
            "deadletters": 1,
            "pending_decisions": 2,
            "scorecard": _gapped_scorecard(ui=None, ux=None, capability=None),
        },
        [{"id": "g-q", "title": "Ship it", "kind": "working_software", "status": "active"}],
    )
    decision = next(item for item in findings if item["id"] == "decisions")
    assert decision["auto_safe"] is False
    moves = proposed_moves_from_findings(findings, auto=True)
    human = [move for move in moves if move.get("status") == "needs_human" or move.get("requires_human")]
    assert human
    assert all(move.get("status") != "applied" for move in moves)
    assert any(move["kind"] == "launch_factory_job" for move in moves)
